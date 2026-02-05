import asyncio
import os
import sys
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv
from handlers.start import router as start_router
from utils.logger import logger
from utils.redis_client import redis_client
import logging
from services.price_watcher import PriceWatcher

logging.basicConfig(level=logging.DEBUG)
load_dotenv()

async def main():
    # Подключение к Redis
    try:
        await redis_client.connect()
    except Exception as e:
        logger.error(f"Ошибка подключения к Redis: {e}")
        logger.info("Продолжаю работу без кэширования...")
        sys.exit(1)
    
    # Инициализация бота
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    # Инициализация наблюдателя за ценами
    price_watcher = PriceWatcher(bot)
    
    dp = Dispatcher()
    dp.include_router(start_router)
    
    logger.info("🚀 Бот запущен!")
    
    # Запуск наблюдателя в фоне
    watcher_task = asyncio.create_task(price_watcher.start())
    
    try:
        await dp.start_polling(bot)
    finally:
        # Остановка наблюдателя
        price_watcher.running = False
        watcher_task.cancel()
        
        # Закрытие соединения с Redis
        await redis_client.close()
        logger.info("✅ Redis соединение закрыто")

if __name__ == "__main__":
    asyncio.run(main())