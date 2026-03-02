"""
Scout Web — FastAPI Backend
Переносит логику Telegram-бота в REST + WebSocket API.
"""

import os
import uuid
import json
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from routers import search, deals, tracker, auth
from utils.redis_client import redis_client
from utils.logger import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    await redis_client.connect()
    logger.info("✅ Scout Web запущен")
    yield
    await redis_client.close()
    logger.info("Scout Web остановлен")


app = FastAPI(
    title="Scout Web API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST роуты
app.include_router(auth.router,    prefix="/api/auth",    tags=["auth"])
app.include_router(search.router,  prefix="/api/search",  tags=["search"])
app.include_router(deals.router,   prefix="/api/deals",   tags=["deals"])
app.include_router(tracker.router, prefix="/api/tracker", tags=["tracker"])


# ── WebSocket: сессия чата ──────────────────────────────────────────
class SessionManager:
    """Хранит активные WS-сессии в памяти."""
    def __init__(self):
        self.connections: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket):
        await ws.accept()
        self.connections[session_id] = ws
        logger.info(f"WS connect: {session_id}")

    def disconnect(self, session_id: str):
        self.connections.pop(session_id, None)
        logger.info(f"WS disconnect: {session_id}")

    async def send(self, session_id: str, data: dict):
        ws = self.connections.get(session_id)
        if ws:
            await ws.send_text(json.dumps(data, ensure_ascii=False))


manager = SessionManager()


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """
    WebSocket для чата. Клиент шлёт JSON:
        {"type": "message", "text": "Москва Сочи 15.03"}
    Сервер отвечает:
        {"type": "typing"}
        {"type": "message", "text": "...", "flights": [...]}
    """
    from services.chat_handler import handle_user_message

    await manager.connect(session_id, websocket)
    # Восстановить состояние из Redis
    state = await redis_client.get_session(session_id) or {}

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            msg_type = payload.get("type")

            if msg_type == "message":
                text = payload.get("text", "").strip()
                if not text:
                    continue

                # Показать "печатает..."
                await manager.send(session_id, {"type": "typing"})

                # Обработать сообщение через логику бота
                response = await handle_user_message(text, state, session_id)

                # Сохранить обновлённое состояние
                await redis_client.set_session(session_id, state)

                await manager.send(session_id, response)

            elif msg_type == "reset":
                state.clear()
                await redis_client.delete_session(session_id)
                await manager.send(session_id, {
                    "type": "message",
                    "text": "Начнём заново! Откуда летим?",
                    "buttons": ["✏️ Ввести маршрут", "🌍 Куда угодно", "🔥 Горящие"]
                })

    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception as e:
        logger.error(f"WS error [{session_id}]: {e}")
        manager.disconnect(session_id)
