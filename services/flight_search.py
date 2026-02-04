# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional
from datetime import datetime, timedelta

def normalize_date(date_str: str) -> str:
    try:
        d, m = date_str.split('.')
        day = int(d); month = int(m); year = datetime.now().year
        if month < datetime.now().month or (month == datetime.now().month and day < datetime.now().day):
            year += 1
        return f"{year}-{month:02d}-{day:02d}"
    except:
        # Если дата не указана — возвращаем ближайшую дату
        return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

async def search_flights(origin: str, dest: str, depart_date: Optional[str] = None, return_date: Optional[str] = None) -> List[Dict]:
    """Поиск рейсов на конкретные даты (ИСПРАВЛЕНО: требуется полная дата)"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    
    # Если дата не указана — используем завтрашний день
    if not depart_date:
        depart_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": depart_date,  # ← ИСПРАВЛЕНО: теперь полная дата YYYY-MM-DD
        "one_way": "false" if return_date else "true",
        "currency": "rub",
        "limit": 10,
        "sorting": "price",
        "token": os.getenv("API_TOKEN", "").strip()
    }
    
    if return_date:
        params["return_at"] = return_date
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("success"):
                    return data.get("data", [])
            return []

async def search_cheapest_flights(origin: str, dest: str) -> List[Dict]:
    """Поиск самых дешёвых билетов на ближайшие 30 дней"""
    all_flights = []
    
    # Формируем диапазон дат: сегодня + 30 дней
    start_date = datetime.now()
    end_date = start_date + timedelta(days=30)
    departure_range = f"{start_date.strftime('%Y-%m-%d')},{end_date.strftime('%Y-%m-%d')}"
    
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": departure_range,  # ← диапазон дат
        "one_way": "true",
        "currency": "rub",
        "limit": 20,
        "sorting": "price",
        "token": os.getenv("API_TOKEN", "").strip()
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("success"):
                    flights = data.get("data", [])
                    # Сортируем по цене и возвращаем топ-15
                    flights.sort(key=lambda f: f.get("value") or f.get("price") or 999999)
                    return flights[:15]
            return []

async def get_hot_offers(limit: int = 7) -> List[Dict]:
    """Горячие предложения из Москвы"""
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": "MOW",
        "departure_at": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") + "," + (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "one_way": "true",
        "currency": "rub",
        "limit": limit * 3,
        "unique": "true",
        "sorting": "price",
        "token": os.getenv("API_TOKEN", "").strip()
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status == 200:
                data = await r.json()
                if data.get("success"):
                    seen = set()
                    offers = []
                    for item in data.get("data", []):
                        route = f"{item['origin']}-{item['destination']}"
                        if route not in seen:
                            offers.append(item)
                            seen.add(route)
                        if len(offers) >= limit:
                            break
                    return offers
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