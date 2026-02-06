# utils/redis_client.py
import os
import json
import time
import logging
from typing import Optional, Dict, Any, List
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

    # ===== Отслеживание цен =====
    async def save_price_watch(
        self,
        user_id: int,
        origin: str,
        dest: str,
        depart_date: str,
        return_date: Optional[str],
        current_price: int,
        passengers: str = "1",
        threshold: int = 0  # ← НОВЫЙ ПАРАМЕТР (0 = любое, 100 = сотни, 1000 = тысячи)
    ) -> str:
        """Сохранить отслеживание цены. Возвращает ключ"""
        key = f"{self.prefix}watch:{user_id}:{origin}:{dest}:{depart_date}"
        if return_date:
            key += f":{return_date}"
        data = {
            "origin": origin,
            "dest": dest,
            "depart_date": depart_date,
            "return_date": return_date,
            "current_price": current_price,
            "passengers": passengers,
            "user_id": user_id,
            "threshold": threshold,  # ← СОХРАНЯЕМ ПОРОГ
            "created_at": int(time.time())
        }
        await self.client.setex(key, 86400 * 30, json.dumps(data, ensure_ascii=False))  # 30 дней
        await self.client.sadd(f"{self.prefix}user:watches:{user_id}", key)
        return key

    async def get_user_watches(self, user_id: int) -> List[Dict[str, Any]]:
        """Получить все отслеживания пользователя"""
        if not self.client:
            return []
        keys = await self.client.smembers(f"{self.prefix}user:watches:{user_id}")
        watches = []
        for key in keys:
            data = await self.client.get(key)
            if data:
                watches.append(json.loads(data))
        return watches

    async def remove_watch(self, user_id: int, watch_key: str):
        """Удалить отслеживание"""
        if not self.client:
            return
        await self.client.delete(watch_key)
        await self.client.srem(f"{self.prefix}user:watches:{user_id}", watch_key)

    async def get_all_watch_keys(self) -> List[str]:
        """Получить все ключи отслеживаний для фоновой проверки"""
        if not self.client:
            return []
        pattern = f"{self.prefix}watch:*"
        cursor = "0"
        keys = []
        while cursor != 0:
            cursor, batch = await self.client.scan(cursor=cursor, match=pattern, count=100)
            keys.extend(batch)
        return keys

# Singleton
redis_client = RedisClient()