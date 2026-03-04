# handlers/multi_search.py
"""
Составной (мульти-сегментный) поиск авиабилетов.
Формат ссылки Aviasales: https://www.aviasales.ru/?params=MOW0503IST1103LON211
"""

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from services.flight_search import format_avia_link_date
from utils.cities_loader import (
    get_iata,
    get_city_name,
    fuzzy_get_iata,
    CITY_TO_IATA,
    IATA_TO_CITY,
    _normalize_name,
)
from utils.link_converter import convert_to_partner_link
from utils.logger import logger
from utils.smart_reminder import cancel_inactivity, schedule_inactivity, mark_fsm_inactive
from handlers.flight_constants import CANCEL_KB
from utils.date_hints import hint_depart

router = Router()

MAX_SEGMENTS = 6


# ════════════════════════════════════════════════════════════════
# FSM
# ════════════════════════════════════════════════════════════════

class MultiSearch(StatesGroup):
    segment_origin = State()   # Ввод города вылета текущего сегмента
    segment_dest   = State()   # Ввод города прибытия
    segment_date   = State()   # Ввод даты
    adults         = State()   # Кол-во взрослых
    has_children   = State()   # Есть ли дети?
    children       = State()   # Кол-во детей
    infants        = State()   # Кол-во младенцев
    confirm        = State()   # Подтверждение перед поиском


# ════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════

def _resolve_city(text: str) -> tuple[str | None, str | None]:
    """Возвращает (iata, city_name) или (None, None)."""
    text = text.strip().lower()
    iata = get_iata(text) or CITY_TO_IATA.get(_normalize_name(text))
    if not iata:
        return None, None
    name = get_city_name(iata) or IATA_TO_CITY.get(iata, iata.upper())
    return iata, name


def _validate_date(date_str: str) -> bool:
    try:
        day, month = map(int, date_str.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except Exception:
        return False


def _build_multi_link(segments: list[dict], pax_code: str) -> str:
    """
    Формирует ?params= ссылку для Aviasales.

    Формат Aviasales для составного маршрута:
      {origin1}{date1}{city1}{date2}{city2}...{dateN}{destN}{pax}
    Город-стык пишется ОДИН раз — он одновременно прилёт и вылет.

    Пример (3 сегмента):
      MOW → IST (10.03), IST → LON (15.03), LON → DXB (20.03), 2 взр., 1 реб., 1 млад.
      → MOW1003IST1503LON2003DXB211

    Если сегменты НЕ соединены (origin != предыдущий dest), город вылета
    добавляется явно перед датой.
    """
    if not segments:
        return f"https://www.aviasales.ru/?params={pax_code}"

    # Первый сегмент: origin + date + dest
    params = (
        segments[0]["origin_iata"]
        + format_avia_link_date(segments[0]["date"])
        + segments[0]["dest_iata"]
    )

    # Последующие сегменты
    for i, seg in enumerate(segments[1:], start=1):
        prev_dest = segments[i - 1]["dest_iata"]
        if seg["origin_iata"] != prev_dest:
            # Несвязанный сегмент — добавляем origin явно
            params += seg["origin_iata"]
        # Дата + destination (origin уже есть как предыдущий dest)
        params += format_avia_link_date(seg["date"]) + seg["dest_iata"]

    params += pax_code
    return f"https://www.aviasales.ru/?params={params}"


def _segments_summary(segments: list[dict]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        orig = seg.get("origin_name", seg.get("origin_iata", "?"))
        dest = seg.get("dest_name", seg.get("dest_iata", "?"))
        date = seg.get("date", "?")
        lines.append(f"{i}. {orig} → {dest} · {date}")
    return "\n".join(lines)


def _build_pax_code(adults: int, children: int = 0, infants: int = 0) -> str:
    """Строит код пассажиров: '1', '21', '211' и т.д."""
    adults = max(1, adults)
    total  = adults + children + infants
    if total > 9:
        remaining = 9 - adults
        children  = min(children, remaining)
        infants   = max(0, min(remaining - children, adults))
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    return code


def _build_pax_desc(adults: int, children: int = 0, infants: int = 0) -> str:
    desc = f"{adults} взр."
    if children:
        desc += f", {children} дет."
    if infants:
        desc += f", {infants} мл."
    return desc


# ════════════════════════════════════════════════════════════════
# Вход в составной поиск
# ════════════════════════════════════════════════════════════════

async def start_multi_search(message: Message, state: FSMContext):
    """Точка входа — вызывается из start.py."""
    await state.clear()
    await state.update_data(segments=[])
    await state.set_state(MultiSearch.segment_origin)
    schedule_inactivity(message.chat.id, message.from_user.id)
    await message.answer(
        "🗺 <b>Составной маршрут — Шаг 1</b>\n\n"
        "Добавьте от 2 до 6 перелётов.\n"
        "Введите <b>город отправления</b> первого перелёта:\n\n"
        "<i>Пример: Москва</i>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB,
    )


# ════════════════════════════════════════════════════════════════
# FSM: город вылета
# ════════════════════════════════════════════════════════════════

@router.message(MultiSearch.segment_origin)
async def ms_origin(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    text = (message.text or "").strip()

    iata, name = _resolve_city(text)
    if not iata:
        fuzzy_iata, fuzzy_name = fuzzy_get_iata(text)
        if fuzzy_iata:
            await message.answer(
                f"❓ Не нашёл «{text}» — возможно, вы имели в виду <b>{fuzzy_name}</b>?\n"
                "Напишите правильное название города.",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        else:
            await message.answer(
                f"❌ Неизвестный город: <b>{text}</b>\nПроверьте написание.",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        return

    await state.update_data(_cur_origin_iata=iata, _cur_origin_name=name)
    await state.set_state(MultiSearch.segment_dest)
    schedule_inactivity(message.chat.id, message.from_user.id)
    data_tmp = await state.get_data()
    seg_num = len(data_tmp.get("segments", [])) + 1
    await message.answer(
        f"🗺 <b>Перелёт {seg_num} — город прибытия</b>\n\n"
        f"📍 Вылет: <b>{name}</b>\n\n"
        "Введите <b>город прибытия</b>:\n<i>Пример: Стамбул</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )


# ════════════════════════════════════════════════════════════════
# FSM: город прибытия
# ════════════════════════════════════════════════════════════════

@router.message(MultiSearch.segment_dest)
async def ms_dest(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    text = (message.text or "").strip()
    data = await state.get_data()
    cur_origin_iata = data.get("_cur_origin_iata", "")

    iata, name = _resolve_city(text)
    if not iata:
        fuzzy_iata, fuzzy_name = fuzzy_get_iata(text)
        if fuzzy_iata:
            await message.answer(
                f"❓ Не нашёл «{text}» — возможно, вы имели в виду <b>{fuzzy_name}</b>?\n"
                "Напишите правильное название города.",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        else:
            await message.answer(
                f"❌ Неизвестный город: <b>{text}</b>\nПроверьте написание.",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        return

    if iata == cur_origin_iata:
        await message.answer(
            "❌ Город прибытия не может совпадать с городом вылета.\nВведите другой город.",
            reply_markup=CANCEL_KB,
        )
        return

    await state.update_data(_cur_dest_iata=iata, _cur_dest_name=name)
    await state.set_state(MultiSearch.segment_date)
    schedule_inactivity(message.chat.id, message.from_user.id)

    orig_name = data.get("_cur_origin_name", cur_origin_iata)
    await message.answer(
        f"📍 {orig_name} → <b>{name}</b>\n\n"
        f"Введите <b>дату вылета</b> в формате <code>ДД.ММ</code>:\n<i>Пример: {hint_depart()}</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )


# ════════════════════════════════════════════════════════════════
# FSM: дата
# ════════════════════════════════════════════════════════════════

@router.message(MultiSearch.segment_date)
async def ms_date(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    date_text = (message.text or "").strip()

    if not _validate_date(date_text):
        await message.answer(
            f"❌ Неверный формат даты.\n<i>Пример: {hint_depart()}</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    data = await state.get_data()
    segments: list = list(data.get("segments", []))

    # Проверка: дата не раньше предыдущего сегмента
    if segments:
        prev_date = segments[-1]["date"]
        try:
            prev_d, prev_m = map(int, prev_date.split('.'))
            cur_d, cur_m   = map(int, date_text.split('.'))
            if cur_m * 100 + cur_d < prev_m * 100 + prev_d:
                await message.answer(
                    f"❌ Дата не может быть раньше предыдущего перелёта ({prev_date}).\n"
                    "Введите корректную дату.",
                    reply_markup=CANCEL_KB,
                )
                return
        except Exception:
            pass

    new_seg = {
        "origin_iata": data["_cur_origin_iata"],
        "origin_name": data["_cur_origin_name"],
        "dest_iata":   data["_cur_dest_iata"],
        "dest_name":   data["_cur_dest_name"],
        "date":        date_text,
    }
    segments.append(new_seg)
    await state.update_data(segments=segments)

    seg_count = len(segments)
    schedule_inactivity(message.chat.id, message.from_user.id)

    summary_text = "✅ <b>Текущий маршрут:</b>\n\n" + _segments_summary(segments)

    if seg_count >= MAX_SEGMENTS:
        await message.answer(
            summary_text + "\n\n📌 Достигнут максимум 6 перелётов.",
            parse_mode="HTML",
        )
        await _ask_adults(message, state)
        return

    rows = []
    if seg_count >= 2:
        rows.append([InlineKeyboardButton(
            text="✅ Завершить маршрут",
            callback_data="ms_done_segments",
        )])
    rows.append([InlineKeyboardButton(
        text=f"➕ Добавить перелёт {seg_count + 1}",
        callback_data="ms_add_segment",
    )])
    rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await state.set_state(MultiSearch.segment_origin)
    await message.answer(
        summary_text + (
            "\n\nДобавьте следующий перелёт или завершите маршрут."
            if seg_count >= 2 else
            "\n\nДобавьте следующий перелёт:"
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )


# ════════════════════════════════════════════════════════════════
# Кнопки управления сегментами
# ════════════════════════════════════════════════════════════════

@router.callback_query(MultiSearch.segment_origin, F.data == "ms_add_segment")
async def ms_add_segment(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    segments = data.get("segments", [])

    if segments:
        last = segments[-1]
        await state.update_data(
            _cur_origin_iata=last["dest_iata"],
            _cur_origin_name=last["dest_name"],
        )
        await state.set_state(MultiSearch.segment_dest)
        await callback.message.answer(
            f"📍 Вылет: <b>{last['dest_name']}</b> (продолжение маршрута)\n\n"
            "Введите <b>город прибытия</b>:",
            parse_mode="HTML",
            reply_markup=CANCEL_KB,
        )
    else:
        await callback.message.answer(
            "Введите город отправления:",
            reply_markup=CANCEL_KB,
        )
    await callback.answer()


@router.callback_query(MultiSearch.segment_origin, F.data == "ms_done_segments")
async def ms_done_segments(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    if len(data.get("segments", [])) < 2:
        await callback.answer("Нужно хотя бы 2 перелёта!", show_alert=True)
        return
    await callback.answer()
    await _ask_adults(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Пассажиры: взрослые
# ════════════════════════════════════════════════════════════════

async def _ask_adults(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"ms_adults_{i}") for i in range(1, 5)],
        [InlineKeyboardButton(text=str(i), callback_data=f"ms_adults_{i}") for i in range(5, 10)],
    ])
    await message.answer("Сколько взрослых пассажиров (от 12 лет)?", reply_markup=kb)
    await state.set_state(MultiSearch.adults)


@router.callback_query(MultiSearch.adults, F.data.regexp(r"^ms_adults_[1-9]$"))
async def ms_adults(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    adults = int(callback.data.split("_")[-1])
    await state.update_data(adults=adults)
    await callback.answer()

    if adults == 9:
        await state.update_data(
            children=0, infants=0,
            pax_code="9",
            pax_desc="9 взр.",
        )
        await _show_multi_summary(callback.message, state)
    else:
        await _ask_has_children(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Пассажиры: есть ли дети?
# ════════════════════════════════════════════════════════════════

async def _ask_has_children(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👶 Да", callback_data="ms_hc_yes"),
         InlineKeyboardButton(text="✅ Нет", callback_data="ms_hc_no")],
    ])
    await message.answer("С вами летят дети?", reply_markup=kb)
    await state.set_state(MultiSearch.has_children)


@router.callback_query(MultiSearch.has_children, F.data.in_({"ms_hc_yes", "ms_hc_no"}))
async def ms_has_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    await callback.answer()

    if callback.data == "ms_hc_yes":
        await _ask_children(callback.message, state)
    else:
        data = await state.get_data()
        adults = data["adults"]
        await state.update_data(
            children=0, infants=0,
            pax_code=str(adults),
            pax_desc=f"{adults} взр.",
        )
        await _show_multi_summary(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Пассажиры: кол-во детей (2–11 лет)
# ════════════════════════════════════════════════════════════════

async def _ask_children(message: Message, state: FSMContext):
    data   = await state.get_data()
    adults = data["adults"]
    max_ch = 9 - adults
    nums   = list(range(0, max_ch + 1))
    rows   = [[InlineKeyboardButton(text=str(n), callback_data=f"ms_ch_{n}") for n in nums[i:i+5]]
              for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        "Сколько детей (от 2 до 11 лет)?\nЕсли у вас младенцы, укажете дальше.",
        reply_markup=kb,
    )
    await state.set_state(MultiSearch.children)


@router.callback_query(MultiSearch.children, F.data.regexp(r"^ms_ch_\d+$"))
async def ms_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data     = await state.get_data()
    adults   = data["adults"]
    children = int(callback.data.split("_")[-1])

    if children < 0 or children > 9 - adults:
        await callback.answer()
        return

    await state.update_data(children=children)
    await callback.answer()

    if 9 - adults - children == 0:
        pax_code = _build_pax_code(adults, children, 0)
        pax_desc = _build_pax_desc(adults, children, 0)
        await state.update_data(infants=0, pax_code=pax_code, pax_desc=pax_desc)
        await _show_multi_summary(callback.message, state)
    else:
        await _ask_infants(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Пассажиры: младенцы (до 2 лет, без места)
# ════════════════════════════════════════════════════════════════

async def _ask_infants(message: Message, state: FSMContext):
    data     = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    max_inf  = min(adults, 9 - adults - children)
    nums     = list(range(0, max_inf + 1))
    rows     = [[InlineKeyboardButton(text=str(n), callback_data=f"ms_inf_{n}") for n in nums[i:i+5]]
                for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Сколько младенцев? (младше 2 лет, без места)", reply_markup=kb)
    await state.set_state(MultiSearch.infants)


@router.callback_query(MultiSearch.infants, F.data.regexp(r"^ms_inf_\d+$"))
async def ms_infants(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data     = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    infants  = int(callback.data.split("_")[-1])

    if infants < 0 or infants > min(adults, 9 - adults - children):
        await callback.answer()
        return

    pax_code = _build_pax_code(adults, children, infants)
    pax_desc = _build_pax_desc(adults, children, infants)
    await state.update_data(infants=infants, pax_code=pax_code, pax_desc=pax_desc)
    await callback.answer()
    await _show_multi_summary(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Экран подтверждения
# ════════════════════════════════════════════════════════════════

async def _show_multi_summary(message: Message, state: FSMContext):
    data     = await state.get_data()
    segments = data.get("segments", [])
    pax_desc = data.get("pax_desc", "1 взр.")

    text = (
        "📋 <b>Проверьте маршрут:</b>\n\n"
        + _segments_summary(segments)
        + f"\n\nПассажиры: {pax_desc}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Найти билеты", callback_data="ms_confirm")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="ms_edit_pax")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(MultiSearch.confirm)


@router.callback_query(MultiSearch.confirm, F.data == "ms_edit_pax")
async def ms_edit_pax(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    await callback.answer()
    await _ask_adults(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Подтверждение и генерация ссылки
# ════════════════════════════════════════════════════════════════

@router.callback_query(MultiSearch.confirm, F.data == "ms_confirm")
async def ms_confirm(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)

    data     = await state.get_data()
    segments = data.get("segments", [])
    pax_code = data.get("pax_code", "1")
    pax_desc = data.get("pax_desc", "1 взр.")

    # Разбираем pax_code → adults / children / infants для API
    try:
        adults   = int(pax_code[0])
        children = int(pax_code[1]) if len(pax_code) > 1 else 0
        infants  = int(pax_code[2]) if len(pax_code) > 2 else 0
    except (ValueError, IndexError):
        adults, children, infants = 1, 0, 0

    await callback.message.edit_text(
        "⏳ <b>Ищу билеты по вашему маршруту...</b>",
        parse_mode="HTML",
    )
    await state.clear()

    # Строим ?params= URL со ВСЕМИ сегментами маршрута.
    # RT API возвращает deep-link на конкретный рейс (только 1 сегмент) —
    # для составного маршрута он не подходит. Aviasales ?params= — единственный
    # корректный формат поисковой ссылки с несколькими сегментами.
    booking_url = _build_multi_link(segments, pax_code)
    logger.info(f"[MultiSearch] booking_url: {booking_url}")

    # Короткая партнёрская ссылка — как в стандартном поиске
    partner_link = await convert_to_partner_link(booking_url)
    logger.info(f"[MultiSearch] user={callback.from_user.id} partner={partner_link[:60]}...")

    route_preview = _segments_summary(segments)
    text = (
        "✅ <b>Составной маршрут готов!</b>\n\n"
        f"{route_preview}\n\n"
        f"👥 Пассажиры: {pax_desc}\n\n"
        "─────────────────\n"
        "👇 <b>Маршрут уже заполнен на Aviasales.</b>\n"
        "Нажмите кнопку ниже, затем <b>«Найти билеты»</b> — "
        "и увидите все варианты с ценами."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Открыть поиск на Aviasales", url=partner_link)],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()