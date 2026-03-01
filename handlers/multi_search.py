# handlers/multi_search.py
"""
Составной (мульти-сегментный) поиск авиабилетов.
Формат ссылки Aviasales: https://www.aviasales.ru/?params=MOW0503IST1103LON211
"""

import re
from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from services.flight_search import (
    format_avia_link_date,
    search_flights_multi,
    update_passengers_in_link,
    normalize_date,
)
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

router = Router()

MAX_SEGMENTS = 6


# ════════════════════════════════════════════════════════════════
# FSM
# ════════════════════════════════════════════════════════════════

class MultiSearch(StatesGroup):
    segment_origin = State()   # Ввод города вылета текущего сегмента
    segment_dest   = State()   # Ввод города прибытия
    segment_date   = State()   # Ввод даты
    passengers     = State()   # Кол-во пассажиров
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
    Формирует ссылку вида:
    https://www.aviasales.ru/?params=MOW0503IST1103LON211
    """
    params = ""
    for seg in segments:
        orig = seg["origin_iata"]
        dest = seg["dest_iata"]
        date = format_avia_link_date(seg["date"])
        params += f"{orig}{date}{dest}"
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


def _make_cancel_kb(add_segment_btn: bool = False, seg_count: int = 0) -> InlineKeyboardMarkup:
    rows = []
    if add_segment_btn and seg_count >= 2:
        rows.append([InlineKeyboardButton(
            text="✅ Готово, перейти к пассажирам",
            callback_data="ms_done_segments"
        )])
    rows.append([InlineKeyboardButton(text="✖ Отменить", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ════════════════════════════════════════════════════════════════
# Вход в составной поиск
# ════════════════════════════════════════════════════════════════

async def start_multi_search(message: Message, state: FSMContext):
    """Точка входа — вызывается из start.py."""
    await state.clear()
    await state.update_data(segments=[], pax_code="1")
    await state.set_state(MultiSearch.segment_origin)
    schedule_inactivity(message.chat.id, message.from_user.id)
    await message.answer(
        "✈️ <b>Составной маршрут</b>\n\n"
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

    data = await state.get_data()
    segments = data.get("segments", [])

    # Если это не первый сегмент — логично подсказать предыдущий пункт назначения
    if segments and segments[-1]["dest_iata"] == iata:
        pass  # OK, логично — продолжение маршрута

    # Временно сохраняем origin для текущего сегмента
    await state.update_data(
        _cur_origin_iata=iata,
        _cur_origin_name=name,
    )
    await state.set_state(MultiSearch.segment_dest)
    schedule_inactivity(message.chat.id, message.from_user.id)
    await message.answer(
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
        "Введите <b>дату вылета</b> в формате <code>ДД.ММ</code>:\n<i>Пример: 10.03</i>",
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
            "❌ Неверный формат даты.\n<i>Пример: 10.03</i>",
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
            prev_num = prev_m * 100 + prev_d
            cur_num  = cur_m  * 100 + cur_d
            if cur_num < prev_num:
                await message.answer(
                    f"❌ Дата перелёта не может быть раньше предыдущего ({prev_date}).\n"
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

    # Показываем текущие сегменты
    summary_text = "✅ <b>Текущий маршрут:</b>\n\n" + _segments_summary(segments)

    if seg_count >= MAX_SEGMENTS:
        # Достигнут лимит — переходим к пассажирам
        await state.set_state(MultiSearch.passengers)
        await message.answer(
            summary_text + "\n\n📌 Достигнут максимум 6 перелётов.",
            parse_mode="HTML",
        )
        await _ask_passengers(message, state)
        return

    # Кнопки: добавить ещё / завершить
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

    await state.set_state(MultiSearch.segment_origin)  # ждём следующий сегмент
    await message.answer(
        summary_text + f"\n\n{'Добавьте следующий перелёт или завершите маршрут.' if seg_count >= 2 else 'Добавьте следующий перелёт:'}",
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

    # Предзаполняем origin = предыдущий destination
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
async def ms_done_segments_from_origin(callback: CallbackQuery, state: FSMContext):
    await _finish_segments(callback, state)


async def _finish_segments(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    segments = data.get("segments", [])
    if len(segments) < 2:
        await callback.answer("Нужно хотя бы 2 перелёта!", show_alert=True)
        return
    await state.set_state(MultiSearch.passengers)
    await callback.answer()
    await _ask_passengers(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Пассажиры
# ════════════════════════════════════════════════════════════════

async def _ask_passengers(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"ms_pax_{i}") for i in range(1, 5)],
        [InlineKeyboardButton(text=str(i), callback_data=f"ms_pax_{i}") for i in range(5, 10)],
    ])
    await message.answer("Сколько пассажиров?", reply_markup=kb)


@router.callback_query(MultiSearch.passengers, F.data.regexp(r"^ms_pax_[1-9]$"))
async def ms_pax(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    count = int(callback.data.split("_")[2])
    await state.update_data(pax_code=str(count))
    await callback.answer()
    await _show_multi_summary(callback.message, state)


async def _show_multi_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    segments = data.get("segments", [])
    pax_code = data.get("pax_code", "1")

    text = (
        "📋 <b>Проверьте маршрут:</b>\n\n"
        + _segments_summary(segments)
        + f"\n\nПассажиры: {pax_code}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Найти билеты", callback_data="ms_confirm")],
        [InlineKeyboardButton(text="✏️ Изменить кол-во пасс.", callback_data="ms_edit_pax")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(MultiSearch.confirm)


@router.callback_query(MultiSearch.confirm, F.data == "ms_edit_pax")
async def ms_edit_pax(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MultiSearch.passengers)
    await callback.answer()
    await _ask_passengers(callback.message, state)


# ════════════════════════════════════════════════════════════════
# Подтверждение и генерация ссылки
# ════════════════════════════════════════════════════════════════

@router.callback_query(MultiSearch.confirm, F.data == "ms_confirm")
async def ms_confirm(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)

    data = await state.get_data()
    segments: list = data.get("segments", [])
    pax_code: str  = data.get("pax_code", "1")

    await callback.message.edit_text(
        "⏳ <b>Ищу билеты по вашему маршруту...</b>",
        parse_mode="HTML"
    )
    await state.clear()

    # Разбираем пассажиров
    try:
        adults   = int(pax_code[0])
        children = int(pax_code[1]) if len(pax_code) > 1 else 0
        infants  = int(pax_code[2]) if len(pax_code) > 2 else 0
    except (ValueError, IndexError):
        adults, children, infants = 1, 0, 0

    # Готовим сегменты для API (даты в формате YYYY-MM-DD)
    api_segments = [
        {
            "origin":      seg["origin_iata"],
            "destination": seg["dest_iata"],
            "date":        normalize_date(seg["date"]),
        }
        for seg in segments
    ]

    # Шаг 1: реальный запрос к API для получения booking URL
    api_url = await search_flights_multi(
        segments=api_segments,
        adults=adults,
        children=children,
        infants=infants,
    )

    if api_url:
        # Шаг 2: обновляем код пассажиров в ссылке из API
        booking_url = update_passengers_in_link(api_url, pax_code)
        logger.info(f"[MultiSearch] API URL получен: {booking_url[:80]}...")
    else:
        # Fallback: формируем ?params= URL напрямую
        booking_url = _build_multi_link(segments, pax_code)
        logger.info(f"[MultiSearch] Fallback URL: {booking_url[:80]}...")

    # Шаг 3: генерируем короткую партнёрскую ссылку (как в стандартном поиске)
    partner_link = await convert_to_partner_link(booking_url)

    logger.info(f"[MultiSearch] user={callback.from_user.id} partner_link={partner_link[:60]}...")

    route_preview = _segments_summary(segments)
    text = (
        "✅ <b>Составной маршрут готов!</b>\n\n"
        f"{route_preview}\n\n"
        f"Пассажиры: {pax_code}\n\n"
        "<i>Нажмите кнопку ниже — Aviasales покажет все варианты по вашему маршруту.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Посмотреть билеты", url=partner_link)],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
