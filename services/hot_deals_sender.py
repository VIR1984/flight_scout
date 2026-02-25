# services/hot_deals_sender.py
"""
Фоновый сервис отправки горячих предложений и дайджестов.

Логика:
- Каждые 3 часа проверяем все «горячие» подписки:
    для каждой берём CATEGORIES[category][:5] направлений,
    ищем самый дешёвый рейс из origin в каждое из них,
    если цена ≤ max_price (или max_price == 0) — отправляем уведомление.
- Ежедневно в 09:00 МСК отправляем дайджест (топ-3 предложения).
- Раз в неделю (понедельник 09:00) — еженедельный дайджест.
"""

import asyncio
import json
import time
import logging
import random
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError

from utils.redis_client import redis_client
from utils.link_converter import convert_to_partner_link
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.cities_loader import get_city_name

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")

# Берём из handlers/hot_deals.py
CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",  ["AYT", "HRG", "SSH", "RHO", "DLM", "LCA", "TFS", "PMI", "CFU", "HER", "PFO", "AER", "SIP", "BUS"]),
    "city":   ("🏙️ Городские поездки", ["IST", "BCN", "CDG", "FCO", "AMS", "BER", "PRG", "BUD", "WAW", "VIE", "ATH", "HEL", "ARN", "OSL", "CPH"]),
    "world":  ("🌍 Путешествия по миру", ["DXB", "BKK", "SIN", "KUL", "HKT", "CMB", "NBO", "GRU", "JFK", "LAX", "YYZ", "ICN", "TYO", "PEK", "DEL"]),
    "russia": ("🇷🇺 По России",         ["AER", "LED", "KZN", "OVB", "SVX", "ROV", "UFA", "CEK", "KRR", "VOG", "MCX", "GRV", "KUF", "IKT", "VVO"]),
}


class HotDealsSender:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.running = False
        self.hot_check_interval = 3 * 3600   # 3 часа
        self.digest_check_interval = 60 * 10  # проверяем таймер каждые 10 мин

    async def start(self):
        self.running = True
        logger.info("🔥 HotDealsSender запущен")

        # Запускаем два цикла параллельно
        await asyncio.gather(
            self._hot_deals_loop(),
            self._digest_loop(),
        )

    # ══════════════════════════════════════════════
    # Горячие предложения — каждые 3 часа
    # ══════════════════════════════════════════════

    async def _hot_deals_loop(self):
        # Первый запуск через 1 минуту (дать боту запуститься)
        await asyncio.sleep(60)
        while self.running:
            try:
                await self._process_hot_subs()
            except Exception as e:
                logger.error(f"❌ [HotDeals] Ошибка в цикле горячих: {e}")
            await asyncio.sleep(self.hot_check_interval)

    async def _process_hot_subs(self):
        all_subs = await redis_client.get_all_hot_subs()
        hot_subs = [(uid, sid, s) for uid, sid, s in all_subs if s.get("sub_type") == "hot"]
        if not hot_subs:
            return
        logger.info(f"🔍 [HotDeals] Проверяем {len(hot_subs)} горячих подписок...")

        for user_id, sub_id, sub in hot_subs:
            if not self.running:
                break
            try:
                await self._check_hot_sub(user_id, sub_id, sub)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ [HotDeals] Ошибка sub {sub_id}: {e}")

    async def _check_hot_sub(self, user_id: int, sub_id: str, sub: dict):
        # Не чаще 12 часов на одну подписку
        last = sub.get("last_notified", 0)
        if time.time() - last < 12 * 3600:
            return

        origin = sub.get("origin_iata", "")
        category = sub.get("category", "world")
        max_price = sub.get("max_price", 0)
        passengers = sub.get("passengers", 1)
        travel_month = sub.get("travel_month")
        travel_year = sub.get("travel_year")

        _, destinations = CATEGORIES.get(category, ("", []))

        # Формируем дату поиска
        if travel_month:
            # 1-е число нужного месяца
            try:
                search_date = date(travel_year, travel_month, 1)
                if search_date < date.today():
                    search_date = date.today() + timedelta(days=7)
            except Exception:
                search_date = date.today() + timedelta(days=7)
        else:
            search_date = date.today() + timedelta(days=30)

        depart_str = search_date.strftime("%Y-%m-%d")

        # Ищем лучший рейс по всем направлениям категории
        best_flight = None
        best_price = None
        best_dest = None

        sample_dests = random.sample(destinations, min(6, len(destinations)))

        for dest in sample_dests:
            if dest == origin:
                continue
            try:
                flights = await search_flights(origin, dest, depart_str, None)
                if not flights:
                    continue
                cheapest = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
                price_per_pax = cheapest.get("value") or cheapest.get("price") or 0
                total = price_per_pax * passengers

                if best_price is None or price_per_pax < best_price:
                    best_price = price_per_pax
                    best_flight = cheapest
                    best_dest = dest

                await asyncio.sleep(0.5)
            except Exception as e:
                logger.debug(f"[HotDeals] {origin}→{dest}: {e}")

        if not best_flight or not best_price:
            return

        # Проверяем бюджет
        if max_price and best_price > max_price:
            return

        # Отправляем уведомление
        await self._send_hot_notification(user_id, sub_id, sub, best_flight, best_price, best_dest, passengers, depart_str)

    async def _send_hot_notification(
        self, user_id: int, sub_id: str, sub: dict,
        flight: dict, price: int, dest_iata: str,
        passengers: int, depart_str: str
    ):
        origin_iata = sub.get("origin_iata", "")
        origin_name = sub.get("origin_name", origin_iata)
        dest_name = get_city_name(dest_iata) or dest_iata
        cat_label, _ = CATEGORIES.get(sub.get("category", ""), ("", []))

        total_price = price * passengers
        pax_str = f"{passengers} чел." if passengers > 1 else "1 чел."

        text = (
            f"🔥 <b>Горячее предложение!</b>\n\n"
            f"📍 {cat_label}\n"
            f"✈️ <b>{origin_name} → {dest_name}</b>\n"
            f"📅 Примерно: {depart_str}\n"
            f"💰 <b>{price:,} ₽</b> / чел.".replace(",", " ")
        )
        if passengers > 1:
            text += f"\n🧮 Итого за {pax_str}: <b>{total_price:,} ₽</b>".replace(",", " ")
        text += "\n\n⏰ <i>Цены меняются — бронируйте быстрее!</i>"

        # Ссылка на бронирование
        clean_link = generate_booking_link(
            flight=flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_str,
            passengers_code=str(passengers),
            return_date=None
        )
        booking_link = await convert_to_partner_link(clean_link)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✈️ Забронировать за {price:,} ₽".replace(",", " "), url=booking_link)],
            [InlineKeyboardButton(text="❌ Отписаться от этой подписки", callback_data=f"hd_del_{sub_id}")],
        ])

        try:
            await self.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
            # Обновляем last_notified
            sub["last_notified"] = int(time.time())
            await redis_client.update_hot_sub(user_id, sub_id, sub)
            logger.info(f"✅ [HotDeals] Уведомление отправлено {user_id}: {origin_iata}→{dest_iata} {price}₽")
        except TelegramForbiddenError:
            logger.warning(f"⚠️ [HotDeals] Пользователь {user_id} заблокировал бота — удаляем подписку")
            await redis_client.delete_hot_sub(user_id, sub_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            logger.error(f"❌ [HotDeals] Telegram API: {e}")

    # ══════════════════════════════════════════════
    # Дайджест — ежедневно / еженедельно
    # ══════════════════════════════════════════════

    async def _digest_loop(self):
        await asyncio.sleep(120)  # старт через 2 минуты
        while self.running:
            try:
                now = datetime.now(MSK)
                # Отправляем дайджест в 09:00 МСК
                if now.hour == 9 and now.minute < 10:
                    is_monday = (now.weekday() == 0)
                    await self._process_digest_subs(is_monday_run=is_monday)
                    await asyncio.sleep(600)  # подождать 10 мин, не слать повторно
            except Exception as e:
                logger.error(f"❌ [Digest] Ошибка в цикле: {e}")
            await asyncio.sleep(self.digest_check_interval)

    async def _process_digest_subs(self, is_monday_run: bool):
        all_subs = await redis_client.get_all_hot_subs()
        digest_subs = [(uid, sid, s) for uid, sid, s in all_subs if s.get("sub_type") == "digest"]
        if not digest_subs:
            return
        logger.info(f"📰 [Digest] Обрабатываем {len(digest_subs)} дайджест-подписок...")

        for user_id, sub_id, sub in digest_subs:
            freq = sub.get("frequency", "daily")
            if freq == "weekly" and not is_monday_run:
                continue  # еженедельный — только по понедельникам
            try:
                await self._send_digest(user_id, sub_id, sub)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ [Digest] sub {sub_id}: {e}")

    async def _send_digest(self, user_id: int, sub_id: str, sub: dict):
        origin = sub.get("origin_iata", "")
        category = sub.get("category", "world")
        max_price = sub.get("max_price", 0)
        passengers = sub.get("passengers", 1)

        cat_label, destinations = CATEGORIES.get(category, ("", []))
        sample_dests = random.sample(destinations, min(8, len(destinations)))

        depart_date = (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")

        deals: List[Tuple[int, str, dict]] = []
        for dest in sample_dests:
            if dest == origin:
                continue
            try:
                flights = await search_flights(origin, dest, depart_date, None)
                if not flights:
                    continue
                cheapest = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
                price = cheapest.get("value") or cheapest.get("price") or 0
                if price and (not max_price or price <= max_price):
                    deals.append((price, dest, cheapest))
                await asyncio.sleep(0.3)
            except Exception:
                pass

        if not deals:
            return

        deals.sort(key=lambda x: x[0])
        top = deals[:3]

        origin_name = sub.get("origin_name", origin)
        freq = sub.get("frequency", "daily")
        freq_str = "Ежедневная подборка" if freq == "daily" else "Еженедельная подборка"

        text = f"📰 <b>{freq_str} горячих рейсов</b>\n{cat_label}\n🛫 Из: <b>{origin_name}</b>\n\n"

        kb_buttons = []
        for i, (price, dest_iata, flight) in enumerate(top, 1):
            dest_name = get_city_name(dest_iata) or dest_iata
            total = price * passengers
            text += f"{i}. ✈️ <b>{origin_name} → {dest_name}</b>\n"
            text += f"   💰 от <b>{price:,} ₽</b> / чел.".replace(",", " ")
            if passengers > 1:
                text += f" · {total:,} ₽ за {passengers} чел.".replace(",", " ")
            text += "\n\n"

            clean_link = generate_booking_link(
                flight=flight, origin=origin, dest=dest_iata,
                depart_date=depart_date, passengers_code=str(passengers), return_date=None
            )
            booking_link = await convert_to_partner_link(clean_link)
            kb_buttons.append([
                InlineKeyboardButton(
                    text=f"✈️ {dest_name} — {price:,} ₽".replace(",", " "),
                    url=booking_link
                )
            ])

        text += "⚠️ <i>Цены актуальны на момент отправки и могут изменяться.</i>"
        kb_buttons.append([
            InlineKeyboardButton(text="❌ Отписаться от дайджеста", callback_data=f"hd_del_{sub_id}")
        ])

        try:
            await self.bot.send_message(
                user_id, text, parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
            )
            sub["last_notified"] = int(time.time())
            await redis_client.update_hot_sub(user_id, sub_id, sub)
            logger.info(f"✅ [Digest] Отправлен {user_id}")
        except TelegramForbiddenError:
            logger.warning(f"⚠️ [Digest] Пользователь {user_id} заблокировал бота — удаляем")
            await redis_client.delete_hot_sub(user_id, sub_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
        except TelegramAPIError as e:
            logger.error(f"❌ [Digest] API error: {e}")