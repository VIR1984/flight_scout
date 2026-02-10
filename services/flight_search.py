import os
import asyncio
import aiohttp
import re
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime
from utils.logger import logger

# ================== CONFIG ==================

AVIASALES_GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()

# ================== DATES ==================

def normalize_date(date_str: str) -> str:
    """ДД.ММ → YYYY-MM-DD"""
    try:
        day, month = map(int, date_str.split('.'))
        year = 2026
        if month < 2 or (month == 2 and day < 8):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return date_str


def format_avia_link_date(date_str: str) -> str:
    """ДД.ММ → ДДММ"""
    try:
        day, month = date_str.split('.')
        return f"{day}{month}"
    except Exception:
        return date_str.replace('.', '')

# ================== MARKER ==================

def add_marker_to_url(url: str, marker: str, sub_id: str = "telegram") -> str:
    if not marker or not url:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["marker"] = [marker]
    query["sub_id"] = [sub_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

# ================== PASSENGERS ==================

def build_passengers_code(adults: int, children: int = 0, infants: int = 0) -> str:
    """
    Aviasales pax format:
    2   → 2 adults
    21  → 2 adults + 1 child
    211 → 2 adults + 1 child + 1 infant
    """
    adults = max(1, adults)
    infants = min(infants, adults)

    code = str(adults)
    if children > 0 or infants > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)

    return code


def patch_aviasales_passengers(link: str, passengers_code: str) -> str:
    """
    /search/MOW1003IST1 → /search/MOW211IST211
    """
    if not link or not passengers_code.isdigit():
        return link

    parsed = urlparse(link)
    path = parsed.path

    match = re.search(
        r"/search/([A-Z]{3})(\d+)([A-Z]{3})(\d+)",
        path
    )
    if not match:
        return link

    origin, _, dest, _ = match.groups()
    new_path = f"/search/{origin}{passengers_code}{dest}{passengers_code}"

    return urlunparse(parsed._replace(path=new_path))

# ================== LINKS ==================

def generate_booking_link(
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str,
    return_date: Optional[str] = None
) -> str:
    """
    Генерация fallback-ссылки Aviasales
    """
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    route = f"{origin}{d1}{dest}{d2}{passengers_code}"

    url = f"https://www.aviasales.ru/search/{route}"

    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()

    if marker:
        url = add_marker_to_url(url, marker, sub_id)

    return url


def build_final_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str,
    return_date: Optional[str] = None
) -> str:
    """
    ЕДИНАЯ точка получения ссылки
    1. Берём link/deep_link из API
    2. Чиним пассажиров
    3. Если нет — fallback
    """
    link = flight.get("link") or flight.get("deep_link")

    if link:
        link = patch_aviasales_passengers(link, passengers_code)
        if not link.startswith("http"):
            link = "https://www.aviasales.ru" + link
    else:
        link = generate_booking_link(
            origin=origin,
            dest=dest,
            depart_date=depart_date,
            passengers_code=passengers_code,
            return_date=return_date
        )

    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        link = add_marker_to_url(link, marker, sub_id)

    return link

# ================== SEARCH ==================

async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    direct: bool = False
) -> List[Dict]:

    if not AVIASALES_TOKEN:
        logger.warning("AVIASALES_TOKEN not set")
        return []

    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_date,
        "currency": currency,
        "token": AVIASALES_TOKEN,
        "group_by": "departure_at",
        "direct": "true" if direct else "false"
    }

    if return_date:
        params["return_at"] = return_date

    async with aiohttp.ClientSession() as session:
        async with session.get(AVIASALES_GROUPED_URL, params=params) as r:
            if r.status != 200:
                logger.error(await r.text())
                return []

            data = await r.json()
            if not data.get("success"):
                return []

            flights = []
            for _, flight in data.get("data", {}).items():
                flight["value"] = flight.get("price")
                flights.append(flight)

            return flights


def find_cheapest_flight_on_exact_date(
    flights: List[Dict],
    depart_date: str,
    return_date: Optional[str] = None
) -> Optional[Dict]:

    if not flights:
        return None

    target_depart = normalize_date(depart_date)
    target_return = normalize_date(return_date) if return_date else None

    exact = []
    for f in flights:
        if f.get("departure_at", "")[:10] != target_depart:
            continue
        if target_return and f.get("return_at", "")[:10] != target_return:
            continue
        exact.append(f)

    return min(exact or flights, key=lambda f: f.get("value", 10**9))
