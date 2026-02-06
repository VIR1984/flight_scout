# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional
from utils.logger import logger
from datetime import datetime

def normalize_date(date_str: str) -> str:
    """Приводит дату к формату ГГГГ-ММ-ДД на основе текущей даты"""
    day, month = map(int, date_str.split('.'))
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    current_day = now.day
    
    # Если дата уже прошла в этом году — берём следующий год
    if (month < current_month) or (month == current_month and day < current_day):
        year = current_year + 1
    else:
        year = current_year
    
    return f"{year}-{month:02d}-{day:02d}"

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
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
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
