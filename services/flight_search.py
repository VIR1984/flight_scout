import os
import asyncio
import aiohttp
import re
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime
from utils.logger import logger
from utils.cities import IATA_TO_CITY

# Конфигурация API
AVIASALES_GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def normalize_date(date_str: str) -> str:
    """Преобразует дату ДД.ММ в формат ГГГГ-ММ-ДД для 2026 года (или 2027 для январь/февраль)"""
    try:
        day, month = map(int, date_str.split('.'))
        year = 2026
        if month < 2 or (month == 2 and day < 8):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return date_str

def format_avia_link_date(date_str: str) -> str:
    """Форматирует дату ДД.ММ → ДДММ для ссылки Aviasales"""
    try:
        day, month = date_str.split('.')
        return f"{day}{month}"
    except Exception:
        return date_str.replace('.', '')

def add_marker_to_url(url: str, marker: str, sub_id: str = "telegram") -> str:
    """
    Добавляет маркер и sub_id к ссылке Aviasales.
    Корректно обрабатывает уже существующие параметры.
    """
    if not marker or not url:
        return url
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    query_params['marker'] = [marker]
    query_params['sub_id'] = [sub_id]
    new_query = urlencode(query_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def format_datetime(dt_str: str) -> str:
    """Форматирует дату-время из ISO в ЧЧ:ММ"""
    if not dt_str:
        return "??:??"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime("%H:%M")
    except:
        return dt_str.split('T')[1][:5] if 'T' in dt_str else "??:??"

def format_duration(minutes: int) -> str:
    """Форматирует длительность полета в читаемый вид"""
    if not minutes:
        return "—"
    hours = minutes // 60
    mins = minutes % 60
    parts = []
    if hours: parts.append(f"{hours}ч")
    if mins: parts.append(f"{mins}м")
    return " ".join(parts) if parts else "—"

def get_airport_name(iata: str) -> str:
    """Возвращает название аэропорта по IATA-коду"""
    AIRPORT_NAMES = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    return AIRPORT_NAMES.get(iata, iata)

def get_airline_name(code: str) -> str:
    """Возвращает название авиакомпании по коду"""
    AIRLINE_NAMES = {
        "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
        "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
    }
    return AIRLINE_NAMES.get(code, code)

def format_transfers_count(transfers: int) -> str:
    """Форматирует количество пересадок в текст"""
    if transfers == 0:
        return "✈️ Прямой рейс"
    elif transfers == 1:
        return "✈️ 1 пересадка"
    else:
        return f"✈️ {transfers} пересадки"

# ==================== ОСНОВНАЯ ФУНКЦИЯ ПОИСКА ====================

async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    direct: bool = False
) -> List[Dict]:
    """
    Ищет авиабилеты через Travelpayouts API (grouped_prices).
    Возвращает список рейсов, совместимый с остальным кодом.
    """
    if not AVIASALES_TOKEN:
        logger.warning("⚠️ AVIASALES_TOKEN не установлен — поиск авиабилетов недоступен")
        return []
    
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_date,
        "currency": currency,
        "token": AVIASALES_TOKEN,
        "group_by": "departure_at",
        "direct": "true" if direct else "false"
    }
    
    if return_date:
        params["return_at"] = return_date
        # Опционально: задать длительность поездки (в днях)
        try:
            d1 = datetime.fromisoformat(depart_date)
            d2 = datetime.fromisoformat(return_date)
            trip_days = (d2 - d1).days
            if trip_days > 0:
                params["min_trip_duration"] = trip_days
                params["max_trip_duration"] = trip_days
        except Exception:
            pass  # игнорируем ошибки парсинга
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(AVIASALES_GROUPED_URL, params=params, timeout=10) as response:
                if response.status == 429:
                    logger.warning("⚠️ Достигнут лимит API Aviasales (429). Ждём 60 секунд...")
                    await asyncio.sleep(60)
                    return []
                
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка API Aviasales: {response.status} - {error_text}")
                    return []
                
                data = await response.json()
                if not data.get("success"):
                    logger.error(f"❌ API вернул ошибку: {data.get('error')}")
                    return []
                
                grouped_flights = data.get("data", {})
                flights = []
                for date_key, flight in grouped_flights.items():
                    # Приводим к формату, совместимому с prices_for_dates
                    flight["value"] = flight.get("price")  # для min(flights, key=lambda f: f.get("value"))
                    flight["departure_at"] = flight.get("departure_at", f"{date_key}T00:00:00+03:00")
                    flight["return_at"] = flight.get("return_at", "")
                    flight["origin"] = flight.get("origin", origin)
                    flight["destination"] = flight.get("destination", destination)
                    flights.append(flight)
                
                # Добавляем маркер ко всем ссылкам
                marker = os.getenv("TRAFFIC_SOURCE", "").strip()
                sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
                for flight in flights:
                    if flight.get("link"):
                        flight["link"] = add_marker_to_url(flight["link"], marker, sub_id)
                    if flight.get("deep_link"):
                        flight["deep_link"] = add_marker_to_url(flight["deep_link"], marker, sub_id)
                
                return flights
                
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Aviasales API: {e}")
            return []

# ==================== ФИЛЬТРАЦИЯ РЕЙСОВ ====================

def filter_flights_by_type(
    flights: List[Dict],
    flight_type: str
) -> List[Dict]:
    """
    Фильтрует рейсы по типу:
    - "direct": только прямые рейсы (без пересадок)
    - "transfer": только рейсы с пересадками
    - "all": все рейсы (без фильтрации)
    """
    if flight_type == "direct":
        return [f for f in flights if f.get("transfers", 999) == 0]
    elif flight_type == "transfer":
        return [f for f in flights if f.get("transfers", 0) > 0]
    else:  # "all"
        return flights

# ==================== ГЕНЕРАЦИЯ ССЫЛОК ====================

def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    """
    Генерирует ссылку для бронирования на Aviasales с ПОЛНЫМ кодом пассажиров.
    Формат маршрута:
    • Туда-обратно: ORIGDDMMDESTDDMM[PASS]  (например, MOW1003AER1503211)
    • В одну сторону: ORIGDDMMDEST[PASS]     (например, AER1003MOW211)
    Где [PASS] — полный код пассажиров (1-3 цифры):
    • "1"   → 1 взрослый
    • "2"   → 2 взрослых
    • "21"  → 2 взр. + 1 реб.
    • "211" → 2 взр. + 1 реб. + 1 мл.
    """
    # Валидация и нормализация кода пассажиров
    if not passengers_code or not isinstance(passengers_code, str):
        passengers_code = "1"
    
    # Убираем всё кроме цифр и оставляем максимум 3 цифры
    passengers_code = re.sub(r'\D', '', passengers_code)[:3]
    
    # Если после очистки пусто или начинается с 0 — используем "1"
    if not passengers_code or passengers_code[0] == '0':
        passengers_code = "1"
    
    # Форматируем даты для ссылки (ДДММ)
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    
    # Формируем маршрут с ПОЛНЫМ кодом пассажиров
    if return_date:
        # Туда-обратно: MOW1003AER1503211
        route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    else:
        # В одну сторону: AER1003MOW211
        route = f"{origin}{d1}{dest}{passengers_code}"
    
    base_url = f"https://www.aviasales.ru/search/{route}"
    
    # Добавляем маркер партнера и sub_id
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        return add_marker_to_url(base_url, marker, sub_id)
    
    return base_url

def update_passengers_in_link(link: str, passengers_code: str) -> str:
    """
    Корректно заменяет количество пассажиров в ссылке Aviasales.
    ВАЖНО: В ссылках от API пассажиры — ВСЕГДА последняя цифра пути.
    """
    if not link or not passengers_code or not passengers_code.isdigit():
        return link
    
    # Валидация кода пассажиров (1-3 цифры, первая 1-9)
    if not re.match(r'^[1-9]\d{0,2}$', passengers_code):
        return link
    
    # Определяем тип ссылки (относительная /search/... или абсолютная)
    if link.startswith('/'):
        path = link
        is_relative = True
        parsed = None
    else:
        parsed = urlparse(link)
        path = parsed.path
        is_relative = False
    
    # Проверяем формат пути
    if '/search/' not in path:
        return link
    
    # Разделяем путь на части до и после /search/
    path_parts = path.split('/search/', 1)
    if len(path_parts) < 2:
        return link
    
    prefix = path_parts[0]  # обычно пустая строка или '/'
    search_part = path_parts[1]
    
    # Разделяем маршрут и параметры запроса (?t=...)
    if '?' in search_part:
        route, query = search_part.split('?', 1)
        has_query = True
    else:
        route, query = search_part, ""
        has_query = False
    
    # Удаляем последнюю цифру (старое количество пассажиров) и добавляем новый код
    if route and route[-1].isdigit():
        new_route = route[:-1] + passengers_code
    else:
        # Если нет цифры в конце, добавляем в конец
        new_route = route + passengers_code
    
    # Собираем путь обратно
    if has_query:
        new_path = f"/search/{new_route}?{query}"
    else:
        new_path = f"/search/{new_route}"
    
    # Возвращаем в исходном формате
    if is_relative:
        return new_path
    else:
        return urlunparse(parsed._replace(path=new_path))

# ==================== ПОИСК САМОГО ДЕШЕВОГО РЕЙСА ====================

def find_cheapest_flight_on_exact_date(
    flights: List[Dict],
    requested_depart_date: str,
    requested_return_date: Optional[str] = None
) -> Optional[Dict]:
    """
    Находит самый дешёвый рейс, соответствующий *точно* запрошенным датам.
    """
    exact_flights = []
    req_depart = normalize_date(requested_depart_date)
    req_return = normalize_date(requested_return_date) if requested_return_date else None
    
    for flight in flights:
        flight_depart = flight.get("departure_at", "")[:10]
        flight_return = flight.get("return_at", "")[:10] if flight.get("return_at") else None
        
        if flight_depart == req_depart:
            if req_return:
                if flight_return and flight_return == req_return:
                    exact_flights.append(flight)
            else:
                exact_flights.append(flight)
    
    if not exact_flights:
        return min(flights, key=lambda f: f.get("value") or f.get("price") or 999999999)
    
    return min(exact_flights, key=lambda f: f.get("value") or f.get("price") or 999999999)

def find_cheapest_flight(
    flights: List[Dict]
) -> Optional[Dict]:
    """Находит самый дешёвый рейс из списка"""
    if not flights:
        return None
    return min(flights, key=lambda f: f.get("value") or f.get("price") or 999999999)

# ==================== ПАРСИНГ ПАССАЖИРОВ ====================

def parse_passengers(s: str) -> str:
    """
    Парсит строку с пассажирами и возвращает код пассажиров.
    Примеры:
    - "2 взр" → "2"
    - "2 взр, 1 реб" → "21"
    - "2 взр, 1 мл" → "201"
    """
    if not s:
        return "1"
    
    if s.isdigit():
        return s
    
    adults = children = infants = 0
    
    for part in s.split(","):
        part = part.strip().lower()
        n = int(re.search(r"\d+", part).group()) if re.search(r"\d+", part) else 1
        
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
    
    # Формируем код пассажиров
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    
    return code

def format_passenger_desc(code: str) -> str:
    """
    Форматирует код пассажиров в читаемое описание.
    Примеры:
    - "1" → "1 взр."
    - "21" → "2 взр., 1 реб."
    - "211" → "2 взр., 1 реб., 1 мл."
    """
    try:
        adults = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants = int(code[2]) if len(code) > 2 else 0
        
        parts = []
        if adults:
            parts.append(f"{adults} взр.")
        if children:
            parts.append(f"{children} реб.")
        if infants:
            parts.append(f"{infants} мл.")
        
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."

# ==================== ФОРМАТИРОВАНИЕ ДАТ ДЛЯ ПОКАЗА ====================

def format_user_date(date_str: str) -> str:
    """
    Форматирует дату ДД.ММ в ДД.ММ.ГГГГ для показа пользователю.
    """
    try:
        d, m = map(int, date_str.split('.'))
        year = 2026
        if m < 2 or (m == 2 and d < 8):
            year = 2027
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

# ==================== ФУНКЦИИ ДЛЯ "ВЕЗДЕ" ====================

async def search_origin_everywhere(
    dest_iata: str,
    depart_date: str,
    flight_type: str = "all"
) -> List[Dict]:
    """
    Ищет рейсы из всех городов России в конкретный город.
    """
    from utils.cities import GLOBAL_HUBS
    
    origins = GLOBAL_HUBS[:5]
    all_flights = []
    
    for orig in origins:
        if orig == dest_iata:
            continue
        
        # Игнорируем return_date для "везде" — всегда однонаправленный поиск
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            None
        )
        
        # Фильтрация по типу рейса
        flights = filter_flights_by_type(flights, flight_type)
        
        flights = [f for f in flights if f.get("destination") == dest_iata]
        for f in flights:
            f["origin"] = orig
        
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    
    return all_flights

async def search_destination_everywhere(
    origin_iata: str,
    depart_date: str,
    flight_type: str = "all"
) -> List[Dict]:
    """
    Ищет рейсы из конкретного города во все популярные направления мира.
    """
    from utils.cities import GLOBAL_HUBS
    
    destinations = GLOBAL_HUBS[:5]
    all_flights = []
    
    for dest in destinations:
        if dest == origin_iata:
            continue
        
        # Игнорируем return_date для "везде" — всегда однонаправленный поиск
        flights = await search_flights(
            origin_iata,
            dest,
            normalize_date(depart_date),
            None
        )
        
        # Фильтрация по типу рейса
        flights = filter_flights_by_type(flights, flight_type)
        
        for f in flights:
            f["destination"] = dest
        
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    
    return all_flights

# ==================== ФОРМИРОВАНИЕ ТЕКСТА РЕЗУЛЬТАТА ====================

def build_flight_result_text(
    flight: Dict,
    origin_iata: str,
    dest_iata: str,
    display_depart: str,
    display_return: Optional[str],
    passenger_desc: str,
    is_roundtrip: bool = False
) -> str:
    """
    Формирует текст результата поиска для показа пользователю.
    """
    price = flight.get("value") or flight.get("price") or "?"
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
    
    departure_time = format_datetime(flight.get("departure_at", ""))
    arrival_time = format_datetime(flight.get("return_at", ""))
    duration = format_duration(flight.get("duration", 0))
    transfers = flight.get("transfers", 0)
    
    origin_airport = get_airport_name(origin_iata)
    dest_airport = get_airport_name(dest_iata)
    transfer_text = format_transfers_count(transfers)
    
    header = f"✅ <b>Самый дешёвый вариант на {display_depart} ({passenger_desc}):</b>"
    route_line = f"🛫 <b>Рейс: {origin_name}</b> → <b>{dest_name}</b>"
    
    text = (
        f"{header}\n"
        f"{route_line}\n"
        f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
        f"📅 Дата вылета: {display_depart}\n"
        f"⏱️ Продолжительность полета: {duration}\n"
        f"{transfer_text}\n"
    )
    
    # Добавляем информацию об авиакомпании и номере рейса
    airline = flight.get("airline", "")
    flight_number = flight.get("flight_number", "")
    if airline or flight_number:
        airline_display = get_airline_name(airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
        text += f"✈️ {flight_display}\n"
    
    text += f"\n💰 <b>Цена от:</b> {price} ₽"
    
    if is_roundtrip and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"
    
    text += f"\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"
    
    return text