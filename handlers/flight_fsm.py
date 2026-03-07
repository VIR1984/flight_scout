# handlers/flight_fsm.py
"""
FSM-состояния и вспомогательные функции поиска билетов.
Импортируется из flight_wizard, country_search и search_results.
"""
import re
from datetime import datetime

from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from handlers.flight_constants import MULTI_AIRPORT_CITIES, AIRPORT_TO_METRO
from handlers.everywhere_search import format_user_date

# ════════════════════════════════════════════════════════════════
# FSM
# ════════════════════════════════════════════════════════════════

class FlightSearch(StatesGroup):
    route                = State()
    choose_airport       = State()
    choose_country_city  = State()  # выбор города когда введена страна
    depart_date          = State()
    need_return          = State()
    return_date          = State()
    flight_type          = State()
    adults               = State()
    has_children         = State()
    children             = State()
    infants              = State()
    confirm              = State()


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

    # Формируем маршрут: Город (IATA_аэропорта) → Город (IATA)
    origin_name = data.get("origin_name", "")
    dest_name   = data.get("dest_name", "")
    origin_iata = data.get("origin_iata", "")
    dest_iata   = data.get("dest_iata", "")
    ap_label    = data.get("origin_airport_label", "")

    # Откуда: если выбран конкретный аэропорт — показываем его IATA,
    # иначе — IATA города (MOW, LED и т.д.)
    if origin_name and origin_name != "Везде":
        origin_part = f"{origin_name} ({origin_iata})" if origin_iata else origin_name
    else:
        origin_part = origin_name or ""

    if dest_name and dest_name != "Везде":
        dest_part = f"{dest_name} ({dest_iata})" if dest_iata else dest_name
    else:
        dest_part = dest_name or ""

    route = f"{origin_part} → {dest_part}"
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
