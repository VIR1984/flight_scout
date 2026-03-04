# services/hot_deals_sender.py
"""
Фоновый сервис отправки горячих предложений и дайджестов.

Улучшения:
  1. Перебираем ВСЕ направления категории (не random.sample).
  2. Дайджест ищет рейсы на выбранный пользователем месяц (не +14 дней).
  3. Базовая цена (EMA в Redis): уведомляем только когда цена упала >= DROP_THRESHOLD.
  4. Кулдаун маршрута: одно направление не шлётся чаще раза в ROUTE_COOLDOWN секунд.
"""

import asyncio
import time
import logging
from datetime import datetime, date, timedelta
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError

from utils.redis_client import redis_client
from utils.api_limiter import BACKGROUND_SEMAPHORE
from utils.link_converter import convert_to_partner_link
from services.flight_search import search_flights, generate_booking_link
from utils.cities_loader import get_city_name

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

DROP_THRESHOLD = 0.10      # уведомлять только при снижении >= 10% от базовой
ROUTE_COOLDOWN = 86400     # кулдаун на маршрут: 24 часа
SUB_COOLDOWN   = 12 * 3600 # общий таймер подписки: не чаще 12 ч

# Единый источник категорий — из handlers
from handlers.hot_deals import CATEGORIES


def _resolve_search_date(sub: dict) -> date:
    """Улучшение 2: ближайший выбранный месяц, fallback — сегодня + 30 дней."""
    today = date.today()
    for mk in sub.get("travel_months", []):
        try:
            m, y = map(int, mk.split("_"))
            candidate = date(y, m, 15)
            if candidate >= today:
                return candidate
        except Exception:
            pass
    tm, ty = sub.get("travel_month"), sub.get("travel_year")
    if tm and ty:
        try:
            candidate = date(ty, tm, 15)
            if candidate >= today:
                return candidate
        except Exception:
            pass
    return today + timedelta(days=30)


class HotDealsSender:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.running = False
        self.hot_check_interval    = 3 * 3600
        self.digest_check_interval = 60 * 10

    async def start(self):
        self.running = True
        logger.info("🔥 HotDealsSender запущен")
        try:
            await asyncio.gather(self._hot_deals_loop(), self._digest_loop())
        except asyncio.CancelledError:
            logger.info("🛑 HotDealsSender остановлен")
        finally:
            self.running = False

    def stop(self):
        self.running = False

    # ══════════════════════════════════════════════
    # Горячие предложения
    # ══════════════════════════════════════════════

    async def _hot_deals_loop(self):
        await asyncio.sleep(60)
        while self.running:
            try:
                await self._process_hot_subs()
            except Exception as e:
                logger.error(f"❌ [HotDeals] {e}")
            await asyncio.sleep(self.hot_check_interval)

    async def _process_hot_subs(self):
        all_subs = await redis_client.get_all_hot_subs()
        hot_subs = [(uid, sid, s) for uid, sid, s in all_subs if s.get("sub_type") == "hot"]
        logger.info(f"🔍 [HotDeals] {len(hot_subs)} горячих подписок")
        for user_id, sub_id, sub in hot_subs:
            if not self.running:
                break
            try:
                await self._check_hot_sub(user_id, sub_id, sub)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ [HotDeals] sub {sub_id}: {e}", exc_info=True)

    async def _check_hot_sub(self, user_id: int, sub_id: str, sub: dict):
        if time.time() - sub.get("last_notified", 0) < SUB_COOLDOWN:
            return

        # ── Города вылета: поддержка мультигорода ──
        origins_list = sub.get("origins", [])
        if origins_list:
            origin_iatas = [o["iata"] for o in origins_list if o.get("iata")]
        else:
            origin_iatas = [sub.get("origin_iata")] if sub.get("origin_iata") else []
        if not origin_iatas:
            logger.warning(f"[HotDeals] sub={sub_id}: нет городов вылета — пропускаем")
            return

        category   = sub.get("category", "world")
        max_price  = sub.get("max_price", 0)
        passengers = sub.get("passengers", 1)
        _, cat_destinations = CATEGORIES.get(category, ("", []))

        # ── Направления назначения ──
        # custom: пользователь задал свой список; иначе — список категории
        if category == "custom":
            dest_pool = sub.get("dest_iata_list", [])
        else:
            dest_pool = cat_destinations

        if not dest_pool:
            logger.warning(f"[HotDeals] sub={sub_id}: пустой список назначений (cat={category})")
            return

        depart_str = _resolve_search_date(sub).strftime("%Y-%m-%d")
        logger.info(f"[HotDeals] sub={sub_id} origins={origin_iatas} → {len(dest_pool)} направлений дата={depart_str}")

        candidates: List[Tuple[int, str, str, dict, Optional[float]]] = []
        for origin in origin_iatas:
            scan_dests = [d for d in dest_pool if d != origin]
            for dest in scan_dests:
                try:
                    async with BACKGROUND_SEMAPHORE:
                        flights = await search_flights(origin, dest, depart_str, None)
                    if not flights:
                        continue
                    cheapest = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
                    price = cheapest.get("value") or cheapest.get("price") or 0
                    if not price:
                        continue

                    baseline = await redis_client.get_baseline_price(origin, dest)
                    await redis_client.update_baseline_price(origin, dest, price)

                    if max_price and price * passengers > max_price * passengers:
                        continue
                    if baseline is not None and (baseline - price) / baseline < DROP_THRESHOLD:
                        logger.debug(f"[HotDeals] {origin}→{dest}: снижение < {DROP_THRESHOLD:.0%}")
                        continue

                    candidates.append((price, origin, dest, cheapest, baseline))
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning(f"[HotDeals] {origin}→{dest}: {e}")

        if not candidates:
            logger.info(f"[HotDeals] sub={sub_id}: нет кандидатов")
            return

        candidates.sort(key=lambda x: x[0])
        chosen = None
        for price, orig, dest, flight, baseline in candidates:
            if not await redis_client.is_route_on_cooldown(sub_id, dest, ROUTE_COOLDOWN):
                chosen = (price, orig, dest, flight, baseline)
                break

        if chosen is None:
            logger.info(f"[HotDeals] sub={sub_id}: все {len(candidates)} кандидатов на кулдауне")
            return

        best_price, best_orig, best_dest, best_flight, baseline = chosen
        logger.info(f"[HotDeals] 🔥 {best_orig}→{best_dest} {best_price}₽")
        await self._send_hot_notification(
            user_id, sub_id, sub, best_flight, best_price, best_orig, best_dest,
            passengers, depart_str, baseline=baseline,
        )

    async def _send_hot_notification(
        self, user_id: int, sub_id: str, sub: dict,
        flight: dict, price: int, origin_iata: str, dest_iata: str,
        passengers: int, depart_str: str, baseline: Optional[float] = None,
    ):
        origin_name = get_city_name(origin_iata) or sub.get("origin_name", origin_iata)
        dest_name   = get_city_name(dest_iata) or dest_iata
        cat_label, _ = CATEGORIES.get(sub.get("category", ""), ("", []))

        discount_line = ""
        if baseline and baseline > price:
            drop_pct = int((baseline - price) / baseline * 100)
            discount_line = f"\n📉 Обычно от <b>{int(baseline):,} ₽</b> — дешевле на <b>{drop_pct}%</b>".replace(",", "\u202f")

        text = (
            f"🔥 <b>Горячее предложение!</b>\n\n"
            f"📍 {cat_label}\n"
            f"✈️ <b>{origin_name} → {dest_name}</b>\n"
            f"📅 Примерно: {depart_str}\n"
            f"💰 <b>{price:,} ₽</b> / чел.".replace(",", "\u202f") + discount_line
        )
        if passengers > 1:
            text += f"\n🧮 Итого за {passengers} чел.: <b>{price * passengers:,} ₽</b>".replace(",", "\u202f")
        text += "\n\n⏰ <i>Цены меняются — бронируйте быстрее!</i>"

        booking_link = await convert_to_partner_link(generate_booking_link(
            flight=flight, origin=origin_iata, dest=dest_iata,
            depart_date=depart_str, passengers_code=str(passengers), return_date=None,
        ))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✈️ Забронировать за {price:,} ₽".replace(",", "\u202f"), url=booking_link)],
            [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"hd_del_{sub_id}")],
            [InlineKeyboardButton(text="↩️ В начало",  callback_data="main_menu")],
        ])

        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
            sub["last_notified"] = int(time.time())
            await redis_client.update_hot_sub(user_id, sub_id, sub)
            await redis_client.set_route_cooldown(sub_id, dest_iata, ROUTE_COOLDOWN)
            logger.info(f"✅ [HotDeals] {user_id}: {origin_iata}→{dest_iata} {price}₽")
        except TelegramForbiddenError:
            await redis_client.delete_hot_sub(user_id, sub_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            logger.error(f"❌ [HotDeals] API: {e}")

    # ══════════════════════════════════════════════
    # Дайджест
    # ══════════════════════════════════════════════

    async def _digest_loop(self):
        await asyncio.sleep(120)
        logger.info("[Digest] Цикл запущен")
        while self.running:
            try:
                now = datetime.now(MSK)
                if now.hour == 9 and now.minute < 10:
                    await self._process_digest_subs(is_monday_run=(now.weekday() == 0))
                    await asyncio.sleep(600)
            except Exception as e:
                logger.error(f"❌ [Digest] {e}", exc_info=True)
            await asyncio.sleep(self.digest_check_interval)

    async def _process_digest_subs(self, is_monday_run: bool):
        all_subs = await redis_client.get_all_hot_subs()
        digest_subs = [(uid, sid, s) for uid, sid, s in all_subs if s.get("sub_type") == "digest"]
        logger.info(f"📰 [Digest] {len(digest_subs)} подписок (пн={is_monday_run})")
        for user_id, sub_id, sub in digest_subs:
            if sub.get("frequency", "daily") == "weekly" and not is_monday_run:
                continue
            try:
                await self._send_digest(user_id, sub_id, sub)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ [Digest] sub {sub_id}: {e}")

    async def _send_digest(self, user_id: int, sub_id: str, sub: dict):
        # ── Города вылета: мультигород ──
        origins_list = sub.get("origins", [])
        if origins_list:
            origin_iatas = [o["iata"] for o in origins_list if o.get("iata")]
        else:
            origin_iatas = [sub.get("origin_iata")] if sub.get("origin_iata") else []
        if not origin_iatas:
            logger.warning(f"[Digest] sub={sub_id}: нет городов вылета")
            return
        # Для отображения берём первый город (или все через запятую)
        origin_name = ", ".join(
            get_city_name(iata) or iata for iata in origin_iatas
        )

        category   = sub.get("category", "world")
        max_price  = sub.get("max_price", 0)
        passengers = sub.get("passengers", 1)
        cat_label, cat_destinations = CATEGORIES.get(category, ("", []))

        if category == "custom":
            dest_pool = sub.get("dest_iata_list", [])
        else:
            dest_pool = cat_destinations

        if not dest_pool:
            logger.warning(f"[Digest] sub={sub_id}: пустой список назначений (cat={category})")
            return

        depart_date = _resolve_search_date(sub).strftime("%Y-%m-%d")
        logger.info(f"[Digest] user={user_id} origins={origin_iatas} кат={category} дата={depart_date}")

        deals: List[Tuple[int, str, str, dict, Optional[float]]] = []
        for origin in origin_iatas:
            scan_dests = [d for d in dest_pool if d != origin]
            for dest in scan_dests:
                try:
                    async with BACKGROUND_SEMAPHORE:
                        flights = await search_flights(origin, dest, depart_date, None)
                    if not flights:
                        continue
                    cheapest = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
                    price = cheapest.get("value") or cheapest.get("price") or 0
                    if not price:
                        continue

                    baseline = await redis_client.update_baseline_price(origin, dest, price)

                    if max_price and price > max_price:
                        continue

                    if await redis_client.is_route_on_cooldown(sub_id, dest, ROUTE_COOLDOWN):
                        logger.debug(f"[Digest] {origin}→{dest}: кулдаун")
                        continue

                    deals.append((price, origin, dest, cheapest, baseline))
                    await asyncio.sleep(0.3)
                except Exception:
                    pass

        if not deals:
            logger.info(f"[Digest] sub={sub_id}: нет предложений")
            return

        deals.sort(key=lambda x: x[0])
        top3 = deals[:3]
        freq_str = "Ежедневная подборка" if sub.get("frequency", "daily") == "daily" else "Еженедельная подборка"
        text = f"📰 <b>{freq_str} горячих рейсов</b>\n{cat_label}\n🛫 Из: <b>{origin_name}</b>\n\n"
        kb_buttons = []

        for i, (price, orig_iata, dest_iata, flight, baseline) in enumerate(top3, 1):
            dest_name = get_city_name(dest_iata) or dest_iata
            orig_name = get_city_name(orig_iata) or orig_iata

            discount = ""
            if baseline and baseline > price:
                discount = f" 📉 <b>-{int((baseline - price) / baseline * 100)}%</b>"

            text += f"{i}. ✈️ <b>{orig_name} → {dest_name}</b>\n"
            text += f"   💰 от <b>{price:,} ₽</b> / чел.{discount}".replace(",", "\u202f")
            if passengers > 1:
                text += f" · {price * passengers:,} ₽ за {passengers} чел.".replace(",", "\u202f")
            text += "\n\n"

            booking_link = await convert_to_partner_link(generate_booking_link(
                flight=flight, origin=orig_iata, dest=dest_iata,
                depart_date=depart_date, passengers_code=str(passengers), return_date=None,
            ))
            kb_buttons.append([InlineKeyboardButton(
                text=f"✈️ {dest_name} — {price:,} ₽".replace(",", "\u202f"),
                url=booking_link,
            )])

        text += "⚠️ <i>Цены актуальны на момент отправки и могут изменяться.</i>"
        kb_buttons.append([InlineKeyboardButton(text="❌ Отписаться от дайджеста", callback_data=f"hd_del_{sub_id}")])
        kb_buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

        try:
            await self.bot.send_message(
                user_id, text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons),
            )
            sub["last_notified"] = int(time.time())
            await redis_client.update_hot_sub(user_id, sub_id, sub)
            # Улучшение 4: кулдаун на все отправленные маршруты
            for _, _orig, dest_iata, _, _ in top3:
                await redis_client.set_route_cooldown(sub_id, dest_iata, ROUTE_COOLDOWN)
            logger.info(f"✅ [Digest] {user_id} топ-3: {[d for _,d,_,_ in top3]}")
        except TelegramForbiddenError:
            await redis_client.delete_hot_sub(user_id, sub_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            logger.error(f"❌ [Digest] API: {e}")