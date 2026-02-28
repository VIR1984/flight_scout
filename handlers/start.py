# handlers/start.py
import json
import asyncio
import os
import re
from uuid import uuid4
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

from services.flight_search import (
    search_flights_realtime,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    find_cheapest_flight_on_exact_date,
    update_passengers_in_link,
    format_passenger_desc,
)
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities_loader import get_iata, get_city_name, fuzzy_get_iata, CITY_TO_IATA, IATA_TO_CITY, _normalize_name
from utils.redis_client import redis_client
from utils.logger import logger
from utils.link_converter import convert_to_partner_link
from utils.smart_reminder import (
    schedule_inactivity,
    cancel_inactivity,
    mark_fsm_inactive,
    remind_after_search,
)
from handlers.quick_search import handle_flight_request
from handlers.flight_constants import (
    CANCEL_KB,
    NAV_KB,
    MULTI_AIRPORT_CITIES,
    AIRPORT_TO_METRO,
    AIRPORT_NAMES,
    SUPPORTED_TRANSFER_AIRPORTS,
    AIRLINE_NAMES,
)
from handlers.everywhere_search import (
    search_origin_everywhere,
    search_destination_everywhere,
    process_everywhere_search,
    format_user_date,
    build_passenger_desc,
)

router = Router()

# Семафор: не более 10 параллельных поисков (защита от перегрузки API)
_SEARCH_SEMAPHORE = asyncio.Semaphore(10)

# Контекст трансферов: user_id → dict
transfer_context: dict[int, dict] = {}


# ════════════════════════════════════════════════════════════════
# FSM
# ════════════════════════════════════════════════════════════════

class FlightSearch(StatesGroup):
    route          = State()
    choose_airport = State()
    depart_date    = State()
    need_return    = State()
    return_date    = State()
    flight_type    = State()
    adults         = State()
    has_children   = State()
    children       = State()
    infants        = State()
    confirm        = State()


# ════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════

def _get_metro(iata: str) -> str | None:
    """SVO → MOW, MOW → MOW, AER → None"""
    if iata in MULTI_AIRPORT_CITIES:
        return iata
    return AIRPORT_TO_METRO.get(iata)


def _has_multi_airports(iata: str) -> bool:
    metro = _get_metro(iata)
    return bool(metro and len(MULTI_AIRPORT_CITIES.get(metro, [])) > 1)


def _airport_keyboard(metro_iata: str, city_name: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=ap_label, callback_data=f"ap_pick_{ap_iata}")]
        for ap_iata, ap_label in MULTI_AIRPORT_CITIES.get(metro_iata, [])
    ]
    rows.append([InlineKeyboardButton(text="Любой аэропорт", callback_data=f"ap_any_{metro_iata}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def validate_route(text: str) -> tuple[str, str]:
    text = text.strip().lower()
    if re.search(r'\s+[-→—>]+\s+', text):
        # "Москва - Сочи", "Москва → Сочи"
        parts = re.split(r'\s+[-→—>]+\s+', text, maxsplit=1)
    elif re.search(r'[→—>]+', text):
        # "Москва→Сочи"
        parts = re.split(r'[→—>]+', text, maxsplit=1)
    elif re.search(r'(?<=[а-яёa-z])-(?=[а-яёa-z])', text):
        # "Москва-Сочи" — дефис без пробелов между буквами
        parts = re.split(r'(?<=[а-яёa-z])-(?=[а-яёa-z])', text, maxsplit=1)
    else:
        parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None, None
    origin = parts[0].strip().replace("санкт петербург", "санкт-петербург")
    dest   = parts[1].strip().replace("ростов на дону", "ростов-на-дону")
    return origin, dest


def validate_date(date_str: str) -> bool:
    try:
        day, month = map(int, date_str.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except Exception:
        return False



# ── Склонение городов (родительный падеж: "из Москвы") ─────────────────────
_GENITIVE = {
    "Москва": "Москвы",
    "Санкт-Петербург": "Санкт-Петербурга",
    "Ростов-на-Дону": "Ростова-на-Дону",
    "Нижний Новгород": "Нижнего Новгорода",
    "Екатеринбург": "Екатеринбурга",
    "Новосибирск": "Новосибирска",
    "Владивосток": "Владивостока",
    "Хабаровск": "Хабаровска",
    "Красноярск": "Красноярска",
    "Краснодар": "Краснодара",
    "Самара": "Самары",
    "Уфа": "Уфы",
    "Казань": "Казани",
    "Пермь": "Перми",
    "Воронеж": "Воронежа",
    "Волгоград": "Волгограда",
    "Ростов": "Ростова",
    "Омск": "Омска",
    "Иркутск": "Иркутска",
    "Сочи": "Сочи",
    "Баку": "Баку",
    "Тбилиси": "Тбилиси",
    "Токио": "Токио",
    "Осло": "Осло",
    "Дели": "Дели",
    "Гоа": "Гоа",
    "Батуми": "Батуми",
}

def _genitive(city: str) -> str:
    """Склоняет город в родительный падеж. "Москва" → "Москвы"."""
    if not city:
        return city
    if city in _GENITIVE:
        return _GENITIVE[city]
    # Простые правила для остальных
    if city.endswith("а") and not city.endswith("ия"):
        return city[:-1] + "ы"
    if city.endswith("я"):
        return city[:-1] + "и"
    if city.endswith("ия"):
        return city[:-2] + "ии"
    if city[-1].lower() in "бвгджзйклмнпрстфхцчшщ":
        return city + "а"
    return city


def _flight_type_text_to_code(text: str) -> str:
    return {"Прямые": "direct", "С пересадкой": "transfer", "Все варианты": "all"}.get(text, "all")


def build_choices_summary(data: dict) -> str:
    lines = []
    n = 1
    ap_label = data.get("origin_airport_label", "")
    route = f"{data.get('origin_name', '')} → {data.get('dest_name', '')}"
    if ap_label:
        route += f"  ({ap_label})"
    lines.append(f"{n}. Маршрут: {route}"); n += 1

    depart_date = data.get("depart_date", "")
    lines.append(f"{n}. Дата вылета: {format_user_date(depart_date) if depart_date else ''}"); n += 1

    need_return = data.get("need_return")
    if need_return is not None:
        if need_return and data.get("return_date"):
            lines.append(f"{n}. Обратный билет: {format_user_date(data['return_date'])}")
        elif need_return:
            lines.append(f"{n}. Обратный билет: да")
        else:
            lines.append(f"{n}. Обратный билет: нет")
        n += 1

    if "flight_type" in data:
        ft_map = {"direct": "прямые рейсы", "transfer": "рейсы с пересадками", "all": "все варианты"}
        lines.append(f"{n}. Тип рейса: {ft_map.get(data['flight_type'], 'все варианты')}"); n += 1

    if "passenger_desc" in data or "adults" in data:
        pd = data.get("passenger_desc")
        if not pd:
            a, c, i = data.get("adults", 1), data.get("children", 0), data.get("infants", 0)
            pd = f"{a} взр." + (f", {c} дет." if c else "") + (f", {i} мл." if i else "")
        lines.append(f"{n}. Пассажиры: {pd}")

    return "\n".join(lines)


def build_passenger_code(adults: int, children: int = 0, infants: int = 0) -> str:
    adults = max(1, adults)
    total  = adults + children + infants
    if total > 9:
        remaining = 9 - adults
        children  = min(children, remaining)
        infants   = max(0, remaining - children)
        infants   = min(infants, adults)
    code = str(adults)
    if children > 0: code += str(children)
    if infants  > 0: code += str(infants)
    return code


def _format_datetime(dt_str: str) -> str:
    if not dt_str:
        return "??:??"
    try:
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime("%H:%M")
    except Exception:
        return dt_str.split('T')[1][:5] if 'T' in dt_str else "??:??"


def _format_duration(minutes: int) -> str:
    if not minutes:
        return "—"
    parts = []
    if minutes // 60: parts.append(f"{minutes // 60}ч")
    if minutes %  60: parts.append(f"{minutes % 60}м")
    return " ".join(parts) or "—"

# ════════════════════════════════════════════════════════════════
# /start и главное меню
# ════════════════════════════════════════════════════════════════

# Inline-подсказка под приветствием — только при первом входе.
# Дальше пользователь пользуется нижней панелью NAV_KB.
def _welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты",        callback_data="start_search")],
        [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
    ])

MAIN_MENU_TEXT = (
    "👋 Привет! Я помогу найти дешёвые авиабилеты.\n\n"
    "Используйте кнопки <b>внизу экрана</b> для навигации."
)

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    # NAV_KB отправляется один раз и остаётся как постоянная нижняя панель
    await message.answer(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=NAV_KB)
    # Inline-подсказка поверх для наглядности при первом запуске
    await message.answer("С чего начнём?", reply_markup=_welcome_kb())


@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)
    if state:
        await state.clear()
    try:
        await callback.message.edit_text(
            "Выберите раздел в нижней панели навигации.",
            reply_markup=_welcome_kb(),
        )
    except Exception:
        await callback.message.answer(
            "Выберите раздел в нижней панели навигации.",
            reply_markup=_welcome_kb(),
        )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Обработчики кнопок нижней панели (ReplyKeyboard)
# ════════════════════════════════════════════════════════════════

@router.message(F.text == "✈️ Поиск")
async def nav_search(message: Message, state: FSMContext):
    """Нижняя панель → Поиск."""
    current = await state.get_state()
    if current and not current.startswith("FlyStackTrack"):
        await state.clear()
    cancel_inactivity(message.chat.id)
    await message.answer(
        "Начнём поиск билетов 👌\n\n"
        "<b>Напишите маршрут:</b> Город отправления — Город прибытия\n\n"
        "<i>Пример: Москва — Сочи</i>\n\n"
        "Если ещё не решили откуда или куда — напишите «Везде».",
        parse_mode="HTML",
        reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.route)
    schedule_inactivity(message.chat.id, message.from_user.id)


@router.message(F.text == "🔥 Горячие")
async def nav_hot(message: Message, state: FSMContext):
    """Нижняя панель → Горячие предложения."""
    cancel_inactivity(message.chat.id)
    await state.clear()
    user_id = message.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    text = (
        "🔥 <b>Горячие предложения</b>\n\n"
        "Укажите направления и бюджет — бот сам следит за ценами "
        "и пришлёт уведомление, когда появятся выгодные билеты."
    )
    buttons = [[InlineKeyboardButton(text="⚙️ Настроить подписку", callback_data="hd_new_sub")]]
    if subs:
        buttons.append([InlineKeyboardButton(
            text=f"📋 Мои подписки ({len(subs)})", callback_data="hd_my_subs"
        )])
    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text == "📋 Подписки")
async def nav_subs(message: Message, state: FSMContext):
    """Нижняя панель → Подписки."""
    cancel_inactivity(message.chat.id)
    user_id = message.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    if not subs:
        await message.answer(
            "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.\n"
            "Хотите настроить уведомления о горячих ценах?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔥 Создать подписку", callback_data="hd_new_sub")]
            ]),
        )
    else:
        from handlers.hot_deals import hd_my_subs_text_kb
        text, kb = await hd_my_subs_text_kb(user_id, subs)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "❓ Помощь")
async def nav_help(message: Message, state: FSMContext):
    """Нижняя панель → Помощь."""
    text = (
        "❓ <b>Как пользоваться</b>\n\n"
        "✈️ <b>Поиск</b> — маршрут, даты, пассажиры → лучшие цены.\n\n"
        "🔥 <b>Горячие</b> — подпишитесь на направления, "
        "и я напишу когда цена упадёт на 10%+.\n\n"
        "📋 <b>Подписки</b> — просмотр и управление активными подписками.\n\n"
        "<i>Нажмите «✈️ Поиск» чтобы начать.</i>"
    )
    await message.answer(text, parse_mode="HTML")


# ════════════════════════════════════════════════════════════════
# П.6 — Справка (callback, для обратной совместимости)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "help_info")
async def handle_help(callback: CallbackQuery):
    text = (
        "❓ <b>Как пользоваться</b>\n\n"
        "✈️ <b>Поиск билетов</b> — маршрут, даты, пассажиры → лучшие цены.\n\n"
        "🔥 <b>Горячие предложения</b> — подпишитесь, "
        "и я напишу когда цена упадёт на 10%+.\n\n"
        "📋 <b>Подписки</b> — просмотр и управление.\n\n"
        "📉 <b>Следить за ценой</b> — уведомление при изменении цены на маршруте."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# П.7 — Мои подписки (из callback, для обратной совместимости)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "my_subscriptions")
async def handle_my_subscriptions(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    if not subs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Создать подписку", callback_data="hd_new_sub")],
        ])
        try:
            await callback.message.edit_text(
                "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.\n"
                "Хотите настроить уведомления о горячих ценах?",
                parse_mode="HTML", reply_markup=kb,
            )
        except Exception:
            await callback.message.answer(
                "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.",
                parse_mode="HTML", reply_markup=kb,
            )
        await callback.answer()
        return
    from handlers.hot_deals import hd_my_subs
    await hd_my_subs(callback, state)


# ════════════════════════════════════════════════════════════════
# FSM: начало поиска
# ════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════
# Умное продолжение — кнопка "Продолжить поиск" из напоминания
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "continue_search")
async def handle_continue_search(callback: CallbackQuery, state: FSMContext):
    """Возвращает пользователя ровно на тот шаг FSM, где он остановился."""
    cancel_inactivity(callback.message.chat.id)
    current = await state.get_state()
    data    = await state.get_data()

    # Нет активного FSM — начинаем заново
    if not current or not current.startswith("FlightSearch"):
        try:
            await callback.message.edit_text(
                "Начнём поиск 👌\n\n"
                "<b>Напишите маршрут:</b> Город отправления - Город прибытия\n"
                "<i>Пример: Москва - Сочи</i>",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        except Exception:
            await callback.message.answer(
                "Начнём поиск 👌\n\n"
                "<b>Напишите маршрут:</b> Город отправления - Город прибытия\n"
                "<i>Пример: Москва - Сочи</i>",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
        await state.set_state(FlightSearch.route)
        schedule_inactivity(callback.message.chat.id, callback.from_user.id)
        await callback.answer()
        return

    await callback.answer("▶️ Продолжаем!")

    # Каждый шаг воспроизводит нужный вопрос без сброса данных
    if current == FlightSearch.route.state:
        origin = data.get("origin_name", "")
        hint = f"\n<i>Последний ввод: {origin}</i>" if origin else ""
        await callback.message.answer(
            f"<b>Маршрут:</b> Город отправления - Город прибытия{hint}\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )

    elif current == FlightSearch.choose_airport.state:
        orig_iata   = data.get("origin_iata", "")
        origin_name = data.get("origin_name", "")
        metro = _get_metro(orig_iata) if orig_iata else None
        if metro:
            await callback.message.answer(
                f"Вы выбрали: <b>{origin_name}</b>\n\n"
                f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
                parse_mode="HTML",
                reply_markup=_airport_keyboard(metro, origin_name),
            )
        else:
            await callback.message.answer("Выберите аэропорт:", reply_markup=CANCEL_KB)

    elif current == FlightSearch.depart_date.state:
        existing = data.get("depart_date", "")
        hint = f"\n<i>Последний ввод: {existing}</i>" if existing else ""
        await callback.message.answer(
            f"Введите дату вылета в формате <code>ДД.ММ</code>{hint}\n<i>Пример: 10.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )

    elif current == FlightSearch.need_return.state:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, нужен",    callback_data="return_yes"),
             InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="return_no")],
        ])
        await callback.message.answer("Нужен ли обратный билет?", reply_markup=kb)

    elif current == FlightSearch.return_date.state:
        await callback.message.answer(
            "Введите дату возврата в формате <code>ДД.ММ</code>\n<i>Пример: 20.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )

    elif current == FlightSearch.flight_type.state:
        await ask_flight_type(callback.message, state)

    elif current in (FlightSearch.adults.state, FlightSearch.has_children.state,
                     FlightSearch.children.state, FlightSearch.infants.state):
        await ask_adults(callback.message, state)

    elif current == FlightSearch.confirm.state:
        await show_summary(callback.message, state)

    else:
        await callback.message.answer("Используйте кнопки внизу экрана.")
        return

    schedule_inactivity(callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    await callback.message.edit_text(
        "Начнём поиск билетов 👌\n\n"
        "<b>Напишите маршрут в формате: Город отправления - Город прибытия</b>\n\n"
        "<i>Пример: Москва - Сочи</i>\n\n"
        "Если ещё не решили, откуда или куда полетите, напишите слово «Везде».",
        parse_mode="HTML",
        reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.route)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# FSM: маршрут
# ════════════════════════════════════════════════════════════════

@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    origin, dest = validate_route(message.text)
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    if origin != "везде":
        orig_iata = get_iata(origin) or CITY_TO_IATA.get(_normalize_name(origin))
        if not orig_iata:
            # П.2: пробуем нечёткий поиск
            fuzzy_iata, fuzzy_name = fuzzy_get_iata(origin)
            if fuzzy_iata:
                await message.answer(
                    f"❓ Не нашёл «{origin}» — может быть, вы имели в виду <b>{fuzzy_name}</b>?\n"
                    f"Напишите маршрут ещё раз с правильным названием.\n\n"
                    f"<i>Пример: {fuzzy_name} - Сочи</i>",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            else:
                await message.answer(
                    f"❌ Не знаю город отправления: <b>{origin}</b>\n"
                    f"Проверьте написание и попробуйте ещё раз.",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            return
        origin_name = get_city_name(orig_iata) or IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = origin_name = None

    if dest != "везде":
        dest_iata = get_iata(dest) or CITY_TO_IATA.get(_normalize_name(dest))
        if not dest_iata:
            # П.2: нечёткий поиск
            fuzzy_iata, fuzzy_name = fuzzy_get_iata(dest)
            if fuzzy_iata:
                await message.answer(
                    f"❓ Не нашёл «{dest}» — может быть, вы имели в виду <b>{fuzzy_name}</b>?\n"
                    f"Напишите маршрут ещё раз с правильным названием.\n\n"
                    f"<i>Пример: Москва - {fuzzy_name}</i>",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            else:
                await message.answer(
                    f"❌ Не знаю город прибытия: <b>{dest}</b>\n"
                    f"Проверьте написание и попробуйте ещё раз.",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            return
        dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest.capitalize())
    else:
        dest_iata = dest_name = None

    if origin == "везде" and dest == "везде":
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\nУкажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB,
        )
        return

    if orig_iata and dest_iata and orig_iata == dest_iata:
        await message.answer(
            "❌ Город вылета и прибытия не могут совпадать.\nПожалуйста, выберите разные города.",
            reply_markup=CANCEL_KB,
        )
        return

    if origin == "везде": origin_name = "Везде"
    if dest   == "везде": dest_name   = "Везде"

    await state.update_data(
        origin=origin, origin_iata=orig_iata,
        dest=dest,     dest_iata=dest_iata,
        origin_name=origin_name, dest_name=dest_name,
    )

    data = await state.get_data()
    cancel_inactivity(message.chat.id)

    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        if orig_iata and _has_multi_airports(orig_iata):
            metro = _get_metro(orig_iata)
            await state.update_data(_edit_mode=True, origin_airports=None, origin_airport_label=None)
            await message.answer(
                f"Вы выбрали: <b>{origin_name}</b>\n\n"
                f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
                parse_mode="HTML",
                reply_markup=_airport_keyboard(metro, origin_name),
            )
            await state.set_state(FlightSearch.choose_airport)
            return
        await show_summary(message, state)
        return

    # Мульти-аэропорт
    if orig_iata and _has_multi_airports(orig_iata):
        metro = _get_metro(orig_iata)
        await state.update_data(origin_airports=None, origin_airport_label=None)
        await message.answer(
            f"Вы выбрали: <b>{origin_name}</b>\n\n"
            f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
            parse_mode="HTML",
            reply_markup=_airport_keyboard(metro, origin_name),
        )
        await state.set_state(FlightSearch.choose_airport)
        schedule_inactivity(message.chat.id, message.from_user.id)
        return

    await message.answer(
        "Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: 10.03</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.depart_date)
    schedule_inactivity(message.chat.id, message.from_user.id)


# ════════════════════════════════════════════════════════════════
# FSM: выбор аэропорта
# ════════════════════════════════════════════════════════════════

@router.callback_query(FlightSearch.choose_airport, F.data.startswith("ap_pick_"))
async def process_airport_pick(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    ap_iata = callback.data.replace("ap_pick_", "")
    metro   = _get_metro(ap_iata) or ap_iata
    ap_label = next(
        (lbl for code, lbl in MULTI_AIRPORT_CITIES.get(metro, []) if code == ap_iata),
        ap_iata,
    )
    await state.update_data(origin_iata=ap_iata, origin_airports=[ap_iata], origin_airport_label=ap_label)
    await callback.answer(f"✈️ {ap_label}")
    await _after_airport_pick(callback, state)


@router.callback_query(FlightSearch.choose_airport, F.data.startswith("ap_any_"))
async def process_airport_any(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    metro_iata = callback.data.replace("ap_any_", "")
    all_iatas  = [ap for ap, _ in MULTI_AIRPORT_CITIES.get(metro_iata, [])]
    await state.update_data(
        origin_iata=metro_iata,
        origin_airports=all_iatas,
        origin_airport_label="Любой аэропорт",
    )
    await callback.answer("🔀 Буду искать по всем аэропортам")
    await _after_airport_pick(callback, state)


async def _after_airport_pick(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(callback.message, state)
        return
    await callback.message.edit_text(
        "Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: 10.03</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.depart_date)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)


# ════════════════════════════════════════════════════════════════
# FSM: дата вылета
# ════════════════════════════════════════════════════════════════

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n<i>Пример: 10.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    cancel_inactivity(message.chat.id)
    await state.update_data(depart_date=message.text)
    data = await state.get_data()
    is_everywhere = data["origin"] == "везде" or data["dest"] == "везде"

    if data.get("_edit_mode"):
        if is_everywhere:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
            return
        if data.get("need_return"):
            await message.answer(
                "✏️ Введите новую дату обратного рейса в формате <code>ДД.ММ</code>\n<i>Пример: 20.03</i>",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
            await state.set_state(FlightSearch.return_date)
        else:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
        return

    if is_everywhere:
        await state.update_data(need_return=False, return_date=None)
        await ask_flight_type(message, state)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен",    callback_data="return_yes"),
         InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="return_no")],
    ])
    await message.answer("Нужен ли обратный билет?", reply_markup=kb)
    await state.set_state(FlightSearch.need_return)


# ════════════════════════════════════════════════════════════════
# FSM: обратный билет
# ════════════════════════════════════════════════════════════════

@router.callback_query(FlightSearch.need_return, F.data.in_({"return_yes", "return_no"}))
async def process_need_return(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    need_return = callback.data == "return_yes"
    await state.update_data(need_return=need_return)
    if need_return:
        await callback.message.edit_text(
            "Введите дату возврата в формате <code>ДД.ММ</code>\n<i>Пример: 15.03</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await state.update_data(return_date=None)
        await ask_flight_type(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.need_return, F.text == "В начало")
async def need_return_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# FSM: дата возврата
# ════════════════════════════════════════════════════════════════

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n<i>Пример: 15.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    data = await state.get_data()
    norm_depart = normalize_date(data.get("depart_date", ""))
    norm_return = normalize_date(message.text)
    if norm_return and norm_depart and norm_return <= norm_depart:
        await message.answer(
            "❌ Дата возврата не может быть раньше или равна дате вылета.\n"
            "Укажите правильную дату возврата.",
            reply_markup=CANCEL_KB,
        )
        return

    cancel_inactivity(message.chat.id)
    await state.update_data(return_date=message.text)

    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(message, state)
        return
    await ask_flight_type(message, state)


# ════════════════════════════════════════════════════════════════
# FSM: тип рейса
# ════════════════════════════════════════════════════════════════

async def ask_flight_type(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Прямые",       callback_data="ft_direct"),
         InlineKeyboardButton(text="🔀 С пересадкой", callback_data="ft_transfer")],
        [InlineKeyboardButton(text="🔍 Все варианты", callback_data="ft_all")],
    ])
    await message.answer("Какие рейсы показывать?", reply_markup=kb)
    await state.set_state(FlightSearch.flight_type)


@router.callback_query(FlightSearch.flight_type, F.data.in_({"ft_direct", "ft_transfer", "ft_all"}))
async def process_flight_type(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    code_map = {"ft_direct": "direct", "ft_transfer": "transfer", "ft_all": "all"}
    await state.update_data(flight_type=code_map[callback.data])
    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(callback.message, state)
    else:
        await ask_adults(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.flight_type, F.text == "В начало")
async def flight_type_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# FSM: пассажиры
# ════════════════════════════════════════════════════════════════

async def ask_adults(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"adults_{i}") for i in range(1, 5)],
        [InlineKeyboardButton(text=str(i), callback_data=f"adults_{i}") for i in range(5, 9)],
        [InlineKeyboardButton(text="9",    callback_data="adults_9")],
    ])
    await message.answer("Сколько взрослых пассажиров (от 12 лет)?", reply_markup=kb)
    await state.set_state(FlightSearch.adults)


@router.callback_query(FlightSearch.adults, F.data.regexp(r"^adults_[1-9]$"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)
    if adults == 9:
        await state.update_data(children=0, infants=0, passenger_desc="9 взр.", passenger_code="9")
        await show_summary(callback.message, state)
    else:
        await ask_has_children(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.adults, F.text == "В начало")
async def adults_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_has_children(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👶 Да", callback_data="hc_yes"),
         InlineKeyboardButton(text="✅ Нет", callback_data="hc_no")],
    ])
    await message.answer("С вами летят дети?", reply_markup=kb)
    await state.set_state(FlightSearch.has_children)


@router.callback_query(FlightSearch.has_children, F.data.in_({"hc_yes", "hc_no"}))
async def process_has_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    if callback.data == "hc_yes":
        await ask_children(callback.message, state)
    else:
        data = await state.get_data()
        adults = data["adults"]
        pd = f"{adults} взр."
        await state.update_data(children=0, infants=0, passenger_desc=pd, passenger_code=str(adults))
        await show_summary(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.has_children, F.text == "В начало")
async def has_children_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_children(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    max_ch = 9 - adults
    nums = list(range(0, max_ch + 1))
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"ch_{n}") for n in nums[i:i+5]]
            for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        "Сколько детей (от 2 до 11 лет)?\nЕсли у вас младенцы, укажете дальше.",
        reply_markup=kb,
    )
    await state.set_state(FlightSearch.children)


@router.callback_query(FlightSearch.children, F.data.regexp(r"^ch_\d+$"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    adults = data["adults"]
    children = int(callback.data.split("_")[1])
    if children < 0 or children > 9 - adults:
        await callback.answer()
        return
    await state.update_data(children=children)
    if 9 - adults - children == 0:
        pd = f"{adults} взр." + (f", {children} дет." if children else "")
        await state.update_data(infants=0, passenger_desc=pd,
                                passenger_code=build_passenger_code(adults, children, 0))
        await show_summary(callback.message, state)
    else:
        await ask_infants(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.children, F.text == "В начало")
async def children_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_infants(message: Message, state: FSMContext):
    data = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    max_inf  = min(adults, 9 - adults - children)
    nums = list(range(0, max_inf + 1))
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"inf_{n}") for n in nums[i:i+5]]
            for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Сколько младенцев? (младше 2 лет без места)", reply_markup=kb)
    await state.set_state(FlightSearch.infants)


@router.callback_query(FlightSearch.infants, F.data.regexp(r"^inf_\d+$"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    infants  = int(callback.data.split("_")[1])
    if infants < 0 or infants > min(adults, 9 - adults - children):
        await callback.answer()
        return
    await state.update_data(infants=infants)
    await show_summary(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.infants, F.text == "В начало")
async def infants_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# Экран подтверждения (summary)
# ════════════════════════════════════════════════════════════════

async def show_summary(message, state: FSMContext):
    await state.update_data(_edit_mode=False)
    data = await state.get_data()
    adults   = data.get("adults", 1)
    children = data.get("children", 0)
    infants  = data.get("infants", 0)

    passenger_code = build_passenger_code(adults, children, infants)
    passenger_desc = f"{adults} взр."
    if children: passenger_desc += f", {children} дет."
    if infants:  passenger_desc += f", {infants} мл."

    await state.update_data(passenger_code=passenger_code, passenger_desc=passenger_desc)
    data = await state.get_data()

    summary = "Проверьте даты и данные:\n\n" + build_choices_summary(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",   callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",       callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",  callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",  callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",   callback_data="main_menu")],
    ])

    await message.answer(summary, parse_mode="HTML")
    await message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

    chat_id = message.chat.id if hasattr(message, "chat") else message.from_user.id
    schedule_inactivity(chat_id, message.from_user.id)


# ════════════════════════════════════════════════════════════════
# Редактирование из summary
# ════════════════════════════════════════════════════════════════

async def _do_edit_action(callback: CallbackQuery, state: FSMContext, action: str):
    if action == "route":
        await state.update_data(_edit_mode=True, origin_airports=None, origin_airport_label=None)
        await callback.message.edit_text(
            "✏️ Введите новый маршрут:\n<b>Город вылета - Город прибытия</b>\n\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.route)

    elif action == "dates":
        await state.update_data(_edit_mode=True)
        data = await state.get_data()
        hint = "\n(затем введёте дату обратного рейса)" if data.get("need_return") else ""
        await callback.message.edit_text(
            f"✏️ Введите новую дату вылета в формате <code>ДД.ММ</code>{hint}\n<i>Пример: 15.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.depart_date)

    elif action == "flight_type":
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_flight_type(callback.message, state)

    elif action == "passengers":
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_adults(callback.message, state)


@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    action = callback.data[len("edit_"):]
    await _do_edit_action(callback, state, action)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Подтверждение и запуск поиска
# ════════════════════════════════════════════════════════════════

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)
    data = await state.get_data()
    logger.info(f"[confirm_search] user={callback.from_user.id} маршрут={data.get('origin_iata')}→{data.get('dest_iata')}")
    await callback.message.edit_text("⏳ Ищу билеты...")
    async with _SEARCH_SEMAPHORE:
        await _do_confirm_search(callback, state, data)


async def _do_confirm_search(callback: CallbackQuery, state: FSMContext, data: dict):
    """Основная логика поиска. Вызывается внутри семафора."""
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere   = data["dest"]   == "везде"
    flight_type    = data.get("flight_type", "all")
    direct_only    = flight_type == "direct"
    transfers_only = flight_type == "transfer"

    # ── Везде ──────────────────────────────────────────────────
    if is_origin_everywhere and not is_dest_everywhere:
        all_flights = await search_origin_everywhere(
            dest_iata=data["dest_iata"], depart_date=data["depart_date"],
            flight_type=flight_type,
        )
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, "origin_everywhere")
        if success:
            await state.clear()
        return

    if not is_origin_everywhere and is_dest_everywhere:
        all_flights = await search_destination_everywhere(
            origin_iata=data["origin_iata"], depart_date=data["depart_date"],
            flight_type=flight_type,
        )
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, "destination_everywhere")
        if success:
            await state.clear()
        return

    # ── Обычный поиск ──────────────────────────────────────────
    origins      = data.get("origin_airports") or [data["origin_iata"]]
    destinations = [data["dest_iata"]]
    all_flights  = []

    pax_code = data.get("passenger_code", "1")
    try:
        rt_adults   = int(pax_code[0])
        rt_children = int(pax_code[1]) if len(pax_code) > 1 else 0
        rt_infants  = int(pax_code[2]) if len(pax_code) > 2 else 0
    except (ValueError, IndexError):
        rt_adults, rt_children, rt_infants = 1, 0, 0

    # Прогресс-анимация
    progress_msg = await callback.message.edit_text("⏳ <b>Ищу билеты...</b>", parse_mode="HTML")

    async def _update_progress():
        await asyncio.sleep(10)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Запрашиваю актуальные цены...</b>\n<i>Получаю данные от авиакомпаний</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await asyncio.sleep(20)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Почти готово...</b>\n<i>Сравниваю предложения</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    progress_task = asyncio.create_task(_update_progress())

    try:
        for orig in origins:
            for dest in destinations:
                if orig == dest:
                    continue
                flights = await search_flights_realtime(
                    origin=orig, destination=dest,
                    depart_date=normalize_date(data["depart_date"]),
                    return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
                    adults=rt_adults, children=rt_children, infants=rt_infants,
                )
                if direct_only:
                    flights = [f for f in flights if f.get("transfers", 999) == 0]
                elif transfers_only:
                    flights = [f for f in flights if f.get("transfers", 0) > 0]
                for f in flights:
                    f["origin"] = orig
                    f["destination"] = dest
                all_flights.extend(flights)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    logger.info(f"🔍 [Search] {len(all_flights)} рейсов от {set(f.get('_source') for f in all_flights)}")

    # ── Нет прямых → предлагаем с пересадками ──────────────────
    if direct_only and not all_flights:
        all_any = []
        for orig in origins:
            for dest in destinations:
                if orig == dest:
                    continue
                all_any.extend(await search_flights_realtime(
                    origin=orig, destination=dest,
                    depart_date=normalize_date(data["depart_date"]),
                    return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
                    adults=rt_adults, children=rt_children, infants=rt_infants,
                ))

        if all_any:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Показать рейсы с пересадками",
                                      callback_data="retry_with_transfers")],
                [InlineKeyboardButton(text="✏️ Изменить параметры", callback_data="back_to_summary")],
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
            ])
            await callback.message.edit_text(
                "😔 <b>Прямых рейсов на эти даты не найдено.</b>\n\nЕсть варианты с пересадками — они часто дешевле!",
                parse_mode="HTML", reply_markup=kb,
            )
        else:
            await _show_no_flights(callback, data, origins, destinations, pax_code)
        return

    # ── Вообще нет рейсов ───────────────────────────────────────
    if not all_flights:
        await _show_no_flights(callback, data, origins, destinations, pax_code)
        await state.clear()
        return

    # ── Сохраняем кэш и показываем результат ───────────────────
    cache_id       = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    display_return = format_user_date(data["return_date"]) if data.get("return_date") else None

    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "origin": data.get("origin", ""), "origin_iata": data.get("origin_iata", ""),
        "origin_name": data.get("origin_name", ""),
        "dest": data.get("dest", ""),     "dest_iata": data["dest_iata"],
        "dest_name": data.get("dest_name", ""),
        "depart_date": data["depart_date"],       "return_date": data.get("return_date"),
        "need_return": data.get("need_return", False),
        "display_depart": display_depart,         "display_return": display_return,
        "original_depart": data["depart_date"],   "original_return": data.get("return_date"),
        "passenger_desc": data["passenger_desc"], "passengers_code": data["passenger_code"],
        "passenger_code": data["passenger_code"],
        "adults": data.get("adults", 1), "children": data.get("children", 0),
        "infants": data.get("infants", 0),
        "origin_everywhere": False, "dest_everywhere": False,
        "flight_type": flight_type,
    })

    top_flight   = find_cheapest_flight_on_exact_date(all_flights, data["depart_date"], data.get("return_date"))
    price        = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata  = top_flight["origin"]
    dest_iata    = top_flight.get("destination") or data["dest_iata"]
    origin_name  = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name    = IATA_TO_CITY.get(dest_iata, dest_iata)
    duration     = _format_duration(top_flight.get("duration", 0))
    transfers    = top_flight.get("transfers", 0)
    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport   = AIRPORT_NAMES.get(dest_iata, dest_iata)

    if transfers == 0:   transfer_text = "✈️ Прямой рейс"
    elif transfers == 1: transfer_text = "✈️ 1 пересадка"
    else:                transfer_text = f"✈️ {transfers} пересадки"

    price_per_pax = int(float(price)) if price != "?" else 0
    passengers_code = data.get("passenger_code", "1")
    try:
        num_adults = int(passengers_code[0])
    except (IndexError, ValueError):
        num_adults = 1
    estimated_total = price_per_pax * num_adults if price != "?" else "?"

    text = "✅ <b>Самый дешёвый вариант</b>\n"
    if price != "?":
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_pax} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерно за {num_adults} взрослых:</b> ~{estimated_total} ₽"
    else:
        text += f"\n💰 <b>Цена:</b> уточните на Aviasales"

    if data.get("children", 0) > 0 or data.get("infants", 0) > 0:
        text += "\n<i>(стоимость для детей/младенцев может рассчитываться по-другому)</i>"

    text += (
        f"\n\n🛫 <b>Рейс:</b> {origin_name} → {dest_name}"
        f"\n📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})"
        f"\n📅 <b>Туда:</b> {display_depart}"
    )
    if data.get("need_return") and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"
    text += f"\n⏱️ <b>Продолжительность:</b> {duration}\n{transfer_text}"

    airline       = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_display = AIRLINE_NAMES.get(airline, airline)
        flight_display  = f"{airline_display} {flight_number}".strip() if flight_number else airline_display
        text += f"\n✈️ <b>Авиакомпания:</b> {flight_display}"

    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight, origin=origin_iata, dest=dest_iata,
            depart_date=data["depart_date"], passengers_code=passengers_code,
            return_date=data["return_date"] if data.get("need_return") else None,
        )
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    fallback_link = generate_booking_link(
        flight=top_flight, origin=origin_iata, dest=dest_iata,
        depart_date=data["depart_date"], passengers_code=passengers_code,
        return_date=data["return_date"] if data.get("need_return") else None,
    )
    if not fallback_link.startswith(("http://", "https://")):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    booking_link  = await convert_to_partner_link(booking_link)
    fallback_link = await convert_to_partner_link(fallback_link)

    kb_buttons = []
    if booking_link:
        kb_buttons.append([InlineKeyboardButton(text=f"✈️ Посмотреть детали за {price} ₽", url=booking_link)])
    kb_buttons.append([InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)])
    kb_buttons.append([InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")])
    kb_buttons.append([InlineKeyboardButton(text="✏️ Изменить данные", callback_data=f"edit_from_results_{cache_id}")])

    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-1, [
            InlineKeyboardButton(text=f"🚖 Трансфер в {dest_name}", url=transfer_link)
        ])

    kb_buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

    # Умное напоминание — предложим вау-цены через 15 минут если ещё не подписаны
    asyncio.create_task(
        remind_after_search(callback.message.chat.id, callback.from_user.id, delay_min=15)
    )


async def _show_no_flights(callback: CallbackQuery, data: dict,
                            origins: list, destinations: list, pax_code: str):
    """Показать экран 'билеты не найдены' со ссылкой на Aviasales."""
    origin_iata = origins[0] if origins else data.get("origin_iata", "MOW")
    d1 = format_avia_link_date(data["depart_date"])
    d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
    dest_iata = destinations[0] if destinations else data.get("dest_iata", "")
    route        = f"{origin_iata}{d1}{dest_iata}{d2}{pax_code}"
    partner_link = await convert_to_partner_link(f"https://www.aviasales.ru/search/{route}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поискать на Aviasales", url=partner_link)],
        [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="back_to_summary")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.edit_text(
        "😔 <b>Билеты не найдены.</b>\n\nПопробуйте изменить даты или маршрут.",
        parse_mode="HTML", reply_markup=kb,
    )


# ════════════════════════════════════════════════════════════════
# Callback-хендлеры результатов
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "retry_with_transfers")
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    if not data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели. Выполните новый поиск.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")]
            ]),
        )
        await callback.answer()
        return
    await state.update_data(flight_type="all")
    await confirm_search(callback, state)
    await callback.answer()


# Обратная совместимость
@router.callback_query(F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers_legacy(callback: CallbackQuery, state: FSMContext):
    await retry_with_transfers(callback, state)


@router.callback_query(F.data == "back_to_summary")
async def back_to_summary(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    if not data or "depart_date" not in data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Новый поиск", callback_data="start_search")]
            ]),
        )
        await callback.answer()
        return
    summary = "Проверьте даты и данные:\n\n" + build_choices_summary(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",    callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",        callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",   callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",   callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
    ])
    await callback.message.edit_text(summary, parse_mode="HTML")
    await callback.message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_from_results_"))
async def edit_from_results(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    cache_id = callback.data.replace("edit_from_results_", "")
    cached   = await redis_client.get_search_cache(cache_id)
    if not cached:
        await callback.answer("Данные устарели, начните новый поиск", show_alert=True)
        return

    cached.pop("flights", None)
    fsm_data = {
        "origin":         cached.get("origin", ""),
        "origin_iata":    cached.get("origin_iata", ""),
        "origin_name":    cached.get("origin_name", ""),
        "dest":           cached.get("dest", ""),
        "dest_iata":      cached.get("dest_iata", ""),
        "dest_name":      cached.get("dest_name", ""),
        "depart_date":    cached.get("depart_date") or cached.get("original_depart", ""),
        "return_date":    cached.get("return_date") or cached.get("original_return"),
        "need_return":    cached.get("need_return", False),
        "flight_type":    cached.get("flight_type", "all"),
        "adults":         cached.get("adults", 1),
        "children":       cached.get("children", 0),
        "infants":        cached.get("infants", 0),
        "passenger_code": cached.get("passenger_code") or cached.get("passengers_code", "1"),
        "passenger_desc": cached.get("passenger_desc", "1 взр."),
        "_edit_mode":     False,
    }
    await state.update_data(**fsm_data)
    await state.set_state(FlightSearch.confirm)

    summary = "Проверьте даты и данные:\n\n" + build_choices_summary(fsm_data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",    callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",        callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",   callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",   callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
    ])
    await callback.message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Слежение за ценой
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    parts = callback.data.split("_")

    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        is_origin_everywhere = data.get("origin_everywhere", False)
        is_dest_everywhere   = data.get("dest_everywhere", False)
        flights = data["flights"]
        if is_dest_everywhere:
            origin, dest = flights[0]["origin"], None
        elif is_origin_everywhere:
            origin = None
            dest   = data.get("dest_iata") or flights[0].get("destination")
        else:
            origin = flights[0]["origin"]
            dest   = data.get("dest_iata") or flights[0].get("destination")
        min_flight  = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
        price       = min_flight.get("value") or min_flight.get("price")
        depart_date = data["original_depart"]
        return_date = data["original_return"]
    else:
        cache_id = parts[1]
        price    = int(parts[2])
        data     = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        top  = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        origin      = top["origin"]
        dest        = data.get("dest_iata") or top.get("destination")
        depart_date = data["original_depart"]
        return_date = data["original_return"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Любое изменение цены",    callback_data=f"set_threshold:0:{cache_id}:{price}")],
        [InlineKeyboardButton(text="🔔 Изменение на сотни ₽",    callback_data=f"set_threshold:100:{cache_id}:{price}")],
        [InlineKeyboardButton(text="🔔 Изменение на тысячи ₽",   callback_data=f"set_threshold:1000:{cache_id}:{price}")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.answer("🔔 <b>Выберите условия уведомлений</b>", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("set_threshold:"))
async def handle_set_threshold(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    _, threshold_str, cache_id, price_str = callback.data.split(":", 3)
    threshold = int(threshold_str)
    price     = int(price_str)

    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return

    top    = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
    origin = top["origin"]
    dest   = data.get("dest_iata") or top.get("destination")

    await redis_client.save_price_watch(
        user_id=callback.from_user.id,
        origin=origin if not data.get("origin_everywhere") else None,
        dest=dest     if not data.get("dest_everywhere")   else None,
        depart_date=data["original_depart"],
        return_date=data["original_return"],
        current_price=price,
        passengers=data.get("passenger_code", "1"),
        threshold=threshold,
    )

    origin_name = IATA_TO_CITY.get(origin, origin)
    dest_name   = IATA_TO_CITY.get(dest, dest)
    condition   = {0: "любом изменении", 100: "изменении на сотни ₽", 1000: "изменении на тысячи ₽"}.get(threshold, "изменении цены")

    response = (
        f"✅ <b>Отлично! Я буду следить за ценами</b>\n"
        f"📲 Пришлю уведомление, если цена изменится!\n"
        f"📍 Маршрут: {origin_name} → {dest_name}\n"
        f"📅 Вылет: {data['display_depart']}\n"
    )
    if data.get("display_return"):
        response += f"📅 Возврат: {data['display_return']}\n"
    response += (
        f"💰 Текущая цена: {price} ₽\n"
        f"🔔 Уведомлять при: {condition}\n"
    )
    await callback.message.edit_text(
        response, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unwatch_"))
async def handle_unwatch(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    key     = callback.data.split("unwatch_")[1]
    user_id = callback.from_user.id
    if f":{user_id}:" not in key:
        await callback.answer("❌ Это не ваше отслеживание!", show_alert=True)
        return
    await redis_client.remove_watch(user_id, key)
    await callback.message.edit_text(
        "✅ Отслеживание цены остановлено.\nБольше не буду присылать уведомления по этому маршруту.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ]),
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Трансфер
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("ask_transfer_"))
async def handle_ask_transfer(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    user_id = callback.from_user.id
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return
    airport_iata = context["airport_iata"]
    airport_name = AIRPORT_NAMES.get(airport_iata, airport_iata)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, покажи варианты", callback_data=f"show_transfer_{user_id}")],
        [InlineKeyboardButton(text="❌ Нет, спасибо",        callback_data=f"decline_transfer_{user_id}")],
        [InlineKeyboardButton(text="↩️ В начало",            callback_data="main_menu")],
    ])
    await callback.message.answer(
        f"🚖 <b>Нужен трансфер из аэропорта {airport_name}?</b>\n"
        "Я могу найти для вас варианты трансфера по лучшим ценам.\nПоказать предложения?",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("decline_transfer_"))
async def handle_decline_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    transfer_context.pop(user_id, None)
    if redis_client.client:
        await redis_client.client.setex(f"declined_transfer:{user_id}", 86400 * 7, "1")
    await callback.message.edit_text(
        "Хорошо! Если передумаете — просто выполните новый поиск билетов. ✈️",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("show_transfer_"))
async def handle_show_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    if redis_client.client:
        if await redis_client.client.get(f"declined_transfer:{user_id}"):
            await callback.answer(
                "Вы недавно отказались от трансферов. Предложения снова появятся через несколько дней.",
                show_alert=True,
            )
            return
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return

    await callback.message.edit_text("Ищу варианты трансфера... 🚖")
    transfers = await search_transfers(airport_iata=context["airport_iata"], transfer_date=context["transfer_date"], adults=1)

    if not transfers:
        await callback.message.edit_text(
            "К сожалению, трансферы для этого аэропорта временно недоступны. 😢\n"
            "Попробуйте позже или забронируйте на сайте напрямую.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
            ]),
        )
        return

    airport_name = AIRPORT_NAMES.get(context["airport_iata"], context["airport_iata"])
    msg = (
        f"🚀 <b>Варианты трансфера {context['depart_date']}</b>\n"
        f"📍 <b>{airport_name}</b> → центр города\n"
    )
    buttons = []
    for i, t in enumerate(transfers[:3], 1):
        price    = t.get("price", 0)
        vehicle  = t.get("vehicle", "Economy")
        duration = t.get("duration_minutes", 0)
        msg += f"\n<b>{i}. {vehicle}</b>\n💰 {price} ₽\n⏱️ ~{duration} мин в пути"
        tlink = generate_transfer_link(
            transfer_id=str(t.get("id", "")),
            marker=os.getenv("TRAFFIC_SOURCE", ""),
            sub_id=f"telegram_{user_id}",
        )
        buttons.append([InlineKeyboardButton(text=f"🚀 Вариант {i}: {price} ₽", url=tlink)])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Любой текст вне FSM → тихий ручной поиск
# ════════════════════════════════════════════════════════════════

@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    text = message.text or ""

    if text.startswith("/"):
        return

    # Чужие FSM — не трогаем
    if current_state and (
        current_state.startswith("FlyStackTrack")
        or current_state.startswith("HotDealsSub")
    ):
        return

    # Внутри FlightSearch — предупреждаем
    if current_state and current_state.startswith("FlightSearch"):
        logger.warning(f"⚠️ [Start] Состояние {current_state}: '{text[:30]}'")
        await message.answer(
            "Пожалуйста, завершите текущий поиск или отмените его через кнопку ↩️ В начало",
            reply_markup=CANCEL_KB,
        )
        return

    # Вне FSM — пробуем тихий ручной поиск
    await handle_flight_request(message)