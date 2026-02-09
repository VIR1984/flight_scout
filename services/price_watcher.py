import asyncio
import os
from typing import Dict, List, Optional
from aiogram import Bot
from services.flight_search import search_flights, normalize_date
from utils.redis_client import redis_client
from utils.logger import logger
import json

async def check_all_watches(bot: Bot):
    """Проверяет все отслеживания цен и уведомляет пользователей при изменении."""
    logger.info("🔍 Начата проверка отслеживаний...")
    watch_keys = await redis_client.get_all_watch_keys()
    changes_count = 0
    removed_count = 0

    for key in watch_keys:
        try:
            raw_data = await redis_client.client.get(key)
            if not raw_data:  # ✅ ИСПРАВЛЕНО: проверяем raw_data, а не raw_
                await redis_client.remove_watch(None, key)  # user_id не нужен
                removed_count += 1
                continue

            data = json.loads(raw_data)
            user_id = data["user_id"]
            origin = data["origin"]
            dest = data["dest"]
            depart_date = data["depart_date"]
            return_date = data.get("return_date")  # Optional
            current_price = data["current_price"]
            passengers = data.get("passengers", "1")
            threshold = data.get("threshold", 0)  # 0 = любое изменение

            # Проверяем текущую цену
            flights = await search_flights(
                origin=origin,
                destination=dest,
                depart_date=normalize_date(depart_date),
                return_date=normalize_date(return_date) if return_date else None,
                currency="rub"
            )

            if not flights:
                # Если рейсов нет — удаляем отслеживание
                await redis_client.remove_watch(user_id, key)
                removed_count += 1
                continue

            # Находим новую цену на точную дату
            from services.flight_search import find_cheapest_flight_on_exact_date
            cheapest_on_date = find_cheapest_flight_on_exact_date(flights, depart_date, return_date)
            if not cheapest_on_date:
                # Цена на точную дату не найдена — удаляем отслеживание
                await redis_client.remove_watch(user_id, key)
                removed_count += 1
                continue

            new_price = cheapest_on_date.get("price") or cheapest_on_date.get("value")
            if new_price is None:
                continue

            # Проверяем порог
            if abs(current_price - new_price) >= threshold:
                # Цена изменилась — уведомляем
                direction = f"{origin} → {dest}"
                dates = f"{depart_date}" + (f" - {return_date}" if return_date else "")
                message = (
                    f"📉 Цена на маршрут <b>{direction}</b> изменилась!\n"
                    f"📅 {dates}\n"
                    f"💰 Было: {current_price} ₽\n"
                    f"💰 Стало: {new_price} ₽"
                )
                try:
                    await bot.send_message(chat_id=user_id, text=message)
                    changes_count += 1
                except Exception as e:
                    logger.error(f"❌ Не удалось отправить уведомление пользователю {user_id}: {e}")
                    # Если ошибка, возможно, юзер удалил бота — удаляем отслеживание
                    await redis_client.remove_watch(user_id, key)
                    removed_count += 1
                    continue

                # Обновляем цену в Redis
                data["current_price"] = new_price
                await redis_client.client.setex(key, 86400 * 30, json.dumps(data, ensure_ascii=False))

        except Exception as e:
            logger.error(f"❌ Ошибка при проверке {key}: {e}")

    logger.info(f"✅ Проверка завершена: всего {len(watch_keys)}, изменений {changes_count}, удалено {removed_count}")

async def run_price_checker(bot: Bot):
    """Цикл проверки цен каждые 6 часов."""
    while True:
        try:
            await check_all_watches(bot)
        except Exception as e:
            logger.error(f"❌ Ошибка в цикле проверки цен: {e}")
        await asyncio.sleep(6 * 3600)  # 6 часов