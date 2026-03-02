# main.py
import asyncio
import os
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from handlers.start import router as start_router
from handlers.flight_wizard import router as wizard_router
from utils.logger import logger
from utils.redis_client import redis_client
from services.price_watcher import PriceWatcher

# Загрузка переменных окружения
load_dotenv()

# Настройка корневого логгера (уровень INFO для продакшена)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

async def main():
    # Подключение к Redis
    try:
        await redis_client.connect()
        if not redis_client.is_enabled():
            logger.warning("⚠️ Redis недоступен — работа без кэширования и отслеживания цен")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Redis: {e}")
        logger.warning("⚠️ Продолжаю работу без кэширования...")

    # Проверка токена бота
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logger.error("❌ BOT_TOKEN не задан в .env файле!")
        logger.error("Создайте файл .env с содержимым: BOT_TOKEN=ваш_токен")
        return

    # Инициализация бота
    bot = Bot(
        token=bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # Инициализация диспетчера
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Регистрация роутеров
    dp.include_router(start_router)
    dp.include_router(wizard_router)

    # Инициализация наблюдателя (только если Redis доступен)
    price_watcher = None
    watcher_task = None
    if redis_client.is_enabled():
        price_watcher = PriceWatcher(bot)
        watcher_task = asyncio.create_task(price_watcher.start())
        logger.info("✅ Наблюдатель за ценами запущен (проверка каждые 6 часов)")
    else:
        logger.warning("⚠️ Наблюдатель за ценами отключён (требуется Redis)")

    logger.info("🚀 Бот запущен! Нажмите Ctrl+C для остановки")

    # Запуск поллинга
    try:
        await dp.start_polling(bot)
    finally:
        # Остановка наблюдателя
        if price_watcher and price_watcher.running:
            logger.info("⏹️ Остановка наблюдателя за ценами...")
            price_watcher.running = False
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass

        # Закрытие соединения с Redis
        await redis_client.close()

        # Закрытие сессии бота
        await bot.session.close()

        logger.info("✅ Бот остановлен корректно")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при запуске бота: {e}")