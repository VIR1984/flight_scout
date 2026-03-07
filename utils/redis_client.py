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
        env = os.getenv("BOT_ENV", "prod")  # "dev" или "prod"
        self.prefix = f"flight_bot:{env}:"

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

    async def set_search_cache(self, cache_id: str, data: Dict[str, Any], ttl: int = 3600):
        if not self.client:
            return
        await self.client.setex(
            f"{self.prefix}search:{cache_id}",
            ttl,
            json.dumps(data, ensure_ascii=False),
        )

    async def get_search_cache(self, cache_id: str) -> Optional[Dict[str, Any]]:
        if not self.client:
            return None
        raw = await self.client.get(f"{self.prefix}search:{cache_id}")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def get_user_watches(self, user_id: int) -> List[Dict[str, Any]]:
        """Получить все отслеживания пользователя (с watch_key для удаления)"""
        if not self.client:
            return []
        keys = await self.client.smembers(f"{self.prefix}user:watches:{user_id}")
        watches = []
        for key in keys:
            raw = await self.client.get(key)
            if raw:
                item = json.loads(raw)
                # Для совместимости со старыми записями без watch_key
                if "watch_key" not in item:
                    key_str = key.decode() if isinstance(key, bytes) else str(key)
                    item["watch_key"] = key_str
                watches.append(item)
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
        cursor = 0
        keys = []
        while True:
            cursor, batch = await self.client.scan(cursor=cursor, match=pattern, count=100)
            keys.extend(batch)
            if cursor == 0:
                break
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

    async def save_flight_track_subscription(
        self,
        user_id: int,
        airline: str,
        flight_number: str,
        depart_date: str,   # формат ДД.ММ
    ) -> str:
        """
        Сохранить подписку на уведомления об изменении статуса рейса.
        Возвращает sub_id.
        Хранится 7 дней (рейс уже точно прошёл).
        """
        if not self.client:
            return ""
        import uuid
        sub_id = str(uuid.uuid4())[:8]
        key = f"{self.prefix}flight_track:{user_id}:{sub_id}"
        data = {
            "airline":       airline,
            "flight_number": flight_number,
            "depart_date":   depart_date,
            "user_id":       str(user_id),
            "sub_id":        sub_id,
        }
        await self.client.hset(key, mapping=data)
        await self.client.expire(key, 86400 * 7)
        return sub_id

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

    # ══════════════════════════════════════════════
    # Базовая цена маршрута (EMA — скользящее среднее)
    # ══════════════════════════════════════════════

    async def get_baseline_price(self, origin: str, dest: str) -> Optional[float]:
        """Возвращает сохранённое EMA-среднее для маршрута origin→dest, или None."""
        if not self.client:
            return None
        raw = await self.client.get(f"{self.prefix}baseline:{origin}:{dest}")
        if raw is None:
            return None
        try:
            return float(json.loads(raw)["avg"])
        except Exception:
            return None

    async def update_baseline_price(
        self, origin: str, dest: str, new_price: float,
        alpha: float = 0.3, ttl: int = 86400 * 30,
    ) -> float:
        """
        Обновляет EMA: avg = alpha * new_price + (1-alpha) * old_avg.
        Первое наблюдение сохраняется as-is. Возвращает актуальное среднее.
        TTL 30 дней — чтобы стale-данные не накапливались.
        """
        if not self.client:
            return new_price
        existing = await self.get_baseline_price(origin, dest)
        avg = new_price if existing is None else alpha * new_price + (1 - alpha) * existing
        await self.client.set(
            f"{self.prefix}baseline:{origin}:{dest}",
            json.dumps({"avg": round(avg, 2)}),
            ex=ttl,
        )
        return avg

    # ══════════════════════════════════════════════
    # Кулдаун маршрута (не слать одно направление чаще раза в сутки)
    # ══════════════════════════════════════════════

    async def is_route_on_cooldown(
        self, sub_id: str, dest: str, cooldown: int = 86400
    ) -> bool:
        """True если маршрут уже отправлялся по этой подписке менее cooldown секунд назад."""
        if not self.client:
            return False
        return await self.client.exists(f"{self.prefix}route_cd:{sub_id}:{dest}") > 0

    async def set_route_cooldown(
        self, sub_id: str, dest: str, cooldown: int = 86400
    ) -> None:
        """Помечает маршрут как отправленный. Ключ живёт cooldown секунд и удаляется сам."""
        if not self.client:
            return
        await self.client.set(f"{self.prefix}route_cd:{sub_id}:{dest}", "1", ex=cooldown)

    # ── Кеш пустых маршрутов ────────────────────────────────────────────────
    # Aviasales grouped_prices кешируется ~48ч на их стороне.
    # Если маршрут вернул пустой ответ — не опрашиваем снова раньше TTL.
    # Ключ: route_empty:{origin}:{dest} — глобальный (не per-sub),
    # т.к. пустой ответ одинаков для всех подписок с тем же маршрутом.

    EMPTY_ROUTE_TTL = 48 * 3600  # 48 часов — срок кеша Aviasales

    async def is_route_empty(self, origin: str, dest: str) -> bool:
        """True если маршрут недавно возвращал пустой ответ API (ждём TTL)."""
        if not self.client:
            return False
        try:
            return await self.client.exists(
                f"{self.prefix}route_empty:{origin}:{dest}"
            ) > 0
        except Exception:
            return False

    async def set_route_empty(self, origin: str, dest: str) -> None:
        """Помечает маршрут как пустой на EMPTY_ROUTE_TTL секунд."""
        if not self.client:
            return
        try:
            await self.client.set(
                f"{self.prefix}route_empty:{origin}:{dest}",
                "1",
                ex=self.EMPTY_ROUTE_TTL,
            )
        except Exception:
            pass

    async def clear_route_empty(self, origin: str, dest: str) -> None:
        """Сбрасывает метку — вызывать когда по маршруту вдруг появились данные."""
        if not self.client:
            return
        try:
            await self.client.delete(f"{self.prefix}route_empty:{origin}:{dest}")
        except Exception:
            pass


    # ════════════════════════════════════════════════════════════════
    # Analytics
    # ════════════════════════════════════════════════════════════════

    async def track_no_results(self, origin_iata: str, dest_iata: str, depart_date: str) -> None:
        """Маршруты по которым ничего не нашлось — тоже интересно."""
        if not self.client:
            return
        p = self.prefix
        route = f"{origin_iata}-{dest_iata}"
        await self.client.zincrby(f"{p}analytics:no_results", 1, route)
        await self.client.incr(f"{p}analytics:total_no_results")


    async def track_link_click(self, context: str = "unknown") -> None:
        """Счётчик генерации партнёрских ссылок (= показов кнопки бронирования)."""
        if not self.client:
            return
        p = self.prefix
        await self.client.hincrby(f"{p}analytics:link_clicks", context, 1)
        await self.client.incr(f"{p}analytics:total_link_clicks")

    async def track_funnel_step(self, step: str) -> None:
        """
        Воронка поиска. Шаги:
          start → route → date → passengers → confirm → result_shown → link_clicked
        Позволяет видеть на каком шаге пользователи уходят.
        """
        if not self.client:
            return
        await self.client.hincrby(f"{self.prefix}analytics:funnel", step, 1)

    async def track_search_type(self, search_type: str) -> None:
        """
        Тип поиска: normal / everywhere_dest / everywhere_origin /
                    country / multi / quick
        """
        if not self.client:
            return
        await self.client.hincrby(f"{self.prefix}analytics:search_types", search_type, 1)

    async def track_subscription_event(self, sub_type: str, action: str = "created") -> None:
        """
        sub_type: hot_deals / digest / price_watch
        action:   created / deleted
        """
        if not self.client:
            return
        p = self.prefix
        await self.client.hincrby(f"{p}analytics:sub_types", sub_type, 1 if action == "created" else -1)
        if action == "created":
            await self.client.incr(f"{p}analytics:total_subs_created")

    async def get_analytics(self) -> dict:
        """Собирает всю аналитику для /stats."""
        if not self.client:
            return {}
        p = self.prefix
        result = {}

        # Общие счётчики
        result["total_searches"]   = int(await self.client.get(f"{p}analytics:total_searches") or 0)
        result["total_no_results"] = int(await self.client.get(f"{p}analytics:total_no_results") or 0)
        result["searching_users"]  = await self.client.scard(f"{p}analytics:searching_users")
        result["total_users"]      = await self.client.scard(f"{p}first_time_users")

        # Топ маршруты
        top_routes = await self.client.zrevrange(f"{p}analytics:routes", 0, 9, withscores=True)
        result["top_routes"] = [(r.decode() if isinstance(r, bytes) else r, int(s)) for r, s in top_routes]

        # Топ направления
        top_dest = await self.client.zrevrange(f"{p}analytics:dest_cities", 0, 9, withscores=True)
        result["top_destinations"] = [(d.decode() if isinstance(d, bytes) else d, int(s)) for d, s in top_dest]

        # Топ города вылета
        top_orig = await self.client.zrevrange(f"{p}analytics:origin_cities", 0, 4, withscores=True)
        result["top_origins"] = [(o.decode() if isinstance(o, bytes) else o, int(s)) for o, s in top_orig]

        # Цены по сегментам
        price_buckets = await self.client.zrevrange(f"{p}analytics:price_buckets", 0, -1, withscores=True)
        result["price_buckets"] = [(b.decode() if isinstance(b, bytes) else b, int(s)) for b, s in price_buckets]

        # Тип рейса
        result["flight_types"]  = await self.client.hgetall(f"{p}analytics:flight_types")
        result["transfers"]     = await self.client.hgetall(f"{p}analytics:transfers")
        result["passengers"]    = await self.client.hgetall(f"{p}analytics:passengers")
        result["trip_type"]     = await self.client.hgetall(f"{p}analytics:trip_type")

        # Маршруты без результатов
        no_res = await self.client.zrevrange(f"{p}analytics:no_results", 0, 4, withscores=True)
        result["top_no_results"] = [(r.decode() if isinstance(r, bytes) else r, int(s)) for r, s in no_res]

        # Поиски по дням (последние 7)
        searches_by_day = await self.client.hgetall(f"{p}analytics:searches_by_day")
        if searches_by_day:
            decoded = {(k.decode() if isinstance(k, bytes) else k): int(v)
                       for k, v in searches_by_day.items()}
            result["searches_by_day"] = dict(sorted(decoded.items())[-7:])

        # Активные подписки
        try:
            all_subs = await self.get_all_hot_subs()
            result["active_subscriptions"] = len(all_subs)
        except Exception:
            result["active_subscriptions"] = "—"

        # Отслеживания цен
        try:
            watches = await self.get_all_watch_keys()
            result["price_watches"] = len(watches)
        except Exception:
            result["price_watches"] = "—"

        # Клики по ссылкам
        result["total_link_clicks"] = int(await self.client.get(f"{p}analytics:total_link_clicks") or 0)
        link_clicks_raw = await self.client.hgetall(f"{p}analytics:link_clicks")
        result["link_clicks_by_context"] = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in link_clicks_raw.items()
        }

        # Воронка поиска
        funnel_raw = await self.client.hgetall(f"{p}analytics:funnel")
        result["funnel"] = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in funnel_raw.items()
        }

        # Типы поиска
        st_raw = await self.client.hgetall(f"{p}analytics:search_types")
        result["search_types"] = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in st_raw.items()
        }

        # Типы подписок
        subs_raw = await self.client.hgetall(f"{p}analytics:sub_types")
        result["sub_types"] = {
            (k.decode() if isinstance(k, bytes) else k): int(v)
            for k, v in subs_raw.items()
        }
        result["total_subs_created"] = int(await self.client.get(f"{p}analytics:total_subs_created") or 0)

        return result

# Singleton
redis_client = RedisClient()