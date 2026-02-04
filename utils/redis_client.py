import os
import json
from typing import Optional, Dict, Any

from redis import asyncio as aioredis


class RedisClient:
    def __init__(self) -> None:
        self.client: aioredis.Redis | None = None
        self.prefix = "flight_bot:"

    # ===================== CONNECTION =====================

    async def connect(self) -> None:
        """
        Подключение к Redis (поддерживает redis:// и rediss://)
        """
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

        self.client = aioredis.from_url(
            redis_url,
            decode_responses=True,
            ssl_cert_reqs=None,   # безопасно для cloud / rediss://
            max_connections=20
        )

        await self.client.ping()
        print("✓ Redis подключен")

    async def close(self) -> None:
        """
        Корректное закрытие соединения и пула
        """
        if self.client:
            await self.client.aclose()
            await self.client.connection_pool.disconnect()

    # ===================== SEARCH CACHE =====================

    async def get_search_cache(
        self,
        cache_id: str
    ) -> Optional[Dict[str, Any]]:
        key = f"{self.prefix}search:{cache_id}"
        data = await self.client.get(key)
        return json.loads(data) if data else None

    async def set_search_cache(
        self,
        cache_id: str,
        data: Dict[str, Any],
        ttl: int = 3600
    ) -> None:
        key = f"{self.prefix}search:{cache_id}"
        await self.client.setex(
            key,
            ttl,
            json.dumps(data, ensure_ascii=False)
        )

    async def delete_search_cache(self, cache_id: str) -> None:
        key = f"{self.prefix}search:{cache_id}"
        await self.client.delete(key)

    # ===================== FIRST TIME USER =====================

    async def is_first_time_user(self, user_id: int) -> bool:
        """
        True — если пользователь первый раз
        False — если уже был
        """
        key = f"{self.prefix}first_time_users"
        user_id_str = str(user_id)

        exists = await self.client.sismember(key, user_id_str)
        if not exists:
            await self.client.sadd(key, user_id_str)

        return not exists

    # ===================== MONITORING =====================

    async def get_search_cache_count(self) -> int:
        """
        Количество активных кэшей поиска
        (для отладки / мониторинга)
        """
        keys = await self.client.keys(f"{self.prefix}search:*")
        return len(keys)


# ===================== SINGLETON =====================

redis_client = RedisClient()
