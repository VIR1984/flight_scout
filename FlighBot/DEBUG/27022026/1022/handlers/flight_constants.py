# handlers/flight_constants.py
"""
Общие константы для обработчиков полётов.
Вынесены сюда чтобы:
  - не дублировать между start.py и quick_search.py
  - избежать циклических импортов
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ── Клавиатуры ────────────────────────────────────────────────────────────────

CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
])

MAIN_MENU_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✈️ Найти билеты",       callback_data="start_search")],
    [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
])

# ── Аэропорты ─────────────────────────────────────────────────────────────────

# Города с несколькими аэропортами: metro-IATA → [(iata_аэропорта, "Название (IATA)")]
MULTI_AIRPORT_CITIES: dict = {
    "MOW": [
        ("SVO", "Шереметьево (SVO)"),
        ("DME", "Домодедово (DME)"),
        ("VKO", "Внуково (VKO)"),
        ("ZIA", "Жуковский (ZIA)"),
    ],
    "IST": [
        ("IST", "Стамбул Новый (IST)"),
        ("SAW", "Сабиха Гёкчен (SAW)"),
    ],
    "PAR": [
        ("CDG", "Шарль-де-Голль (CDG)"),
        ("ORY", "Орли (ORY)"),
    ],
    "LON": [
        ("LHR", "Хитроу (LHR)"),
        ("LGW", "Гатвик (LGW)"),
        ("STN", "Станстед (STN)"),
    ],
    "MIL": [
        ("MXP", "Мальпенса (MXP)"),
        ("LIN", "Линате (LIN)"),
        ("BGY", "Бергамо (BGY)"),
    ],
    "BKK": [
        ("BKK", "Суварнабхуми (BKK)"),
        ("DMK", "Дон Мыанг (DMK)"),
    ],
    "TYO": [
        ("NRT", "Нарита (NRT)"),
        ("HND", "Ханеда (HND)"),
    ],
    "NYC": [
        ("JFK", "Кеннеди (JFK)"),
        ("EWR", "Ньюарк (EWR)"),
        ("LGA", "Ла Гуардия (LGA)"),
    ],
    "CHI": [
        ("ORD", "О'Хара (ORD)"),
        ("MDW", "Мидуэй (MDW)"),
    ],
}

# Обратный индекс: IATA аэропорта → metro-IATA
AIRPORT_TO_METRO: dict = {
    ap: metro
    for metro, aps in MULTI_AIRPORT_CITIES.items()
    for ap, _ in aps
}

# Читаемые названия аэропортов (для отображения в результатах)
AIRPORT_NAMES: dict = {
    "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково",  "ZIA": "Жуковский",
    "LED": "Пулково",     "AER": "Адлер",       "KZN": "Казань",   "OVB": "Новосибирск",
    "ROV": "Ростов",      "KUF": "Курумоч",     "UFA": "Уфа",      "CEK": "Челябинск",
    "TJM": "Тюмень",      "KJA": "Красноярск",  "OMS": "Омск",     "BAX": "Барнаул",
    "KRR": "Краснодар",   "GRV": "Грозный",     "MCX": "Махачкала","VOG": "Волгоград",
    "SVX": "Кольцово",    "IKT": "Иркутск",     "VVO": "Владивосток",
    "HKT": "Пхукет",      "BKK": "Суварнабхуми","DXB": "Дубай",
    "IST": "Стамбул",     "AYT": "Анталья",     "CDG": "Шарль-де-Голль",
}

# Аэропорты с доступным трансфером через GetTransfer
SUPPORTED_TRANSFER_AIRPORTS: set = {
    "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
    "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
    "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK",
}

# Авиакомпании: IATA-код → русское название
AIRLINE_NAMES: dict = {
    "SU": "Аэрофлот",
    "S7": "S7 Airlines",
    "DP": "Победа",
    "U6": "Уральские авиалинии",
    "FV": "Россия",
    "UT": "ЮТэйр",
    "N4": "Нордстар",
    "IK": "Победа",
    "TK": "Turkish Airlines",
    "EK": "Emirates",
    "FZ": "flydubai",
    "HY": "Uzbekistan Airways",
}