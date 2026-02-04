# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional
from utils.logger import logger

def normalize_date(date_str: str) -> str:
    try:
        d, m = date_str.split('.')
        day = int(d)
        month = int(m)
        year = 2026
        if month < 2 or (month == 2 and day < 3):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except Exception as e:
        logger.warning(f"Ошибка парсинга даты '{date_str}': {e}")
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
        "direct": "false",
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

def generate_booking_link(flight: dict, origin: str, dest: str, depart_date: str, passengers_code: str = "1", return_date: Optional[str] = None) -> str:
    link_suffix = flight.get("link", "")
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    base = "https://www.aviasales.ru"
    full_url = base + link_suffix

    if marker:
        if "?" in full_url:
            full_url += f"&marker={marker}"
        else:
            full_url += f"?marker={marker}"
    return full_url