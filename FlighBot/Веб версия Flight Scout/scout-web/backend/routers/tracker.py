# routers/tracker.py
import json
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
from utils.redis_client import redis_client
from utils.logger import logger

router = APIRouter()


class TrackerAdd(BaseModel):
    origin: str
    destination: str
    depart_date: str
    return_date: Optional[str] = None
    adults: int = 1
    budget: Optional[int] = None  # Уведомлять если цена ниже


@router.post("/add")
async def add_tracker(req: TrackerAdd, x_session_id: str = Header(...)):
    """Добавляет маршрут в отслеживание для текущей сессии."""
    key = f"tracker:{x_session_id}"
    existing_raw = await redis_client.client.get(key) if redis_client.client else None
    existing = json.loads(existing_raw) if existing_raw else []

    item = req.dict()
    item["id"] = f"{req.origin}-{req.destination}-{req.depart_date}"

    # Не дублировать
    if not any(t["id"] == item["id"] for t in existing):
        existing.append(item)

    if redis_client.client:
        await redis_client.client.setex(key, 86400 * 30, json.dumps(existing))

    return {"ok": True, "trackers": existing}


@router.get("/list")
async def list_trackers(x_session_id: str = Header(...)):
    """Возвращает список отслеживаемых маршрутов."""
    key = f"tracker:{x_session_id}"
    raw = await redis_client.client.get(key) if redis_client.client else None
    trackers = json.loads(raw) if raw else []
    return {"trackers": trackers}


@router.delete("/{tracker_id}")
async def remove_tracker(tracker_id: str, x_session_id: str = Header(...)):
    """Удаляет маршрут из отслеживания."""
    key = f"tracker:{x_session_id}"
    raw = await redis_client.client.get(key) if redis_client.client else None
    trackers = json.loads(raw) if raw else []
    trackers = [t for t in trackers if t["id"] != tracker_id]
    if redis_client.client:
        await redis_client.client.setex(key, 86400 * 30, json.dumps(trackers))
    return {"ok": True, "trackers": trackers}
