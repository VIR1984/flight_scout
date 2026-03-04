# services/price_watcher.py
"""
Фоновый сервис отслеживания цен.

Ключевые оптимизации для масштабирования:
  1. ДЕДУПЛИКАЦИЯ: если 100 человек следят за MOW→AER на одну дату —
     делаем ОДИН запрос к API, результат раздаём всем.
  2. BACKGROUND_SEMAPHORE: фоновые запросы не блокируют живых пользователей.
  3. Параллельные запросы к API по уникальным маршрутам (с семафором).
"""
import asyncio
import json
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError

from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.redis_client import redis_client
from utils.api_limiter import BACKGROUND_SEMAPHORE
from utils.logger import logger
from utils.cities import IATA_TO_CITY
from utils.link_converter import convert_to_partner_link


class PriceWatcher:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.running = False
        self.check_interval = 21600   # 6 часов
        # Кэш результатов на текущий цикл: route_key -> (price|None, ts)
        self._cycle_cache: Dict[str, Tuple[Optional[int], float]] = {}

    async def start(self):
        self.running = True
        logger.info("PriceWatcher запущен (интервал 6 ч)")
        await self.check_all_watches()
        while self.running:
            try:
                await asyncio.sleep(self.check_interval)
                if self.running:
                    await self.check_all_watches()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Критическая ошибка PriceWatcher: {e}")
                await asyncio.sleep(300)

    async def stop(self):
        self.running = False

    # ────────────────────────────────────────────────────────
    # Главный цикл
    # ────────────────────────────────────────────────────────

    async def check_all_watches(self):
        watch_keys = await redis_client.get_all_watch_keys()
        if not watch_keys:
            return

        # Загружаем все данные
        watches: List[Tuple[str, dict]] = []
        for key in watch_keys:
            raw = await redis_client.client.get(key)
            if not raw:
                continue
            try:
                watches.append((
                    key.decode() if isinstance(key, bytes) else key,
                    json.loads(raw)
                ))
            except json.JSONDecodeError:
                await redis_client.client.delete(key)

        # ── ДЕДУПЛИКАЦИЯ ─────────────────────────────────────
        # Группируем по маршруту: route_key -> [(str_key, watch), ...]
        by_route: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
        for str_key, watch in watches:
            by_route[self._route_key(watch)].append((str_key, watch))

        saved_requests = len(watches) - len(by_route)
        logger.info(
            f"Проверка: {len(watches)} отслеживаний, "
            f"{len(by_route)} уникальных маршрутов "
            f"(сэкономлено {saved_requests} API-запросов)"
        )

        self._cycle_cache.clear()

        # Запрашиваем цены параллельно — по одному на маршрут
        await asyncio.gather(
            *[self._fetch_route_price(rk, items[0][1]) for rk, items in by_route.items()],
            return_exceptions=True
        )

        total_notified = total_removed = 0
        for route_key, items in by_route.items():
            new_price = self._cycle_cache.get(route_key, (None,))[0]
            for str_key, watch in items:
                if not self.running:
                    break
                try:
                    result = await self._process_watch(watch, str_key, new_price)
                    if result == "removed":
                        total_removed += 1
                    elif result:
                        total_notified += 1
                except Exception as e:
                    logger.error(f"Ошибка обработки {str_key}: {e}")

        logger.info(f"Проверка завершена: уведомлений {total_notified}, удалено {total_removed}")

    # ────────────────────────────────────────────────────────
    # API-запрос с семафором
    # ────────────────────────────────────────────────────────

    @staticmethod
    def _route_key(watch: dict) -> str:
        return (f"{watch.get('origin') or 'X'}:{watch.get('dest') or 'X'}:"
                f"{watch.get('depart_date', '')}:{watch.get('return_date') or ''}")

    async def _fetch_route_price(self, route_key: str, watch: dict) -> None:
        async with BACKGROUND_SEMAPHORE:
            try:
                flights = await search_flights(
                    origin=watch.get("origin"),
                    destination=watch.get("dest"),
                    depart_date=normalize_date(watch.get("depart_date", "")),
                    return_date=normalize_date(watch["return_date"]) if watch.get("return_date") else None,
                )
                if not flights:
                    self._cycle_cache[route_key] = (None, time.time())
                    return
                mf = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
                raw = mf.get("value") or mf.get("price")
                self._cycle_cache[route_key] = (int(float(raw)) if raw else None, time.time())
            except Exception as e:
                logger.error(f"API ошибка {route_key}: {e}")
                self._cycle_cache[route_key] = (None, time.time())

    # ────────────────────────────────────────────────────────
    # Обработка одного watch
    # ────────────────────────────────────────────────────────

    async def _process_watch(self, watch: dict, key: str, new_price: Optional[int]) -> Optional[str]:
        user_id       = watch["user_id"]
        current_price = watch.get("current_price", 0)
        threshold     = watch.get("threshold", 0)
        last_notified = watch.get("last_notified", 0)

        if (time.time() - last_notified) / 3600 < 24:
            return False
        if not new_price:
            return False

        price_change = int(float(current_price)) - int(float(new_price))
        abs_change   = abs(price_change)

        # Тихо обновляем при росте или незначимом изменении
        if abs_change >= 50 and price_change <= 0:
            watch["current_price"] = new_price
            await redis_client.client.setex(key, 86400 * 30, json.dumps(watch, ensure_ascii=False))
            return False

        if not (price_change > 0 and abs_change >= max(50, threshold)):
            return False

        try:
            success = await self._send_notification(user_id, watch, new_price, price_change, key)
        except Exception as e:
            logger.error(f"Уведомление {user_id}: {e}")
            if "blocked" in str(e).lower():
                await redis_client.remove_watch(user_id, key)
                return "removed"
            return False

        if success:
            watch["current_price"] = new_price
            watch["last_notified"] = int(time.time())
            await redis_client.client.setex(key, 86400 * 30, json.dumps(watch, ensure_ascii=False))
            logger.info(f"Уведомление {user_id}: {current_price}→{new_price} ₽ ({price_change:+d})")
            return True
        else:
            await redis_client.remove_watch(user_id, key)
            return "removed"

    # ────────────────────────────────────────────────────────
    # Отправка уведомления
    # ────────────────────────────────────────────────────────

    async def _send_notification(self, user_id, watch, new_price, price_change, key) -> bool:
        try:
            origin_name = IATA_TO_CITY.get(watch.get("origin", ""), watch.get("origin", "")) or "Везде"
            dest_name   = IATA_TO_CITY.get(watch.get("dest", ""),   watch.get("dest", ""))   or "Везде"
            depart      = watch.get("display_depart") or watch.get("depart_date", "")
            ret         = watch.get("display_return") or watch.get("return_date")
            pax         = self._format_passengers(watch.get("passengers", "1"))

            text = (
                f"📉 <b>Цена снизилась</b>\n\n"
                f"<b>Маршрут:</b> {origin_name} → {dest_name}\n"
                f"<b>Вылет:</b> {depart}\n"
            )
            if ret:
                text += f"<b>Обратно:</b> {ret}\n"
            if pax:
                text += f"<b>Пассажиры:</b> {pax}\n"
            text += (
                f"\nБыло: {watch['current_price']}\u202f₽  →  "
                f"<b>Стало: {new_price}\u202f₽</b>\n"
                f"<i>Выгода: {abs(price_change)}\u202f₽</i>"
            )

            link = await convert_to_partner_link(generate_booking_link(
                flight={"value": new_price, "origin": watch.get("origin"), "destination": watch.get("dest")},
                origin=watch.get("origin", ""), dest=watch.get("dest", ""),
                depart_date=normalize_date(watch["depart_date"]) if watch.get("depart_date") else "",
                passengers_code=watch.get("passengers", "1"),
                return_date=normalize_date(watch["return_date"]) if watch.get("return_date") else None,
            ))

            await self.bot.send_message(
                chat_id=user_id, text=text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"Забронировать за {new_price}\u202f₽", url=link)],
                    [InlineKeyboardButton(text="Больше не следить", callback_data=f"unwatch_{key}")],
                    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
                ]),
                disable_web_page_preview=True,
            )
            return True

        except TelegramForbiddenError:
            return False
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            return False
        except TelegramAPIError as e:
            logger.error(f"Telegram API {user_id}: {e}")
            return False
        except Exception:
            raise

    @staticmethod
    def _format_passengers(code: str) -> str:
        try:
            adults   = int(code[0])
            children = int(code[1]) if len(code) > 1 else 0
            infants  = int(code[2]) if len(code) > 2 else 0
            parts = []
            if adults:   parts.append(f"{adults} взр.")
            if children: parts.append(f"{children} реб.")
            if infants:  parts.append(f"{infants} мл.")
            return ", ".join(parts)
        except Exception:
            return ""
