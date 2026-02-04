# utils/redis_client.py
import os
import json
import logging
from typing import Optional, Dict, Any
from redis import asyncio as redis  # redis 4.6 async

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class RedisClient:
    def __init__(self):
        self.client: Optional[redis.Redis] = None
        self.prefix = "flight_bot:"

    async def connect(self):
        """Подключение к Redis"""
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            logger.warning("REDIS_URL не задан — Redis отключён")
            return

        try:
            # ⚡ Для rediss:// SSL включается автоматически
            self.client = redis.from_url(
                redis_url,
                decode_responses=True,
            )
            await self.client.ping()
            logger.info("✓ Redis подключён")
        except Exception as e:
            logger.error(f"Ошибка подключения к Redis: {e}")
            self.client = None

    async def close(self):
        """Закрытие соединения"""
        if self.client:
            await self.client.close()

    def is_enabled(self) -> bool:
        return self.client is not None

    # ===== Кэш поиска =====
    async def get_search_cache(self, cache_id: str) -> Optional[Dict[str, Any]]:
        if not self.client:
            return None
        data = await self.client.get(f"{self.prefix}search:{cache_id}")
        return json.loads(data) if data else None

    async def set_search_cache(self, cache_id: str, data: Dict[str, Any], ttl: int = 3600):
        if not self.client:
            return
        await self.client.setex(
            f"{self.prefix}search:{cache_id}",
            ttl,
            json.dumps(data, ensure_ascii=False),
        )

    async def delete_search_cache(self, cache_id: str):
        if self.client:
            await self.client.delete(f"{self.prefix}search:{cache_id}")

    # ===== Первый запуск пользователя =====
    async def is_first_time_user(self, user_id: int) -> bool:
        if not self.client:
            return True
        key = f"{self.prefix}first_time_users"
        exists = await self.client.sismember(key, str(user_id))
        if not exists:
            await self.client.sadd(key, str(user_id))
        return not exists


# Singleton
redis_client = RedisClient()
