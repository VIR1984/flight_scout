# handlers/flight_constants.py
"""
Общие константы для обработчиков полётов.
"""

from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
)

# ── Нижняя панель навигации (ReplyKeyboard, persistent) ───────────────────────
#
# Отправляется один раз при /start и остаётся у пользователя навсегда.
# Работает как таб-бар мобильного приложения — доступна на любом шаге.
#
NAV_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="✈️ Поиск"),       KeyboardButton(text="🗺 Маршрут")],
        [KeyboardButton(text="🔥 Горячие"),      KeyboardButton(text="📋 Подписки")],
        [KeyboardButton(text="💬 Обратная связь"), KeyboardButton(text="❓ Помощь")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Inline-кнопка отмены внутри FSM-шагов ────────────────────────────────────
#
# Показывается под каждым вопросом визарда как страховка —
# но поскольку NAV_KB всегда видна, пользователь может просто нажать
# любую кнопку навигации и тоже выйти.
#
CANCEL_KB = None  # кнопка "Отменить поиск" убрана

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
    # ── Российские ──────────────────────────────────────────
    "SU": "Аэрофлот",
    "S7": "S7 Airlines",
    "DP": "Победа",
    "U6": "Уральские авиалинии",
    "FV": "Россия",
    "UT": "ЮТэйр",
    "N4": "Нордстар",
    "IK": "Победа",
    "WZ": "Red Wings",
    "5N": "Nordavia",
    "Y7": "NordStar",
    "R3": "Якутия",
    "6W": "Саратовские авиалинии",
    "YC": "Yamal Airlines",
    # ── Ближний Восток ───────────────────────────────────────
    "EK": "Emirates",
    "FZ": "flydubai",
    "EY": "Etihad Airways",
    "G9": "Air Arabia",
    "SV": "Saudia",
    "QR": "Qatar Airways",
    "WY": "Oman Air",
    "OV": "SalamAir",
    "SL": "SalamAir",
    "GF": "Gulf Air",
    "J9": "Jazeera Airways",
    "XY": "flynas",
    "PC": "Pegasus Airlines",
    # ── Турция ───────────────────────────────────────────────
    "TK": "Turkish Airlines",
    "AJ": "Corendon Airlines",
    "XC": "Corendon Airlines",
    # ── Азия ─────────────────────────────────────────────────
    "SQ": "Singapore Airlines",
    "TR": "Scoot",
    "MH": "Malaysia Airlines",
    "AK": "AirAsia",
    "FD": "Thai AirAsia",
    "TG": "Thai Airways",
    "VZ": "Thai Vietjet",
    "VJ": "VietJet Air",
    "BL": "Pacific Airlines",
    "VN": "Vietnam Airlines",
    "QH": "Bamboo Airways",
    "HX": "Hong Kong Airlines",
    "CX": "Cathay Pacific",
    "KE": "Korean Air",
    "OZ": "Asiana Airlines",
    "TW": "T'way Air",
    "LJ": "Jin Air",
    "7C": "Jeju Air",
    "CI": "China Airlines",
    "CZ": "China Southern",
    "CA": "Air China",
    "MU": "China Eastern",
    "HO": "Juneyao Airlines",
    "9C": "Spring Airlines",
    "AI": "Air India",
    "6E": "IndiGo",
    "SG": "SpiceJet",
    # ── СНГ ──────────────────────────────────────────────────
    "HY": "Uzbekistan Airways",
    "ZL": "Qazaq Air",
    "KC": "Air Astana",
    "DV": "SCAT Airlines",
    "B9": "Air Bashkortostan",
    "9U": "Air Moldova",
    "ZE": "Eastar Jet",
    "5J": "Cebu Pacific",
    # ── Европа ───────────────────────────────────────────────
    "LH": "Lufthansa",
    "BA": "British Airways",
    "AF": "Air France",
    "KL": "KLM",
    "AY": "Finnair",
    "SK": "SAS",
    "LX": "Swiss",
    "OS": "Austrian Airlines",
    "AZ": "ITA Airways",
    "IB": "Iberia",
    "VY": "Vueling",
    "U2": "easyJet",
    "FR": "Ryanair",
    "W6": "Wizz Air",
    "RK": "Ryanair UK",
    # ── Прочие ───────────────────────────────────────────────
    "AA": "American Airlines",
    "UA": "United Airlines",
    "DL": "Delta Air Lines",
    "QF": "Qantas",
    "ET": "Ethiopian Airlines",
    "KQ": "Kenya Airways",
}

# ── Страны: ISO → русское название ────────────────────────────────────────────
COUNTRY_NAMES_RU: dict[str, str] = {
    "RU": "Россия",    "AE": "ОАЭ",       "TR": "Турция",    "TH": "Таиланд",
    "CN": "Китай",     "EG": "Египет",    "GR": "Греция",    "ES": "Испания",
    "IT": "Италия",    "FR": "Франция",   "DE": "Германия",  "CZ": "Чехия",
    "HU": "Венгрия",   "AT": "Австрия",   "NL": "Нидерланды","PL": "Польша",
    "FI": "Финляндия", "SE": "Швеция",    "NO": "Норвегия",  "DK": "Дания",
    "PT": "Португалия","SG": "Сингапур",  "MY": "Малайзия",  "ID": "Индонезия",
    "VN": "Вьетнам",   "IN": "Индия",     "JP": "Япония",    "KR": "Ю. Корея",
    "US": "США",       "CA": "Канада",    "AU": "Австралия", "MV": "Мальдивы",
    "LK": "Шри-Ланка", "CY": "Кипр",     "MT": "Мальта",    "HR": "Хорватия",
    "RS": "Сербия",    "GE": "Грузия",    "AM": "Армения",   "AZ": "Азербайджан",
    "KZ": "Казахстан", "UZ": "Узбекистан","BY": "Беларусь",  "IL": "Израиль",
    "JO": "Иордания",  "MA": "Марокко",   "TN": "Тунис",     "MX": "Мексика",
    "BR": "Бразилия",  "TJ": "Таджикистан","KG": "Кыргызстан","MN": "Монголия",
    "QA": "Катар",     "BH": "Бахрейн",   "KW": "Кувейт",    "OM": "Оман",
    "LB": "Ливан",     "GB": "Великобритания","UA": "Украина", "MD": "Молдова",
}


def iso_flag(iso: str) -> str:
    """'RU' → '🇷🇺'  (Unicode regional indicator pair)"""
    if not iso or len(iso) != 2:
        return "🌐"
    base = 0x1F1E6 - ord("A")
    return chr(base + ord(iso[0].upper())) + chr(base + ord(iso[1].upper()))


def iata_country_iso(iata: str) -> str:
    """Возвращает ISO-код страны по IATA города (через CITIES_DATA)."""
    try:
        from utils.cities_loader import CITIES_DATA
        return CITIES_DATA.get(iata, {}).get("country_code", "") or ""
    except Exception:
        return ""