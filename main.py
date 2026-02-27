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
from handlers.hot_deals import router as hot_deals_router
from handlers.flight_wizard import router as flight_wizard_router

# Импорт утилит и сервисов
from utils.logger import logger
from utils.redis_client import redis_client
from utils.cities_loader import load_cities_from_api
from services.price_watcher import PriceWatcher
from services.hot_deals_sender import HotDealsSender

# Уровень логирования: DEBUG — видим все детали, INFO — только важное
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
load_dotenv()


async def main():
    # ─── 1. Подключение к Redis ───
    try:
        await redis_client.connect()
        logger.info("✅ Redis подключён")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Redis: {e}")
        logger.warning("⚠️ Продолжаю работу без кэширования...")

    # ─── 2. Загрузка базы городов ───
    logger.info("🌍 Загружаю базу городов...")
    await load_cities_from_api()

    # ─── 3. Инициализация бота ───
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    # Регистрируем синглтон для использования в фоновых задачах
    import utils.bot_instance as _bot_instance
    _bot_instance.bot = bot

    # ─── 4. Диспетчер ───
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # ─── 5. Регистрация роутеров ───
    # ВАЖНО: hot_deals_router должен быть ДО start_router,
    # потому что start_router раньше перехватывал hot_deals_menu
    dp.include_router(hot_deals_router)
    logger.info("✅ Роутер: hot_deals_router")

    # flight_wizard перехватывает start_search и новые FSM-состояния
    dp.include_router(flight_wizard_router)
    logger.info("✅ Роутер: flight_wizard_router")

    dp.include_router(start_router)
    logger.info("✅ Роутер: start_router")

    dp.include_router(flystack_router)
    logger.info("✅ Роутер: flystack_router")

    dp.include_router(everywhere_router)
    logger.info("✅ Роутер: everywhere_router")

    # ─── 6. Фоновые задачи ───
    price_watcher = PriceWatcher(bot)
    watcher_task = asyncio.create_task(price_watcher.start())
    logger.info("✅ PriceWatcher запущен")

    hot_deals_sender = HotDealsSender(bot)
    hot_deals_task = asyncio.create_task(hot_deals_sender.start())
    logger.info("✅ HotDealsSender запущен")

    logger.info("🚀 Бот запущен! Ожидаю сообщения...")

    # ─── 7. Polling ───
    try:
        await dp.start_polling(bot)
    finally:
        logger.info("🛑 Остановка бота...")

        # Останавливаем фоновые задачи
        price_watcher.running = False
        hot_deals_sender.stop()

        watcher_task.cancel()
        hot_deals_task.cancel()

        # Ждём завершения
        for task in [watcher_task, hot_deals_task]:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        await redis_client.close()
        await bot.session.close()
        logger.info("✅ Бот остановлен, соединения закрыты")


if __name__ == "__main__":
    asyncio.run(main())