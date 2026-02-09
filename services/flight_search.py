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
                        from .flight_search import generate_fallback_link
                        flight["booking_url"] = generate_fallback_link(flight, origin, destination, depart_date, return_date)

                return flights

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Aviasales API: {e}")
            return []

# Резервная функция на случай отсутствия link/deep_link (не используется в нормальной работе)
def generate_fallback_link(flight: Dict, origin: str, dest: str, depart_date: str, return_date: Optional[str]) -> str:
    """Генерирует базовую ссылку без t-параметров (только маршрут)"""
    def format_date(d: str) -> str:
        return d.replace('.', '') if d else ''
    
    d1 = format_date(depart_date)
    d2 = format_date(return_date) if return_date else ''
    passengers = str(flight.get("number_of_adults", 1))
    route = f"{origin}{d1}{dest}{d2}{passengers}"
    base = f"https://www.aviasales.ru/search/{route}"
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        parsed = urlparse(base)
        query = parse_qs(parsed.query)
        query['marker'] = [marker]
        query['sub_id'] = [sub_id]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    return base