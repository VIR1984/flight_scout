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

def format_avia_link_date(date_str: str) -> str:
    """Преобразует '10.03' → '1003'"""
    try:
        d, m = date_str.split('.')
        return f"{int(d):02d}{int(m):02d}"
    except:
        return "0101"

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
