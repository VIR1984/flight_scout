# routers/deals.py
import os
import aiohttp
from fastapi import APIRouter, Query
from typing import Optional
from utils.logger import logger

router = APIRouter()

AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "")

CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",    ["AYT","HRG","SSH","RHO","DLM","AER","SIP"]),
    "world":  ("🌍 Путешествия по миру", ["DXB","BKK","SIN","ICN","TYO","DEL","JFK"]),
    "russia": ("🇷🇺 По России",          ["AER","LED","KZN","OVB","SVX","ROV","KRR"]),
}


@router.get("/hot")
async def get_hot_deals(
    origin: str = Query("MOW", description="IATA аэропорта вылета"),
    category: Optional[str] = Query(None, description="sea|world|russia"),
    limit: int = Query(12, le=50),
):
    """
    Возвращает горячие предложения через Aviasales Data API (grouped_prices).
    Для каждого популярного направления запрашивает минимальную цену.
    """
    if not AVIASALES_TOKEN:
        # Возвращаем демо-данные если токен не настроен
        return {"deals": _demo_deals()}

    cat_destinations = []
    if category and category in CATEGORIES:
        cat_destinations = CATEGORIES[category][1]
    else:
        for _, (_, dests) in CATEGORIES.items():
            cat_destinations.extend(dests)

    deals = []
    async with aiohttp.ClientSession() as session:
        for dest in cat_destinations[:limit]:
            try:
                async with session.get(
                    "https://api.travelpayouts.com/aviasales/v3/grouped_prices",
                    params={"origin": origin, "destination": dest,
                            "currency": "rub", "token": AVIASALES_TOKEN,
                            "group_by": "month"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data.get("success"):
                        continue
                    for month_key, flight in (data.get("data") or {}).items():
                        deals.append({
                            "origin":       origin,
                            "destination":  dest,
                            "price":        flight.get("price", 0),
                            "depart_date":  flight.get("departure_at", "")[:10],
                            "return_date":  flight.get("return_at", "")[:10] or None,
                            "airline":      flight.get("airline", ""),
                            "transfers":    flight.get("transfers", 0),
                            "link":         flight.get("link", ""),
                        })
                        break  # берём первый (самый дешёвый)
            except Exception as e:
                logger.warning(f"[deals] {dest}: {e}")
                continue

    deals.sort(key=lambda d: d["price"])
    return {"deals": deals[:limit]}


def _demo_deals():
    return [
        {"origin":"MOW","destination":"AER","price":3800,"depart_date":"2026-03-22","airline":"SU","transfers":0,"link":"https://aviasales.ru"},
        {"origin":"MOW","destination":"AYT","price":6900,"depart_date":"2026-04-05","airline":"PC","transfers":0,"link":"https://aviasales.ru"},
        {"origin":"LED","destination":"DXB","price":11200,"depart_date":"2026-04-10","airline":"FZ","transfers":0,"link":"https://aviasales.ru"},
        {"origin":"MOW","destination":"BKK","price":24500,"depart_date":"2026-05-01","airline":"TG","transfers":1,"link":"https://aviasales.ru"},
        {"origin":"MOW","destination":"HRG","price":9800,"depart_date":"2026-04-15","airline":"MS","transfers":0,"link":"https://aviasales.ru"},
        {"origin":"MOW","destination":"LED","price":2100,"depart_date":"2026-03-25","airline":"SU","transfers":0,"link":"https://aviasales.ru"},
    ]
