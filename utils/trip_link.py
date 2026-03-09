"""
utils/trip_link.py

Генерация партнёрских ссылок Trip.com для поиска авиабилетов.

Переменные окружения (.env):
    TRIP_ALLIANCE_ID  — партнёрский AllianceId (например 7799359)
    TRIP_SID          — Site ID (например 294004999)
    TRIP_SUB3         — trip_sub3 идентификатор (например D13438784)
    TRIP_SUB1         — необязательный sub1 для трекинга (по умолчанию "tg_bot")

Использование:
    from utils.trip_link import build_trip_link
    url = build_trip_link(
        origin="SVO", dest="BKK",
        depart_date="2026-04-17",
        passengers_code="211",      # 2 взр., 1 реб., 1 мл.
        return_date="2026-04-20",   # None если туда
    )
"""

import os
from datetime import date
from urllib.parse import urlencode


def _normalize_trip_date(date_str: str) -> str:
    """
    Конвертирует дату любого формата в ГГГГ-ММ-ДД для Trip.com.
    Поддерживает: 'ДД.ММ', 'ДД.ММ.ГГГГ', 'ГГГГ-ММ-ДД' (возвращает как есть).
    """
    if not date_str:
        return date_str
    # Уже в нужном формате ГГГГ-ММ-ДД
    if len(date_str) == 10 and date_str[4] == '-':
        return date_str
    try:
        parts = date_str.split('.')
        day, month = int(parts[0]), int(parts[1])
        # Если год передан явно (ДД.ММ.ГГГГ)
        if len(parts) == 3 and len(parts[2]) == 4:
            year = int(parts[2])
        else:
            # Ближайший будущий год
            today = date.today()
            year = today.year
            try:
                target = date(year, month, day)
            except ValueError:
                return date_str
            if target < today:
                year += 1
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return date_str

# ── Маппинг IATA аэропортов → коды городов Trip.com ──────────────────────────
# Trip.com использует коды мета-городов (LON вместо LHR/LGW, MOW вместо SVO/DME)
# Если кода нет в маппинге — используем IATA как есть (работает для большинства городов)
_IATA_TO_TRIP: dict[str, str] = {
    # Россия
    "SVO": "MOW", "DME": "MOW", "VKO": "MOW", "ZIA": "MOW",
    "LED": "LED",
    "AER": "AER",
    "SVX": "SVX",
    "OVB": "OVB",
    # Великобритания
    "LHR": "LON", "LGW": "LON", "STN": "LON", "LTN": "LON", "LCY": "LON",
    "MAN": "MAN",
    # США
    "ORD": "CHI", "MDW": "CHI",
    "JFK": "NYC", "EWR": "NYC", "LGA": "NYC",
    "LAX": "LAX",
    "SFO": "SFO",
    "MIA": "MIA",
    # Япония
    "NRT": "TYO", "HND": "TYO",
    "KIX": "OSA", "ITM": "OSA",
    # Китай
    "PVG": "SHA", "SHA": "SHA",
    "PEK": "BJS", "PKX": "BJS",
    "CAN": "CAN",
    "SZX": "SZX",
    # Германия
    "FRA": "FRA",
    "MUC": "MUC",
    "TXL": "BER", "SXF": "BER", "BER": "BER",
    # Франция
    "CDG": "PAR", "ORY": "PAR",
    # Италия
    "FCO": "ROM", "CIA": "ROM",
    "MXP": "MIL", "LIN": "MIL",
    # Испания
    "MAD": "MAD",
    "BCN": "BCN",
    # ОАЭ
    "DXB": "DXB",
    "AUH": "AUH",
    # Таиланд
    "BKK": "BKK", "DMK": "BKK",
    "HKT": "HKT",
    # Вьетнам
    "HAN": "HAN",
    "SGN": "SGN",
    # Индия
    "DEL": "DEL",
    "BOM": "BOM",
    # Австралия
    "SYD": "SYD",
    "MEL": "MEL",
    # Турция
    "IST": "IST", "SAW": "IST",
    "AYT": "AYT",
    # Греция
    "ATH": "ATH",
    # Египет
    "CAI": "CAI",
    "HRG": "HRG",
    "SSH": "SSH",
    # Индонезия
    "CGK": "JKT",
    "DPS": "DPS",
    # Малайзия
    "KUL": "KUL",
    # Сингапур
    "SIN": "SIN",
    # Филиппины
    "MNL": "MNL",
    # Южная Корея
    "ICN": "SEL", "GMP": "SEL",
    # Гонконг
    "HKG": "HKG",
    # Тайвань
    "TPE": "TPE",
}


def iata_to_trip_city(iata: str) -> str:
    """Конвертирует IATA аэропорта в код города Trip.com."""
    return _IATA_TO_TRIP.get(iata.upper(), iata.upper())


def build_trip_link(
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: str | None = None,
) -> str | None:
    """
    Строит партнёрскую ссылку Trip.com для поиска авиабилетов.

    Args:
        origin:          IATA код аэропорта вылета (например "SVO")
        dest:            IATA код аэропорта назначения (например "BKK")
        depart_date:     Дата вылета "YYYY-MM-DD"
        passengers_code: Строка вида "211" = 2 взр. + 1 реб. + 1 мл.
        return_date:     Дата обратного рейса "YYYY-MM-DD" или None

    Returns:
        Готовая партнёрская URL или None если не настроен AllianceId
    """
    alliance_id = os.getenv("TRIP_ALLIANCE_ID", "").strip()
    sid         = os.getenv("TRIP_SID", "").strip()
    sub3        = os.getenv("TRIP_SUB3", "").strip()
    sub1        = os.getenv("TRIP_SUB1", "tg_bot").strip()

    if not alliance_id or not sid:
        return None

    # Нормализуем даты в формат ГГГГ-ММ-ДД (Trip.com не принимает ДД.ММ)
    depart_date = _normalize_trip_date(depart_date)
    if return_date:
        return_date = _normalize_trip_date(return_date)

    # Конвертируем IATA → Trip.com city codes
    dcity = iata_to_trip_city(origin)
    acity = iata_to_trip_city(dest)

    # Декодируем пассажиров из passengers_code "211" → adults=2, children=1, infants=1
    try:
        code     = str(passengers_code).strip()
        adults   = int(code[0]) if len(code) >= 1 else 1
        children = int(code[1]) if len(code) >= 2 else 0
        infants  = int(code[2]) if len(code) >= 3 else 0
    except (ValueError, IndexError):
        adults, children, infants = 1, 0, 0

    trip_type = "rt" if return_date else "ow"

    params: dict = {
        "dcity":           dcity,
        "acity":           acity,
        "ddate":           depart_date,
        "triptype":        trip_type,
        "class":           "y",
        "quantity":        adults,
        "locale":          "ru-RU",
        "curr":            "RUB",
        "Allianceid":      alliance_id,
        "SID":             sid,
        "trip_sub1":       sub1,
    }

    if return_date:
        params["rdate"] = return_date

    if children > 0:
        params["childqty"] = children

    if infants > 0:
        params["babyqty"] = infants

    if sub3:
        params["trip_sub3"] = sub3

    return f"https://ru.trip.com/flights/showfarefirst?{urlencode(params)}"


def is_trip_supported(origin: str, dest: str) -> bool:
    """
    Проверяет что маршрут поддерживается Trip.com.
    Trip.com покрывает большинство российских маршрутов включая внутренние.
    Отключаем только для совсем маленьких региональных аэропортов.
    """
    # Очень маленькие аэропорты без представительства на Trip.com
    UNSUPPORTED = {
        "NAL", "ESL", "IGT", "STW", "GDZ", "MRV",
        "GDX", "UUS", "PKC", "UUD", "PES", "MMK", "ARH",
    }
    return origin.upper() not in UNSUPPORTED and dest.upper() not in UNSUPPORTED