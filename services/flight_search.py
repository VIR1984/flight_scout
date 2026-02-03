# services/flight_search.py
import aiohttp
import os
from typing import List, Dict, Optional

def normalize_date(date_str: str) -> str:
    try:
        d, m = date_str.split('.')
        day = int(d); month = int(m); year = 2026
        if month < 2 or (month == 2 and day < 3):  # сегодня 03.02.2026
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except:
        return "2026-03-15"

async def search_flights(origin: str, dest: str, depart_date: str, return_date: Optional[str] = None) -> List[Dict]:
    url = "https://api.travelpayouts.com/v1/prices/cheap"
    params = {
        "origin": origin,
        "destination": dest,
        "depart_date": normalize_date(depart_date),
        "currency": "RUB",
        "token": os.getenv("API_TOKEN")
    }
    if return_date:
        params["return_date"] = normalize_date(return_date)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status == 200:
                data = await r.json()
                # Для обратных билетов структура та же — API возвращает их автоматически при наличии return_date
                return list(data.get("data", {}).get(dest, {}).values())
    return []

def format_avia_link_date(date_str: str) -> str:
    try:
        d, m = date_str.split('.')
        return f"{int(d):02d}{int(m):02d}"
    except:
        return "1503"

def generate_booking_link(flight: dict, origin: str, dest: str, depart_date: str, passengers_code: str = "1", return_date: Optional[str] = None) -> str:
    marker = os.getenv("TRAFFIC_SOURCE")
    d1 = format_avia_link_date(depart_date)
    if return_date:
        d2 = format_avia_link_date(return_date)
        route = f"{origin}{d1}{dest}{d2}"
    else:
        route = f"{origin}{d1}{dest}"
    return f"https://www.aviasales.ru/search/{route}{passengers_code}?marker={marker}"