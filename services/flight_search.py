# services/flight_search.py
import aiohttp
import os
from typing import List, Dict
from datetime import datetime


def normalize_date(date_str: str) -> str:
    """Преобразует ДД.ММ → YYYY-MM-DD (с учётом текущей даты 02.02.2026)"""
    try:
        d, m = date_str.split('.')
        day = int(d)
        month = int(m)
        year = 2026  # текущий год
        
        # Если дата уже прошла в этом году — ищем на 2027
        if month < 2 or (month == 2 and day < 2):
            year = 2027
            
        return f"{year}-{str(month).zfill(2)}-{str(day).zfill(2)}"
    except:
        return "2026-03-15"

async def search_one_way(origin: str, dest: str, date: str = "15.03") -> List[Dict]:
    url = "https://api.travelpayouts.com/v1/prices/cheap"
    params = {
    "origin": origin,
    "destination": dest,
    "depart_date": normalize_date(date),  # ← должно быть 2026-02-16
    "currency": "USD",
    "token": os.getenv("API_TOKEN")
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            if r.status == 200:
                data = await r.json()
                return list(data.get("data", {}).get(dest, {}).values())
    return []
    
def format_avia_link_date(date_str: str) -> str:
    """Преобразует 15.02 → 1502 (только день и месяц)"""
    try:
        d, m = date_str.split('.')
        return f"{d.zfill(2)}{m.zfill(2)}"
    except:
        return "1503"

def generate_booking_link(flight: dict, origin: str, dest: str, date_str: str) -> str:
    marker = os.getenv("TRAFFIC_SOURCE")
    d = format_avia_link_date(date_str)  # Например: "1502"
    return f"https://www.aviasales.ru/search/{origin}{d}{dest}1?marker={marker}"