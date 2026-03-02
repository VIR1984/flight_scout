# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

def normalize_date(date_str: str) -> str:
    """Преобразует ДД.ММ в ГГГГ-ММ-ДД (ближайшая дата в будущем)"""
    try:
        d, m = map(int, date_str.split('.'))
        now = datetime.now()
        year = now.year
        
        # Если дата уже прошла в этом году — берём следующий год
        if m < now.month or (m == now.month and d <= now.day):
            year += 1
        
        return f"{year}-{m:02d}-{d:02d}"
    except Exception as e:
        logger.warning(f"Ошибка парсинга даты '{date_str}': {e}")
        # Возвращаем завтрашнюю дату как фолбэк
        tomorrow = datetime.now() + timedelta(days=1)
        return tomorrow.strftime("%Y-%m-%d")

async def search_flights(origin: str, dest: str, depart_date: str, return_date: Optional[str] = None) -> List[Dict]:
    """Поиск рейсов на конкретные даты"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    token = os.getenv("API_TOKEN", "").strip()
    
    if not token:
        logger.error("API_TOKEN не найден в .env!")
        return []
    
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": depart_date,
        "one_way": "false" if return_date else "true",
        "currency": "rub",
        "limit": 10,
        "sorting": "price",
        "token": token
    }
    
    if return_date:
        params["return_at"] = return_date
    
    logger.debug(f"Запрос к API: {url} | Параметры: {params}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as r:
                logger.debug(f"Статус ответа API: {r.status}")
                if r.status == 200:
                    data = await r.json()
                    logger.debug(f"Ответ API: {data}")
                    
                    if not data.get("success"):
                        logger.warning(f"API вернул ошибку: {data.get('error', 'неизвестная ошибка')}")
                        return []
                    
                    flights = data.get("data", [])
                    logger.info(f"Найдено рейсов: {len(flights)}")
                    return flights
                else:
                    logger.error(f"Ошибка API: {r.status} - {await r.text()}")
                    return []
    except Exception as e:
        logger.exception(f"Исключение при запросе к API: {e}")
        return []

async def search_cheapest_flights(origin: str, dest: str) -> List[Dict]:
    """Поиск самых дешёвых билетов на ближайшие 30 дней"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    token = os.getenv("API_TOKEN", "").strip()
    
    if not token:
        logger.error("API_TOKEN не найден в .env!")
        return []
    
    # Диапазон: завтра + 30 дней
    start_date = datetime.now() + timedelta(days=1)
    end_date = start_date + timedelta(days=30)
    departure_range = f"{start_date.strftime('%Y-%m-%d')},{end_date.strftime('%Y-%m-%d')}"
    
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": departure_range,
        "one_way": "true",
        "currency": "rub",
        "limit": 20,
        "sorting": "price",
        "token": token
    }
    
    logger.debug(f"Запрос дешёвых билетов: {url} | Диапазон: {departure_range}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as r:
                logger.debug(f"Статус ответа (дешёвые): {r.status}")
                if r.status == 200:
                    data = await r.json()
                    logger.debug(f"Ответ API (дешёвые): {data}")
                    
                    if not data.get("success"):
                        logger.warning(f"API вернул ошибку: {data.get('error', 'неизвестная ошибка')}")
                        return []
                    
                    flights = data.get("data", [])
                    logger.info(f"Найдено дешёвых рейсов: {len(flights)}")
                    # Сортируем по цене и возвращаем топ-15
                    flights.sort(key=lambda f: f.get("value") or f.get("price") or 999999)
                    return flights[:15]
                else:
                    logger.error(f"Ошибка API (дешёвые): {r.status} - {await r.text()}")
                    return []
    except Exception as e:
        logger.exception(f"Исключение при запросе дешёвых билетов: {e}")
        return []

async def get_hot_offers(limit: int = 7) -> List[Dict]:
    """Горячие предложения из Москвы на ближайшие дни"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    token = os.getenv("API_TOKEN", "").strip()
    
    if not token:
        logger.error("API_TOKEN не найден в .env!")
        return []
    
    # Диапазон: завтра + 14 дней
    start_date = datetime.now() + timedelta(days=1)
    end_date = start_date + timedelta(days=14)
    departure_range = f"{start_date.strftime('%Y-%m-%d')},{end_date.strftime('%Y-%m-%d')}"
    
    params = {
        "origin": "MOW",
        "departure_at": departure_range,
        "one_way": "true",
        "currency": "rub",
        "limit": limit * 3,
        "unique": "true",
        "sorting": "price",
        "token": token
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as r:
                if r.status == 200:
                    data = await r.json()
                    if data.get("success"):
                        seen = set()
                        offers = []
                        for item in data.get("data", []):
                            route = f"{item['origin']}-{item['destination']}"
                            if route not in seen and item.get("value"):
                                offers.append(item)
                                seen.add(route)
                            if len(offers) >= limit:
                                break
                        return offers
        return []
    except Exception as e:
        logger.exception(f"Ошибка при получении горячих предложений: {e}")
        return []

def generate_booking_link(flight: dict, origin: str, dest: str, depart_date: str, passengers_code: str = "1", return_date: Optional[str] = None) -> str:
    """Генерация ссылки для бронирования"""
    link_suffix = flight.get("link", "")
    marker = os.getenv("TRAFFIC_SOURCE", "")
    base = "https://www.aviasales.ru"
    full_url = base + link_suffix
    if marker:
        full_url += f"&marker={marker}"
    return full_url