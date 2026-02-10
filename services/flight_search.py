import os
import asyncio
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote
from utils.logger import logger

# Конфигурация API
AVIASALES_API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
GROUPED_PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()

def normalize_date(date_str: str) -> str:
    """Преобразует дату ДД.ММ в формат ГГГГ-ММ-ДД для 2026 года (или 2027 для январь/февраль)"""
    try:
        day, month = map(int, date_str.split('.'))
        year = 2026
        if month < 2 or (month == 2 and day < 8):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except:
        return date_str

def format_avia_link_date(date_str: str) -> str:
    """Форматирует дату ДД.ММ → ДДММ для ссылки Aviasales"""
    try:
        day, month = date_str.split('.')
        return f"{day}{month}"
    except:
        return date_str.replace('.', '')

def add_marker_to_url(url: str, marker: str, sub_id: str = "telegram") -> str:
    """
    Добавляет маркер и sub_id к ссылке Aviasales.
    Корректно обрабатывает уже существующие параметры.
    """
    if not marker or not url:
        return url
    
    # Парсим URL
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    
    # Удаляем старые значения маркера и sub_id (на случай дублирования)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    
    # Добавляем новые значения
    query_params['marker'] = [marker]
    query_params['sub_id'] = [sub_id]
    
    # Собираем обратно
    new_query = urlencode(query_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def update_passengers_in_aviasales_link(link: str, passengers_code: str) -> str:
    """
    Обновляет код пассажиров в ссылке Aviasales.
    
    Формат ссылки:
    - Односторонний: IST1003MOW1?t=... → заменяем "1" перед ?t на новый код
    - Туда-обратно: MOW1003IST15031?t=... → заменяем "1" перед ?t на новый код
    
    Args:
        link: Исходная ссылка от API
        passengers_code: Код пассажиров ("1", "2", "21", "211" и т.д.)
    
    Returns:
        Ссылка с обновленным кодом пассажиров
    """
    if not link or not passengers_code:
        return link
    
    # Находим позицию параметра ?t=
    t_param_pos = link.find('?t=')
    
    if t_param_pos == -1:
        # Если ?t= не найден, это старый формат - добавляем в конец маршрута
        if '/search/' in link:
            # Удаляем всё после /search/ до конца или до ?
            base_part = link.split('/search/')[1]
            if '?' in base_part:
                base_part = base_part.split('?')[0]
            
            # Удаляем последнюю цифру (старый код пассажиров) если есть
            while base_part and base_part[-1].isdigit():
                base_part = base_part[:-1]
            
            # Добавляем новый код
            new_route = base_part + passengers_code
            return link.replace(f"/search/{base_part}", f"/search/{new_route}")
        return link
    
    # Разбиваем ссылку на части: до ?t= и после
    before_t = link[:t_param_pos]
    after_t = link[t_param_pos:]
    
    # Удаляем старый код пассажиров (цифры перед ?t=)
    while before_t and before_t[-1].isdigit():
        before_t = before_t[:-1]
    
    # Добавляем новый код пассажиров
    new_link = before_t + passengers_code + after_t
    
    return new_link

async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    direct: bool = False
) -> List[Dict]:
    """
    Ищет авиабилеты через Travelpayouts API.
    """
    if not AVIASALES_TOKEN:
        logger.warning("⚠️ AVIASALES_TOKEN/API_TOKEN не установлен — поиск авиабилетов недоступен")
        return []
    
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "currency": currency,
        "token": AVIASALES_TOKEN,
        "limit": 10,
        "sorting": "price"
    }
    
    if return_date:
        params["return_date"] = return_date
    
    if direct:
        params["direct"] = "true"
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(AVIASALES_API_URL, params=params, timeout=10) as response:
                if response.status == 429:
                    logger.warning("⚠️ Достигнут лимит API Aviasales (429). Ждём 60 секунд...")
                    await asyncio.sleep(60)
                    return []
                
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка API Aviasales: {response.status} - {error_text}")
                    return []
                
                data = await response.json()
                flights = data.get("data", [])
                
                # Добавляем маркер ко всем ссылкам в результатах
                marker = os.getenv("TRAFFIC_SOURCE", "").strip()
                sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
                
                for flight in flights:
                    # Обработка поля 'link' (если есть)
                    if flight.get("link"):
                        flight["link"] = add_marker_to_url(flight["link"], marker, sub_id)
                    
                    # Обработка поля 'deep_link' (если есть)
                    if flight.get("deep_link"):
                        flight["deep_link"] = add_marker_to_url(flight["deep_link"], marker, sub_id)
                
                return flights
                
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Aviasales API: {e}")
            return []

async def search_grouped_prices(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    passengers: str = "1"
) -> Optional[Dict]:
    """
    Ищет авиабилеты через Travelpayouts Grouped Prices API.
    
    Args:
        origin: IATA код аэропорта вылета
        destination: IATA код аэропорта прилёта
        depart_date: дата вылета в формате 'ГГГГ-ММ-ДД'
        return_date: дата возврата в формате 'ГГГГ-ММ-ДД' (опционально)
        currency: валюта ('rub', 'eur', 'usd')
        passengers: код пассажиров ("1", "2", "21", "211" и т.д.)
    
    Returns:
        Результат API или None
    """
    if not AVIASALES_TOKEN:
        logger.warning("⚠️ AVIASALES_TOKEN не установлен — поиск недоступен")
        return None
    
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": depart_date,
        "currency": currency,
        "token": AVIASALES_TOKEN
    }
    
    if return_date:
        params["return_date"] = return_date
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(GROUPED_PRICES_URL, params=params, timeout=10) as response:
                if response.status == 429:
                    logger.warning("⚠️ Достигнут лимит API Aviasales (429). Ждём 60 секунд...")
                    await asyncio.sleep(60)
                    return None
                
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка Grouped Prices API: {response.status} - {error_text}")
                    return None
                
                data = await response.json()
                
                # Обновляем ссылки с правильным количеством пассажиров
                marker = os.getenv("TRAFFIC_SOURCE", "").strip()
                sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
                
                if "data" in data:
                    for route in data["data"]:
                        if route.get("link"):
                            # Обновляем код пассажиров
                            route["link"] = update_passengers_in_aviasales_link(route["link"], passengers)
                            # Добавляем маркер
                            route["link"] = add_marker_to_url(route["link"], marker, sub_id)
                
                return data
                
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Grouped Prices API")
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Grouped Prices API: {e}")
            return None

def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    """
    Генерирует ссылку для бронирования на Aviasales с маркером.
    
    Для нового формата использует структуру с ?t= параметром.
    Формат: ORIGDDMMDEST[PASS]?t=... для в одну сторону
    Формат: ORIGDDMMDESTDDMM[PASS]?t=... для туда/обратно
    
    Args:
        flight: Данные рейса из API
        origin: IATA код вылета
        dest: IATA код прилёта
        depart_date: Дата вылета ДД.ММ
        passengers_code: Код пассажиров ("1", "2", "21", "211")
        return_date: Дата возврата ДД.ММ (опционально)
    
    Returns:
        Ссылка для бронирования
    """
    # Сначала пробуем использовать ссылку из самого рейса
    if flight.get("link"):
        # Обновляем код пассажиров в существующей ссылке
        link = update_passengers_in_aviasales_link(flight["link"], passengers_code)
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
        
        if marker:
            return add_marker_to_url(link, marker, sub_id)
        return link
    
    # Fallback: генерируем ссылку вручную (старый формат)
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    
    if return_date:
        route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    else:
        route = f"{origin}{d1}{dest}{passengers_code}"
    
    base_url = f"https://www.aviasales.ru/search/{route}"
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    if marker:
        return add_marker_to_url(base_url, marker, sub_id)
    
    return base_url

def find_cheapest_flight_on_exact_date(
    flights: List[Dict],
    requested_depart_date: str,
    requested_return_date: Optional[str] = None
) -> Optional[Dict]:
    """
    Находит самый дешёвый рейс, соответствующий *точно* запрошенным датам.
    """
    exact_flights = []
    
    for flight in flights:
        flight_depart_date = flight.get("departure_at", "")[:10]  # YYYY-MM-DD
        flight_return_date = flight.get("return_at", "")[:10] if flight.get("return_at") else None
        
        # Преобразуем запрошенные даты в формат YYYY-MM-DD для сравнения
        req_depart = normalize_date(requested_depart_date)
        req_return = normalize_date(requested_return_date) if requested_return_date else None
        
        # Сравниваем даты
        if flight_depart_date == req_depart:
            if req_return:
                if flight_return_date and flight_return_date == req_return:
                    exact_flights.append(flight)
            else:
                # Односторонний — достаточно совпадения вылета
                exact_flights.append(flight)
    
    if not exact_flights:
        # Если нет точных совпадений, возвращаем самый дешёвый из всех (как fallback)
        return min(flights, key=lambda f: f.get("value") or f.get("price") or 999999999)
    
    # Сортируем по цене среди подходящих под даты
    return min(exact_flights, key=lambda f: f.get("value") or f.get("price") or 999999999)