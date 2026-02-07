# services/price_watcher.py
import asyncio
import json
import time
from datetime import datetime
from typing import Dict, Optional
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramAPIError
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.redis_client import redis_client
from utils.logger import logger
from utils.cities import IATA_TO_CITY


class PriceWatcher:
    """Фоновый сервис для отслеживания изменения цен на авиабилеты"""
    
    def __init__(self, bot: Bot):
        self.bot = bot
        self.running = False
        self.check_interval = 21600  # 6 часов
        self.last_notification: Dict[str, float] = {}  # key → timestamp последнего уведомления
        self.route_cache: Dict[str, tuple] = {}  # кэш результатов поиска для одного маршрута
        self.cache_ttl = 300  # 5 минут кэширования
    
    async def start(self):
        """Запустить периодическую проверку цен"""
        self.running = True
        logger.info("✅ Запуск наблюдателя за ценами (проверка каждые 6 часов)...")
        
        # Первая проверка сразу после запуска
        await self.check_all_watches()
        
        while self.running:
            try:
                await asyncio.sleep(self.check_interval)
                if self.running:
                    await self.check_all_watches()
            except asyncio.CancelledError:
                logger.info("⏹️ Наблюдатель остановлен по запросу")
                break
            except Exception as e:
                logger.error(f"❌ Критическая ошибка в наблюдателе: {e}")
                await asyncio.sleep(300)
    
    async def stop(self):
        """Остановить наблюдателя"""
        self.running = False
        logger.info("⏹️ Наблюдатель за ценами остановлен")
    
    async def check_all_watches(self):
        """Проверить все активные отслеживания"""
        watch_keys = await redis_client.get_all_watch_keys()
        if not watch_keys:
            logger.info("🔍 Нет активных отслеживаний для проверки")
            return
        
        logger.info(f"🔍 Начата проверка {len(watch_keys)} отслеживаний...")
        self.route_cache.clear()
        
        # Проверяем чанками по 5 маршрутов
        chunk_size = 5
        total_updated = 0
        total_removed = 0
        
        for i in range(0, len(watch_keys), chunk_size):
            if not self.running:
                break
                
            chunk = watch_keys[i:i + chunk_size]
            for key in chunk:
                if not self.running:
                    break
                    
                try:
                    raw_data = await redis_client.client.get(key)
                    if not raw_
                        continue
                    
                    watch_data = json.loads(raw_data)
                    result = await self.check_single_watch(watch_data, key)
                    
                    if result == "removed":
                        total_removed += 1
                    elif result:
                        total_updated += 1
                        
                except json.JSONDecodeError:
                    logger.warning(f"⚠️ Неверный формат данных для ключа {key}, удаляем")
                    await redis_client.client.delete(key)
                    total_removed += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка при проверке {key}: {e}")
            
            if i + chunk_size < len(watch_keys) and self.running:
                await asyncio.sleep(3)
        
        logger.info(
            f"✅ Проверка завершена: всего {len(watch_keys)}, изменений {total_updated}, удалено {total_removed}"
        )
    
    async def check_single_watch(self, watch: Dict, key: str) -> Optional[str]:
        """Проверить одно отслеживание. Возвращает: True если отправлено уведомление, 'removed' если удалено, иначе False"""
        user_id = watch["user_id"]
        origin = watch["origin"]
        dest = watch["dest"]
        depart_date = watch["depart_date"]
        return_date = watch.get("return_date")
        current_price = watch["current_price"]
        threshold = watch.get("threshold", 0)
        passengers = watch.get("passengers", "1")
        last_notified = watch.get("last_notified", 0)
        
        # Защита от спама: не уведомлять чаще чем раз в 24 часа
        hours_since_last = (time.time() - last_notified) / 3600
        if hours_since_last < 24:
            return False
        
        # Кэширование результатов поиска для одного маршрута
        cache_key = f"{origin}:{dest}:{depart_date}:{return_date or ''}"
        if cache_key in self.route_cache:
            cached_price, cached_time = self.route_cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                new_price = cached_price
            else:
                del self.route_cache[cache_key]
                new_price = await self._fetch_min_price(origin, dest, depart_date, return_date)
        else:
            new_price = await self._fetch_min_price(origin, dest, depart_date, return_date)
            if new_price:
                self.route_cache[cache_key] = (new_price, time.time())
        
        if not new_price:
            return False
        
        # Рассчитываем изменение с минимальным порогом 50₽ для избежания "дрожания"
        price_change = current_price - new_price
        abs_change = abs(price_change)
        should_notify = price_change != 0 and abs_change >= max(50, threshold)
        
        if not should_notify:
            # Тихо обновляем цену без уведомления
            if abs_change > 0:
                watch["current_price"] = new_price
                await redis_client.client.setex(
                    key,
                    86400 * 30,
                    json.dumps(watch, ensure_ascii=False)
                )
            return False
        
        # === ВСТРОЕННЫЙ ФРАГМЕНТ ОБРАБОТКИ БЛОКИРОВОК ===
        try:
            success = await self._send_price_notification(
                user_id=user_id,
                watch=watch,
                new_price=new_price,
                price_change=price_change,
                key=key
            )
        except Exception as e:
            logger.error(f"❌ Не удалось отправить уведомление пользователю {user_id}: {e}")
            # Если пользователь заблокировал бота — удаляем отслеживание автоматически
            if "blocked" in str(e).lower() or "user not found" in str(e).lower():
                await redis_client.remove_watch(user_id, key)
                logger.info(f"Автоматически удалено отслеживание для заблокировавшего пользователя {user_id}")
            return "removed"
        # === КОНЕЦ ВСТРОЕННОГО ФРАГМЕНТА ===
        
        if success:
            watch["current_price"] = new_price
            watch["last_notified"] = int(time.time())
            await redis_client.client.setex(
                key,
                86400 * 30,
                json.dumps(watch, ensure_ascii=False)
            )
            logger.info(f"✅ Уведомление отправлено {user_id}: {current_price} ₽ → {new_price} ₽ ({price_change:+d} ₽)")
            return True
        else:
            # Пользователь недоступен — удаляем отслеживание
            await redis_client.remove_watch(user_id, key)
            logger.warning(f"🗑️ Удалено отслеживание для недоступного пользователя {user_id}")
            return "removed"
    
    async def _fetch_min_price(self, origin: str, dest: str, depart_date: str, return_date: Optional[str]) -> Optional[int]:
        """Получить минимальную цену для маршрута"""
        try:
            flights = await search_flights(
                origin=origin,
                dest=dest,
                depart_date=normalize_date(depart_date),
                return_date=normalize_date(return_date) if return_date else None
            )
            if not flights:
                return None
            min_flight = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
            return min_flight.get("value") or min_flight.get("price") or None
        except Exception as e:
            logger.error(f"❌ Ошибка при поиске цен {origin}→{dest}: {e}")
            return None
    
    async def _send_price_notification(
        self,
        user_id: int,
        watch: Dict,
        new_price: int,
        price_change: int,
        key: str
    ) -> bool:
        """Отправить уведомление о смене цены. Возвращает True при успехе."""
        try:
            origin_name = IATA_TO_CITY.get(watch["origin"], watch["origin"])
            dest_name = IATA_TO_CITY.get(watch["dest"], watch["dest"])
            emoji = "📉" if price_change > 0 else "📈"
            passenger_desc = self._format_passengers(watch.get("passengers", "1"))
            
            message = (
                f"{emoji} <b>Цена изменилась!</b>\n\n"
                f"📍 <b>Маршрут:</b> {origin_name} → {dest_name}\n"
                f"📅 <b>Вылет:</b> {watch['depart_date']}\n"
            )
            
            if watch.get("return_date"):
                message += f"📅 <b>Возврат:</b> {watch['return_date']}\n"
            
            if passenger_desc:
                message += f"👥 <b>Пассажиры:</b> {passenger_desc}\n"
            
            message += (
                f"\n"
                f"💰 <b>Было:</b> {watch['current_price']} ₽\n"
                f"💰 <b>Стало:</b> {new_price} ₽\n"
                f"{emoji} <b>Разница:</b> {abs(price_change)} ₽\n\n"
                f"✈️ <b>Спешите забронировать — цены могут вырасти!</b>"
            )
            
            dummy_flight = {
                "value": new_price,
                "origin": watch["origin"],
                "destination": watch["dest"]
            }
            
            booking_link = generate_booking_link(
                flight=dummy_flight,
                origin=watch["origin"],
                dest=watch["dest"],
                depart_date=watch["depart_date"],
                passengers_code=watch.get("passengers", "1"),
                return_date=watch.get("return_date")
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"✈️ Забронировать за {new_price} ₽", url=booking_link)],
                [InlineKeyboardButton(text="❌ Больше не следить", callback_data=f"unwatch_{key}")]
            ])
            
            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
            
            return True
            
        except TelegramForbiddenError:
            logger.warning(f"⚠️ Пользователь {user_id} заблокировал бота")
            return False
        except TelegramRetryAfter as e:
            logger.warning(f"⚠️ Telegram rate limit для {user_id}, ждём {e.retry_after}с")
            await asyncio.sleep(e.retry_after)
            return False
        except TelegramAPIError as e:
            logger.error(f"❌ Ошибка Telegram API для {user_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Неизвестная ошибка при отправке уведомления {user_id}: {e}")
            raise  # Пробрасываем исключение для обработки в check_single_watch
    
    @staticmethod
    def _format_passengers(code: str) -> str:
        """Форматирует код пассажиров в читаемый вид"""
        try:
            adults = int(code[0])
            children = int(code[1]) if len(code) > 1 else 0
            infants = int(code[2]) if len(code) > 2 else 0
            
            parts = []
            if adults: parts.append(f"{adults} взр.")
            if children: parts.append(f"{children} реб.")
            if infants: parts.append(f"{infants} мл.")
            
            return ", ".join(parts) if parts else ""
        except:
            return ""