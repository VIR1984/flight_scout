import os
import asyncio
import aiohttp
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Конфигурация API
AVIASALES_API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()

def normalize_date(date_str: str) -> str:
    """Преобразует дату ДД.ММ в формат ГГГГ-ММ-ДД для 2026 года (или 2027 для январь/февраль)"""
    try:
        day, month = map(int, date_str.split('.'))
        year = 2026
        # Если дата в будущем относительно февраля 2026 — используем 2027
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
        raise ValueError("AVIASALES_TOKEN не установлен в переменных окружения")
    
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
                    print(f"❌ Ошибка API Aviasales: {response.status} - {error_text}")
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
            print("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            print(f"❌ Ошибка при запросе к Aviasales API: {e}")
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
    Генерирует ссылку для бронирования на Aviasales с маркером.
    
    Args:
        flight: данные о рейсе из API
        origin: IATA код вылета
        dest: IATA код прилёта
        depart_date: дата вылета в формате 'ДД.ММ'
        passengers_code: код пассажиров (например, '12' = 1 взр + 2 реб)
        return_date: дата возврата в формате 'ДД.ММ' (опционально)
    
    Returns:
        Полная ссылка с маркером
    """
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    base_url = f"https://www.aviasales.ru/search/{route}"
    
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    # ИСПРАВЛЕНО: проверяем просто наличие маркера, а не только цифры
    if marker:
        return add_marker_to_url(base_url, marker, sub_id)
    
    return base_url