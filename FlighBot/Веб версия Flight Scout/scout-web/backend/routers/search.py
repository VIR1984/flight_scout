# routers/search.py
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
from services.flight_search import search_flights_realtime, generate_booking_link, normalize_date

router = APIRouter()


class SearchRequest(BaseModel):
    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    adults: int = 1
    children: int = 0
    infants: int = 0
    flight_type: str = "all"   # all | direct | transfer


@router.post("/flights")
async def search_flights(req: SearchRequest):
    """
    Поиск рейсов через Travelpayouts Real-time API.
    Возвращает список рейсов, отсортированных по цене.
    """
    try:
        flights = await search_flights_realtime(
            origin=req.origin.upper(),
            destination=req.destination.upper(),
            depart_date=normalize_date(req.depart_date),
            return_date=normalize_date(req.return_date) if req.return_date else None,
            adults=req.adults,
            children=req.children,
            infants=req.infants,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Фильтр по типу рейса
    if req.flight_type == "direct":
        flights = [f for f in flights if f.get("transfers", 0) == 0]
    elif req.flight_type == "transfer":
        flights = [f for f in flights if f.get("transfers", 0) > 0]

    return {"flights": flights, "total": len(flights)}


@router.get("/booking-link")
async def get_booking_link(
    origin: str, destination: str,
    depart_date: str, return_date: Optional[str] = None,
    passengers: str = "1"
):
    """Генерирует прямую ссылку на Aviasales."""
    link = generate_booking_link(
        flight={}, origin=origin, dest=destination,
        depart_date=depart_date, passengers_code=passengers,
        return_date=return_date
    )
    return {"link": link}
