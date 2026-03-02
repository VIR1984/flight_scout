# utils/redis_client.py  (адаптирован из бота)
import os
import json
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self):
        self.client = None
        self.prefix = "scout_web:"

    async def connect(self):
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            logger.warning("REDIS_URL не задан — работаем без кэша")
            return
        try:
            from redis import asyncio as redis
            self.client = redis.from_url(redis_url, decode_responses=True)
            await self.client.ping()
            logger.info("✅ Redis подключён")
        except Exception as e:
            logger.error(f"Redis ошибка: {e}")
            self.client = None

    async def close(self):
        if self.client:
            await self.client.close()

    def is_enabled(self) -> bool:
        return self.client is not None

    # ── Сессии чата ────────────────────────────────────────────────

    async def get_session(self, session_id: str) -> Optional[Dict]:
        if not self.client:
            return None
        raw = await self.client.get(f"{self.prefix}session:{session_id}")
        return json.loads(raw) if raw else None

    async def set_session(self, session_id: str, data: Dict, ttl: int = 3600):
        if not self.client:
            return
        await self.client.setex(
            f"{self.prefix}session:{session_id}", ttl,
            json.dumps(data, ensure_ascii=False)
        )

    async def delete_session(self, session_id: str):
        if self.client:
            await self.client.delete(f"{self.prefix}session:{session_id}")

    # ── Кэш поиска ─────────────────────────────────────────────────

    async def get_search_cache(self, cache_key: str) -> Optional[Any]:
        if not self.client:
            return None
        raw = await self.client.get(f"{self.prefix}search:{cache_key}")
        return json.loads(raw) if raw else None

    async def set_search_cache(self, cache_key: str, data: Any, ttl: int = 1800):
        if not self.client:
            return
        await self.client.setex(
            f"{self.prefix}search:{cache_key}", ttl,
            json.dumps(data, ensure_ascii=False)
        )


redis_client = RedisClient()
