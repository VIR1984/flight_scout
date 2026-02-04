# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional
from datetime import datetime
from utils.logger import logger

def normalize_date(date_str: str) -> str:
    try:
        d, m = date_str.split('.')
        day = int(d); month = int(m); year = 2026
        if month < 2 or (month == 2 and day < 3):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except:
        return "2026-03-15"

async def search_flights(origin: str, dest: str, depart_date: str, return_date: Optional[str] = None) -> List[Dict]:
    logger.info(f"🔍 Запрос: {origin} → {dest}, вылет: {depart_date}, возврат: {return_date}")
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": origin,
        "destination": dest,
        "departure_at": depart_date,
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
            logger.info(f"📡 Ответ API: статус={r.status}")
            if r.status == 200:
                data = await r.json()
                success = data.get("success")
                logger.info(f"✅ Успешный ответ: {success}, найдено записей: {len(data.get('data', []))}")
                if success:
                    return data.get("data", [])
                else:
                    logger.warning(f"❌ API вернул ошибку: {data.get('message', 'no message')}")
            else:
                logger.error(f"💥 Ошибка HTTP: {r.status}")
            return []

async def get_hot_offers(limit: int = 7) -> List[Dict]:
    # Для горячих предложений тоже используем v3, но с origin=MOW и без destination
    url = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
    params = {
        "origin": "MOW",
        "departure_at": "2026-02",  # ближайший месяц
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
    # Используем готовую ссылку из API
    link_suffix = flight.get("link", "")
    marker = os.getenv("TRAFFIC_SOURCE", "")
    base = "https://www.aviasales.ru"
    full_url = base + link_suffix
    if marker:
        full_url += f"&marker={marker}"
    return full_url