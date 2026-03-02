# handlers/country_search.py
"""
Логика выбора города когда пользователь вводит страну.
Показывает топ-4 города + "Ввести свой" + "Любой в стране".
Экспортирует: router, _ask_country_city (вызывается из flight_wizard)
"""
import aiohttp
from aiogram import Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.context import FSMContext

from utils.cities_loader import (
    get_iata, get_city_name, fuzzy_get_iata,
    CITY_TO_IATA, _normalize_name,
    COUNTRY_TOP_CITIES, COUNTRY_NAME_TO_ISO,
)
from utils.smart_reminder import schedule_inactivity
from handlers.flight_constants import CANCEL_KB
from handlers.flight_fsm import (
    FlightSearch, _get_metro, _has_multi_airports, _airport_keyboard,
)
from utils.logger import logger

router = Router()

# ════════════════════════════════════════════════════════════════
# Словарь стран: ISO → (Именительный, Предложный падеж)
# Предложный используется в кнопке "Любой город в ___"
# ════════════════════════════════════════════════════════════════
COUNTRY_NAMES_RU: dict[str, tuple[str, str]] = {
    "AE": ("ОАЭ",          "ОАЭ"),
    "TR": ("Турция",       "Турции"),
    "TH": ("Таиланд",      "Таиланде"),
    "CN": ("Китай",        "Китае"),
    "EG": ("Египет",       "Египте"),
    "GR": ("Греция",       "Греции"),
    "ES": ("Испания",      "Испании"),
    "IT": ("Италия",       "Италии"),
    "FR": ("Франция",      "Франции"),
    "DE": ("Германия",     "Германии"),
    "CZ": ("Чехия",        "Чехии"),
    "HU": ("Венгрия",      "Венгрии"),
    "AT": ("Австрия",      "Австрии"),
    "NL": ("Нидерланды",   "Нидерландах"),
    "PL": ("Польша",       "Польше"),
    "FI": ("Финляндия",    "Финляндии"),
    "SE": ("Швеция",       "Швеции"),
    "NO": ("Норвегия",     "Норвегии"),
    "DK": ("Дания",        "Дании"),
    "PT": ("Португалия",   "Португалии"),
    "SG": ("Сингапур",     "Сингапуре"),
    "MY": ("Малайзия",     "Малайзии"),
    "ID": ("Индонезия",    "Индонезии"),
    "VN": ("Вьетнам",      "Вьетнаме"),
    "IN": ("Индия",        "Индии"),
    "JP": ("Япония",       "Японии"),
    "KR": ("Южная Корея",  "Южной Корее"),
    "US": ("США",          "США"),
    "CA": ("Канада",       "Канаде"),
    "AU": ("Австралия",    "Австралии"),
    "MV": ("Мальдивы",     "Мальдивах"),
    "LK": ("Шри-Ланка",    "Шри-Ланке"),
    "CY": ("Кипр",         "Кипре"),
    "MT": ("Мальта",       "Мальте"),
    "HR": ("Хорватия",     "Хорватии"),
    "RS": ("Сербия",       "Сербии"),
    "GE": ("Грузия",       "Грузии"),
    "AM": ("Армения",      "Армении"),
    "AZ": ("Азербайджан",  "Азербайджане"),
    "KZ": ("Казахстан",    "Казахстане"),
    "UZ": ("Узбекистан",   "Узбекистане"),
    "BY": ("Беларусь",     "Беларуси"),
    "IL": ("Израиль",      "Израиле"),
    "JO": ("Иордания",     "Иордании"),
    "MA": ("Марокко",      "Марокко"),
    "TN": ("Тунис",        "Тунисе"),
    "MX": ("Мексика",      "Мексике"),
    "BR": ("Бразилия",     "Бразилии"),
    "TJ": ("Таджикистан",  "Таджикистане"),
    "KG": ("Кыргызстан",   "Кыргызстане"),
    "MN": ("Монголия",     "Монголии"),
    "QA": ("Катар",        "Катаре"),
    "BH": ("Бахрейн",      "Бахрейне"),
    "KW": ("Кувейт",       "Кувейте"),
    "OM": ("Оман",         "Омане"),
    "LB": ("Ливан",        "Ливане"),
    "RU": ("Россия",       "России"),
}

# ISO → (lat, lon) столицы для запроса погоды (open-meteo.com, без API-ключа)
_CAPITAL_COORDS: dict[str, tuple[float, float]] = {
    "AE": (25.20, 55.27),  "TR": (41.01, 28.98),  "TH": (13.75, 100.52),
    "CN": (39.91, 116.39), "EG": (30.06, 31.25),  "GR": (37.98, 23.73),
    "ES": (40.42, -3.70),  "IT": (41.90, 12.50),  "FR": (48.85,  2.35),
    "DE": (52.52, 13.40),  "CZ": (50.08, 14.44),  "HU": (47.50, 19.04),
    "AT": (48.21, 16.37),  "NL": (52.37,  4.90),  "PL": (52.23, 21.01),
    "FI": (60.17, 24.94),  "SE": (59.33, 18.07),  "NO": (59.91, 10.75),
    "DK": (55.68, 12.57),  "PT": (38.72, -9.14),  "SG": ( 1.35,103.82),
    "MY": ( 3.14,101.69),  "ID": (-6.21,106.85),  "VN": (21.03,105.85),
    "IN": (28.61, 77.21),  "JP": (35.69,139.69),  "KR": (37.57,126.98),
    "US": (38.91,-77.04),  "CA": (45.42,-75.69),  "AU": (-35.28,149.13),
    "MV": ( 4.18, 73.51),  "LK": ( 6.93, 79.85),  "CY": (35.17, 33.36),
    "MT": (35.90, 14.51),  "HR": (45.81, 15.98),  "RS": (44.80, 20.46),
    "GE": (41.69, 44.83),  "AM": (40.18, 44.51),  "AZ": (40.41, 49.87),
    "KZ": (51.18, 71.45),  "UZ": (41.30, 69.24),  "BY": (53.90, 27.57),
    "IL": (31.77, 35.22),  "JO": (31.95, 35.93),  "MA": (33.99, -6.85),
    "TN": (36.82, 10.17),  "MX": (19.43,-99.13),  "BR": (-15.78,-47.93),
    "TJ": (38.56, 68.77),  "KG": (42.87, 74.59),  "MN": (47.91,106.88),
    "QA": (25.29, 51.53),  "BH": (26.21, 50.59),  "KW": (29.37, 47.98),
    "OM": (23.59, 58.59),  "LB": (33.89, 35.50),  "RU": (55.75, 37.62),
}

# WMO weather code → эмодзи
_WMO_EMOJI: dict[int, str] = {
    0: "☀️", 1: "🌤️", 2: "⛅",  3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌦️", 55: "🌧️",
    61: "🌧️", 63: "🌧️", 65: "🌧️",
    71: "🌨️", 73: "🌨️", 75: "❄️",
    80: "🌦️", 81: "🌧️", 82: "⛈️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}


async def _fetch_weather(iso: str) -> str:
    """Возвращает '☀️ +28°C' для столицы страны или '' при ошибке/таймауте."""
    coords = _CAPITAL_COORDS.get(iso)
    if not coords:
        return ""
    lat, lon = coords
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weathercode&forecast_days=1"
    )
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=4)) as r:
                if r.status != 200:
                    return ""
                j    = await r.json()
                cur  = j.get("current", {})
                temp = cur.get("temperature_2m")
                code = int(cur.get("weathercode", 0))
                if temp is None:
                    return ""
                icon = _WMO_EMOJI.get(code, "🌡️")
                sign = "+" if temp >= 0 else ""
                return f"{icon} {sign}{round(temp)}°C"
    except Exception as exc:
        logger.debug(f"[weather] {iso}: {exc}")
        return ""


# ════════════════════════════════════════════════════════════════
# Выбор города из страны
# ════════════════════════════════════════════════════════════════

async def _ask_country_city(
    message: Message,
    state: FSMContext,
    country_name: str,
    cities: list,
    role: str,  # "origin" или "dest"
):
    """Показывает топ-города страны + 'Ввести свой' + 'Любой город' + 'Отменить'."""
    iso = COUNTRY_NAME_TO_ISO.get(country_name.lower().strip().replace("ё", "е"))
    all_country_iatas = COUNTRY_TOP_CITIES.get(iso, []) if iso else [c["iata"] for c in cities]

    # Правильное написание и склонение
    names         = COUNTRY_NAMES_RU.get(iso) if iso else None
    display_name  = names[0] if names else country_name.capitalize()
    prepositional = names[1] if names else country_name.capitalize()

    # Погода в столице (параллельно с рендером, таймаут 4с)
    weather = await _fetch_weather(iso) if iso else ""

    await state.update_data(
        _country_role=role,
        _country_name=display_name,
        _country_iatas=all_country_iatas,
    )
    await state.set_state(FlightSearch.choose_country_city)

    prompt       = "вылета" if role == "origin" else "назначения"
    weather_line = f"\n🌡️ Сейчас в столице: <b>{weather}</b>" if weather else ""
    text = (
        f"🌍 <b>{display_name}</b> — популярные города {prompt}:{weather_line}\n\n"
        "Выберите город или найдите самый дешёвый билет по всей стране."
    )

    buttons = []
    for city in cities:
        buttons.append([InlineKeyboardButton(
            text=city["name"],
            callback_data=f"cc_{role}_{city['iata']}",
        )])
    buttons.append([InlineKeyboardButton(
        text="✏️ Ввести свой город",
        callback_data=f"cc_{role}_custom",
    )])
    buttons.append([InlineKeyboardButton(
        text=f"🔍 Любой город в {prepositional}",
        callback_data=f"cc_{role}_any",
    )])
    buttons.append([InlineKeyboardButton(
        text="✖ Отменить поиск",
        callback_data="main_menu",
    )])

    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(FlightSearch.choose_country_city, F.data.startswith("cc_"))
async def process_country_city_pick(callback: CallbackQuery, state: FSMContext):
    """Пользователь нажал на один из городов страны."""
    parts = callback.data.split("_", 2)  # cc_{role}_{iata|custom|any}
    role  = parts[1]
    value = parts[2]

    if value == "custom":
        prompt = "отправления" if role == "origin" else "назначения"
        await callback.message.edit_text(
            f"Введите город {prompt}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
            ])
        )
        await state.update_data(_country_custom_role=role)
        await state.set_state(FlightSearch.choose_country_city)
        await callback.answer()
        return

    if value == "any":
        data          = await state.get_data()
        country_name  = data.get("_country_name", "стране")
        country_iatas = data.get("_country_iatas", [])

        await callback.answer()
        await callback.message.edit_text(
            f"🔍 Буду искать самый дешёвый рейс по всей <b>{country_name}</b>",
            parse_mode="HTML",
        )

        if role == "dest":
            await state.update_data(
                dest=f"везде_{country_name}",
                dest_iata=None,
                dest_name=country_name,
                _country_dest_iatas=country_iatas,
                _country_role=None, _country_custom_role=None,
            )
            await _finalize_route(callback.message, state)
        else:
            await state.update_data(
                origin=f"везде_{country_name}",
                origin_iata=None,
                origin_name=country_name,
                _country_origin_iatas=country_iatas,
                _country_role=None, _country_custom_role=None,
            )
            dest_val = data.get("dest", "")
            if not data.get("dest_iata") and dest_val != "везде":
                await callback.message.answer(
                    "Теперь введите <b>город назначения</b>:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                    ])
                )
                await state.update_data(_country_custom_role="dest")
            else:
                await _finalize_route(callback.message, state)
        return

    # Конкретный город выбран
    city_name = get_city_name(value) or value
    await callback.answer()
    await callback.message.edit_text(
        f"{'Город вылета' if role == 'origin' else 'Город назначения'}: <b>{city_name}</b>",
        parse_mode="HTML",
    )

    data = await state.get_data()

    if role == "origin":
        await state.update_data(
            origin=city_name, origin_iata=value, origin_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        dest_val = data.get("dest", "")
        if not data.get("dest_iata") and dest_val != "везде":
            await callback.message.answer(
                "Отлично! Теперь введите <b>город назначения</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
            await state.set_state(FlightSearch.choose_country_city)
            await state.update_data(_country_custom_role="dest")
        else:
            await _finalize_route(callback.message, state)
    else:
        await state.update_data(
            dest=city_name, dest_iata=value, dest_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        await _finalize_route(callback.message, state)


@router.message(FlightSearch.choose_country_city)
async def process_country_city_text(message: Message, state: FSMContext):
    """Пользователь написал свой город вместо нажатия кнопки."""
    data  = await state.get_data()
    role  = data.get("_country_custom_role") or data.get("_country_role", "dest")
    city  = message.text.strip()

    iata = get_iata(city) or CITY_TO_IATA.get(_normalize_name(city))
    if not iata:
        fuzzy_iata, fuzzy_name = fuzzy_get_iata(city)
        if fuzzy_iata:
            await message.answer(
                f"❓ Не нашёл «{city}» — вы имели в виду <b>{fuzzy_name}</b>?\n"
                "Напишите название ещё раз.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
        else:
            await message.answer(
                f"❌ Город «{city}» не найден. Проверьте написание и попробуйте ещё раз.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
        return

    city_name = get_city_name(iata) or city.capitalize()

    if role == "origin":
        await state.update_data(
            origin=city_name, origin_iata=iata, origin_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        dest_val = data.get("dest", "")
        if not data.get("dest_iata") and dest_val != "везде":
            await message.answer(
                f"✅ Город вылета: <b>{city_name}</b>\n\nТеперь введите <b>город назначения</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
            await state.update_data(_country_custom_role="dest")
        else:
            await _finalize_route(message, state)
    else:
        await state.update_data(
            dest=city_name, dest_iata=iata, dest_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        await _finalize_route(message, state)


async def _finalize_route(target, state: FSMContext):
    """После выбора обоих городов — проверки и переход к следующему шагу."""
    data        = await state.get_data()
    orig_iata   = data.get("origin_iata")
    dest_iata   = data.get("dest_iata")
    origin_name = data.get("origin_name", "")
    dest_name   = data.get("dest_name", "")

    msg = target if isinstance(target, Message) else target

    if orig_iata and dest_iata and orig_iata == dest_iata:
        await msg.answer(
            "❌ Город вылета и прибытия не могут совпадать. Выберите разные города.",
            reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.route)
        return

    await state.set_state(FlightSearch.route)

    if orig_iata and _has_multi_airports(orig_iata):
        metro = _get_metro(orig_iata) or orig_iata
        await state.update_data(origin_iata=metro)
        await state.set_state(FlightSearch.choose_airport)
        kb = _airport_keyboard(metro)
        await msg.answer(
            f"Вы выбрали: <b>{origin_name}</b>\n\n"
            f"Из {origin_name} летят из нескольких аэропортов — выберите нужный:",
            parse_mode="HTML", reply_markup=kb,
        )
    else:
        await state.set_state(FlightSearch.depart_date)
        await msg.answer(
            "Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: 10.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        from utils.inactivity import schedule_inactivity
        schedule_inactivity(msg.chat.id, msg.from_user.id if hasattr(msg, "from_user") else 0)