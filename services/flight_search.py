import os
import asyncio
import aiohttp
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger
import re

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

def update_link_with_user_dates(link: str, origin: str, dest: str, depart_date: str, return_date: Optional[str], passengers_code: str) -> str:
    """
    Обновляет партнёрскую ссылку, заменяя даты на указанные пользователем.
    Сохраняет остальные параметры (t, expected_price_uuid и т.д.).
    """
    # Пример: /AER1102MOW12021?t=...&search_date=09022026&...
    # Нужно заменить AER1102MOW12021 на AER1003MOW15031 и search_date на 10032026

    # Форматируем даты
    d1 = format_avia_link_date(depart_date)  # "10.03" -> "1003"
    d2 = format_avia_link_date(return_date) if return_date else ""
    # Формируем новый маршрут
    new_route = f"{origin}{d1}{dest}{d2}{passengers_code}"

    # Извлекаем текущий маршрут из ссылки (до ?)
    path_parts = link.split('?', 1)
    old_path = path_parts[0]  # например, '/AER1102MOW12021'
    query = path_parts[1] if len(path_parts) > 1 else ""

    # Заменяем старый маршрут на новый
    new_path = f"/{new_route}"

    # Разбираем query параметры
    parsed_query = parse_qs(query, keep_blank_values=True)
    # Обновляем search_date: форматируем как DDMMYYYY
    date_parts = depart_date.split('.')
    if len(date_parts) == 2:
        # Формат: ДД.ММ
        day_month = date_parts[0] + date_parts[1]  # "1003"
        year = "2026"  # по умолчанию
    elif len(date_parts) == 3:
        # Формат: ДД.ММ.ГГГГ
        day_month = date_parts[0] + date_parts[1]  # "1003"
        year = date_parts[2]  # "2026"
    else:
        year = "2026"  # fallback
    parsed_query['search_date'] = [day_month + year]

    # Собираем обратно
    new_query = urlencode(parsed_query, doseq=True)
    new_link = new_path
    if new_query:
        new_link += "?" + new_query

    return new_link

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
    Возвращает рейсы с готовыми ссылками 'link' для бронирования.
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

    # ⚠️ КРИТИЧЕСКИ ВАЖНО: указываем one_way=false для туда-обратно
    if return_date:
        params["return_date"] = return_date
        params["one_way"] = "false"
    else:
        params["one_way"] = "true"

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

                # Добавляем маркер ко всем ссылкам
                marker = os.getenv("TRAFFIC_SOURCE", "").strip()
                sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
                for flight in flights:
                    if flight.get("link"):
                        # ИСПОЛЬЗУЕМ ССЫЛКУ ИЗ API КАК ЕСТЬ
                        full_link = "https://www.aviasales.ru" + flight["link"]
                        flight["booking_url"] = add_marker_to_url(full_link, marker, sub_id)
                    elif flight.get("deep_link"):
                        flight["booking_url"] = add_marker_to_url(flight["deep_link"], marker, sub_id)
                    else:
                        # Резерв: генерируем базовую ссылку (редко нужно)
                        # (реализация внутри start.py)
                        pass

                return flights

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Aviasales API: {e}")
            return []

def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    """
    Генерирует базовую ссылку без дат — Aviasales сам подставит фильтры по дате.
    Это даёт максимальную точность: пользователь увидит те же цены, что и в боте.
    """
    # Формируем маршрут: ORIGDEST[PASS]
    route = f"{origin}{dest}{passengers_code}"
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