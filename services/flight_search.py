# services/flight_search.py
import os
import asyncio
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger

# Конфигурация API
AVIASALES_API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
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
    
    Args:
        origin: IATA код аэропорта вылета (например, 'MOW')
        destination: IATA код аэропорта прилёта (например, 'AER')
        depart_date: дата вылета в формате 'ГГГГ-ММ-ДД'
        return_date: дата возврата в формате 'ГГГГ-ММ-ДД' (опционально)
        currency: валюта ('rub', 'eur', 'usd')
        direct: только прямые рейсы (без пересадок)
    
    Returns:
        Список найденных рейсов
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

def generate_booking_link(
    flight: dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    route = f"{origin}{d1}{dest}{d2}{passengers_code}"

    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()

    base = "https://www.aviasales.ru/search/"
    url = f"{base}{route}"

    if marker.isdigit():
        url += f"?marker={marker}&sub_id={sub_id}"

    return url


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