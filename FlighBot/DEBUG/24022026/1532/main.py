import asyncio
import os
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# Импорт роутеров
from handlers.start import router as start_router
from handlers.flystack_track import router as flystack_router
from handlers.everywhere_search import router as everywhere_router

# Импорт утилит и сервисов
from utils.logger import logger
from utils.redis_client import redis_client
from utils.cities_loader import load_cities_from_api
from services.price_watcher import PriceWatcher
from utils.link_converter import convert_to_partner_link
from handlers.hot_deals import router as hot_deals_router
from services.hot_deals_sender import HotDealsSender

# Настройка базового логирования
logging.basicConfig(level=logging.INFO)
load_dotenv()

async def main():
    # ─── 1. Подключение к Redis ───
    try:
        await redis_client.connect()
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Redis: {e}")
        logger.info("⚠️ Продолжаю работу без кэширования...")
    
    # ─── 2. Загрузка базы городов ───
    logger.info("🌍 Инициализация базы городов...")
    await load_cities_from_api()
    
    # ─── 3. Инициализация бота ───
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    # ─── 4. Инициализация диспетчера ───
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    
    # ─── 5. Регистрация роутеров ───
    dp.include_router(start_router)        
    logger.info("✅ Зарегистрирован роутер: start_router")
    dp.include_router(flystack_router)       
    logger.info("✅ Зарегистрирован роутер: flystack_router")
    dp.include_router(everywhere_router)     # Поиск "Везде"
    dp.include_router(hot_deals_router)
    hot_deals_sender = HotDealsSender(bot)
    hot_deals_task = asyncio.create_task(hot_deals_sender.start())
    
    # ─── 6. Запуск фоновых задач ───
    price_watcher = PriceWatcher(bot)
    watcher_task = asyncio.create_task(price_watcher.start())
    
    logger.info("🚀 Бот запущен!")
    logger.info(f"📋 Зарегистрированные роутеры: start_router, flystack_router")
    
    # ─── 7. Запуск polling ───
    try:
        await dp.start_polling(bot)
    finally:
        # Остановка при выключении
        price_watcher.running = False
        watcher_task.cancel()
        await redis_client.close()
        await bot.session.close()
        logger.info("✅ Бот остановлен, соединения закрыты")

if __name__ == "__main__":
    asyncio.run(main())