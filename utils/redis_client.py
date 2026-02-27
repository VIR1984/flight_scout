# utils/redis_client.py
import os
import uuid
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
        threshold: int = 0
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
            "threshold": threshold,
            "created_at": int(time.time())
        }
        await self.client.setex(key, 86400 * 30, json.dumps(data, ensure_ascii=False))
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

    # ===== FlyStack usage tracking =====
    async def get_flystack_usage(self, user_id: int, month: str) -> int:
        """Получить количество использованных запросов FlyStack за месяц"""
        if not self.client:
            return 0
        key = f"{self.prefix}flystack:{user_id}:{month}"
        count = await self.client.get(key)
        return int(count) if count else 0

    async def increment_flystack_usage(self, user_id: int, month: str, limit: int = 3) -> bool:
        """Увеличить счётчик. Возвращает True если лимит не превышен."""
        if not self.client:
            return True
        key = f"{self.prefix}flystack:{user_id}:{month}"
        current = await self.client.get(key)
        current = int(current) if current else 0
        if current >= limit:
            return False
        await self.client.incr(key)
        await self.client.expire(key, 86400 * 35)
        return True

    # ===== Горячие предложения / Дайджест =====

    async def save_hot_sub(self, user_id: int, sub: dict) -> str:
        """Сохранить горячую/дайджест-подписку. Возвращает sub_id."""
        if not self.client:
            return ""
        sub_id = str(uuid.uuid4())[:8]
        key = f"{self.prefix}hotsub:{user_id}:{sub_id}"
        ttl = 86400 * 180  # 180 дней
        await self.client.setex(key, ttl, json.dumps(sub, ensure_ascii=False))
        await self.client.sadd(f"{self.prefix}hotsubs:{user_id}", sub_id)
        await self.client.sadd(f"{self.prefix}hotsubs_all", key)
        logger.info(f"✅ [HotSub] Сохранена подписка {sub_id} для {user_id}")
        return sub_id

    async def get_hot_subs(self, user_id: int) -> dict:
        """Вернуть все подписки пользователя: {sub_id: sub_data}."""
        if not self.client:
            return {}
        sub_ids = await self.client.smembers(f"{self.prefix}hotsubs:{user_id}")
        result = {}
        for sid in sub_ids:
            key = f"{self.prefix}hotsub:{user_id}:{sid}"
            raw = await self.client.get(key)
            if raw:
                result[sid] = json.loads(raw)
            else:
                await self.client.srem(f"{self.prefix}hotsubs:{user_id}", sid)
        return result

    async def get_all_hot_subs(self) -> list:
        """Вернуть все подписки всех пользователей: [(user_id, sub_id, sub_data), ...]."""
        if not self.client:
            return []
        all_keys = await self.client.smembers(f"{self.prefix}hotsubs_all")
        result = []
        dead_keys = []
        for key in all_keys:
            raw = await self.client.get(key)
            if not raw:
                dead_keys.append(key)
                continue
            try:
                sub = json.loads(raw)
                # key = flight_bot:hotsub:{user_id}:{sub_id}
                parts = key.split(":")
                user_id = int(parts[-2])
                sub_id = parts[-1]
                result.append((user_id, sub_id, sub))
            except Exception:
                dead_keys.append(key)
        if dead_keys:
            await self.client.srem(f"{self.prefix}hotsubs_all", *dead_keys)
        return result

    async def update_hot_sub(self, user_id: int, sub_id: str, sub: dict):
        """Обновить данные подписки (например, last_notified)."""
        if not self.client:
            return
        key = f"{self.prefix}hotsub:{user_id}:{sub_id}"
        ttl = 86400 * 180
        await self.client.setex(key, ttl, json.dumps(sub, ensure_ascii=False))

    async def delete_hot_sub(self, user_id: int, sub_id: str):
        """Удалить подписку."""
        if not self.client:
            return
        key = f"{self.prefix}hotsub:{user_id}:{sub_id}"
        await self.client.delete(key)
        await self.client.srem(f"{self.prefix}hotsubs:{user_id}", sub_id)
        await self.client.srem(f"{self.prefix}hotsubs_all", key)
        logger.info(f"🗑️ [HotSub] Удалена подписка {sub_id} пользователя {user_id}")

    # ===== Базовые цены маршрутов =====

    async def get_baseline_price(self, origin: str, dest: str) -> Optional[float]:
        """
        Возвращает сохранённую базовую цену для маршрута origin→dest.
        Это скользящее среднее по последним наблюдениям.
        """
        if not self.client:
            return None
        key = f"{self.prefix}baseline:{origin}:{dest}"
        raw = await self.client.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return float(data["avg"])
        except Exception:
            return None

    async def update_baseline_price(
        self,
        origin: str,
        dest: str,
        new_price: float,
        ttl: int = 86400 * 30,   # 30 дней — чтобы не устаревало навсегда
        alpha: float = 0.3,       # вес нового наблюдения в скользящем среднем
    ) -> float:
        """
        Обновляет базовую цену маршрута по формуле экспоненциального
        скользящего среднего (EMA):
            avg = alpha * new_price + (1 - alpha) * old_avg

        Первое наблюдение сохраняется как есть.
        Возвращает актуальное среднее после обновления.
        """
        if not self.client:
            return new_price
        key = f"{self.prefix}baseline:{origin}:{dest}"
        existing = await self.get_baseline_price(origin, dest)
        if existing is None:
            avg = new_price
        else:
            avg = alpha * new_price + (1 - alpha) * existing
        await self.client.set(key, json.dumps({"avg": round(avg, 2)}), ex=ttl)
        return avg


# Singleton
redis_client = RedisClient()