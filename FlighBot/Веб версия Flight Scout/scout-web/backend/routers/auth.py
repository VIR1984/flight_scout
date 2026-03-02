# routers/auth.py
import uuid
from fastapi import APIRouter, Response, Cookie
from typing import Optional

router = APIRouter()


@router.post("/session")
async def create_session(response: Response):
    """Создаёт анонимную сессию (UUID) и кладёт в cookie."""
    session_id = str(uuid.uuid4())
    response.set_cookie(
        key="scout_session",
        value=session_id,
        max_age=86400 * 30,   # 30 дней
        httponly=True,
        samesite="lax",
    )
    return {"session_id": session_id}


@router.get("/me")
async def get_session(scout_session: Optional[str] = Cookie(None)):
    """Возвращает текущую сессию."""
    if not scout_session:
        return {"session_id": None, "authenticated": False}
    return {"session_id": scout_session, "authenticated": True}
