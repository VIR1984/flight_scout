# utils/redis_client.py
"""
Клиент для работы с Redis: кэширование поиска и отслеживание цен
"""
import os
import json
import time
import logging
from typing import Optional, Dict, Any, List
from redis import asyncio as redis  # redis 4.6 async

logger = logging.getLogger(__name__)

class RedisClient:
    """Singleton-клиент для работы с Redis"""
    
    def __init__(self):
        self.client: Optional[redis.Redis] = None
        self.prefix = "flight_bot:"

    async def connect(self):
        """Подключение к Redis"""
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            logger.warning("⚠️ REDIS_URL не задан — кэширование отключено")
            return
        
        try:
            # Для rediss:// SSL включается автоматически
            self.client = redis.from_url(
                redis_url,
                decode_responses=True,
            )
            await self.client.ping()
            logger.info("✅ Redis подключён")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к Redis: {e}")
            self.client = None

    async def close(self):
        """Закрытие соединения"""
        if self.client:
            await self.client.close()
            logger.info("✅ Redis соединение закрыто")

    def is_enabled(self) -> bool:
        """Проверка, включён ли Redis"""
        return self.client is not None

    # ===== Кэш поиска =====
    async def get_search_cache(self, cache_id: str) -> Optional[Dict[str, Any]]:
        """
        Получить кэш поиска по идентификатору
        
        Args:
            cache_id: UUID кэша
            
        Returns:
            Данные кэша или None
        """
        if not self.client:
            return None
        
        try:
            data = await self.client.get(f"{self.prefix}search:{cache_id}")
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"❌ Ошибка при получении кэша {cache_id}: {e}")
            return None

    async def set_search_cache(self, cache_id: str, data: Dict[str, Any], ttl: int = 3600):
        """
        Сохранить кэш поиска
        
        Args:
            cache_id: UUID кэша
            data: данные для сохранения
            ttl: время жизни в секундах (по умолчанию 1 час)
        """
        if not self.client:
            return
        
        try:
            await self.client.setex(
                f"{self.prefix}search:{cache_id}",
                ttl,
                json.dumps(data, ensure_ascii=False),
            )
            logger.debug(f"💾 Кэш сохранён: {cache_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении кэша {cache_id}: {e}")

    async def delete_search_cache(self, cache_id: str):
        """
        Удалить кэш поиска
        
        Args:
            cache_id: UUID кэша
        """
        if not self.client:
            return
        
        try:
            await self.client.delete(f"{self.prefix}search:{cache_id}")
            logger.debug(f"🗑️ Кэш удалён: {cache_id}")
        except Exception as e:
            logger.error(f"❌ Ошибка при удалении кэша {cache_id}: {e}")

    # ===== Первый запуск пользователя =====
    async def is_first_time_user(self, user_id: int) -> bool:
        """
        Проверить, первый ли раз пользователь запускает бота
        
        Args:
            user_id: ID пользователя
            
        Returns:
            True если первый раз
        """
        if not self.client:
            return True
        
        try:
            key = f"{self.prefix}first_time_users"
            exists = await self.client.sismember(key, str(user_id))
            if not exists:
                await self.client.sadd(key, str(user_id))
            return not exists
        except Exception as e:
            logger.error(f"❌ Ошибка при проверке first_time для {user_id}: {e}")
            return True

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
        """
        Сохранить отслеживание цены
        
        Args:
            user_id: ID пользователя
            origin: IATA-код города отправления
            dest: IATA-код города назначения
            depart_date: дата вылета (ДД.ММ)
            return_date: дата возврата (ДД.ММ) или None
            current_price: текущая цена
            passengers: код пассажиров (например, "1", "21")
            threshold: порог уведомления (0=любое, 100=сотни, 1000=тысячи)
            
        Returns:
            Ключ отслеживания
        """
        if not self.client:
            logger.warning("⚠️ Redis отключён, отслеживание не сохранено")
            return ""
        
        try:
            # Формируем ключ
            key = f"{self.prefix}watch:{user_id}:{origin}:{dest}:{depart_date}"
            if return_date:
                key += f":{return_date}"
            
            # Формируем данные
            data = {
                "origin": origin,
                "dest": dest,
                "depart_date": depart_date,
                "return_date": return_date,
                "current_price": current_price,
                "passengers": passengers,
                "user_id": user_id,
                "threshold": threshold,
                "created_at": int(time.time()),
                "last_notified": 0  # Время последнего уведомления
            }
            
            # Сохраняем в Redis (30 дней)
            await self.client.setex(key, 86400 * 30, json.dumps(data, ensure_ascii=False))
            
            # Добавляем в список отслеживаний пользователя
            await self.client.sadd(f"{self.prefix}user:watches:{user_id}", key)
            
            logger.info(
                f"👀 Отслеживание сохранено: {user_id} | "
                f"{origin}→{dest} | {depart_date} | порог: {threshold}₽"
            )
            
            return key
            
        except Exception as e:
            logger.error(f"❌ Ошибка при сохранении отслеживания для {user_id}: {e}")
            return ""

    async def get_user_watches(self, user_id: int) -> List[Dict[str, Any]]:
        """
        Получить все отслеживания пользователя
        
        Args:
            user_id: ID пользователя
            
        Returns:
            Список отслеживаний
        """
        if not self.client:
            return []
        
        try:
            keys = await self.client.smembers(f"{self.prefix}user:watches:{user_id}")
            watches = []
            
            for key in keys:
                data = await self.client.get(key)
                if data:
                    watches.append(json.loads(data))
            
            return watches
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении отслеживаний для {user_id}: {e}")
            return []

    async def remove_watch(self, user_id: int, watch_key: str):
        """
        Удалить отслеживание
        
        Args:
            user_id: ID пользователя
            watch_key: ключ отслеживания
        """
        if not self.client:
            return
        
        try:
            await self.client.delete(watch_key)
            await self.client.srem(f"{self.prefix}user:watches:{user_id}", watch_key)
            logger.info(f"🗑️ Отслеживание удалено: {watch_key}")
        except Exception as e:
            logger.error(f"❌ Ошибка при удалении отслеживания {watch_key}: {e}")

    async def get_all_watch_keys(self) -> List[str]:
        """
        Получить все ключи отслеживаний для фоновой проверки
        
        Returns:
            Список ключей
        """
        if not self.client:
            return []
        
        try:
            pattern = f"{self.prefix}watch:*"
            cursor = "0"
            keys = []
            
            while cursor != 0:
                cursor, batch = await self.client.scan(cursor=cursor, match=pattern, count=100)
                keys.extend(batch)
            
            logger.debug(f"🔍 Найдено отслеживаний для проверки: {len(keys)}")
            return keys
            
        except Exception as e:
            logger.error(f"❌ Ошибка при получении всех ключей отслеживаний: {e}")
            return []

# Singleton instance
redis_client = RedisClient()