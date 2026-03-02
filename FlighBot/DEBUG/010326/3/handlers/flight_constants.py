# handlers/flight_constants.py
"""
Общие константы для обработчиков полётов.
"""

from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)

# ── Нижняя панель навигации (ReplyKeyboard, persistent) ───────────────────────
#
# Отправляется один раз при /start и остаётся у пользователя навсегда.
# Работает как таб-бар мобильного приложения — доступна на любом шаге.
#
NAV_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✈️ Поиск"),    KeyboardButton(text="🔥 Горячие")],
        [KeyboardButton(text="📋 Подписки"), KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,   # компактная, не занимает пол-экрана
    is_persistent=True,     # не скрывается после нажатия (Telegram 6.9+)
)

# ── Inline-кнопка отмены внутри FSM-шагов ────────────────────────────────────
#
# Показывается под каждым вопросом визарда как страховка —
# но поскольку NAV_KB всегда видна, пользователь может просто нажать
# любую кнопку навигации и тоже выйти.
#
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
])

# ── Аэропорты ─────────────────────────────────────────────────────────────────

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

AIRPORT_TO_METRO: dict = {
    ap: metro
    for metro, aps in MULTI_AIRPORT_CITIES.items()
    for ap, _ in aps
}

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

SUPPORTED_TRANSFER_AIRPORTS: set = {
    "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
    "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
    "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK",
}

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