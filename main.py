# main.py
"""
Точка входа авиабота — инициализация, подключение к сервисам, запуск поллинга
"""
import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv
from handlers.start import router as start_router
from handlers.flight_wizard import router as wizard_router
from utils.logger import logger
from utils.redis_client import redis_client
from services.price_watcher import PriceWatcher

# Загрузка переменных окружения из .env
load_dotenv()

# Настройка корневого логгера (уровень INFO для продакшена)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# ===== Глобальный роутер для обработки отмены из ЛЮБОГО состояния =====
global_router = Router()

@global_router.callback_query(lambda c: c.data == "cancel_search")
async def cancel_search(callback: CallbackQuery, state: FSMContext):
    """Глобальный обработчик отмены поиска (работает из любого состояния)"""
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")],
        [InlineKeyboardButton(text="💡 Ручной ввод", callback_data="manual_input")]
    ])
    try:
        await callback.message.edit_text(
            "❌ Поиск отменён.\n"
            "Выберите действие:",
            reply_markup=kb
        )
    except Exception:
        await callback.message.answer(
            "❌ Поиск отменён.\n"
            "Выберите действие:",
            reply_markup=kb
        )
    await callback.answer()

@global_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Глобальная команда /cancel для отмены любого активного поиска"""
    current_state = await state.get_state()
    if not current_state:
        await message.answer("ℹ️ Нет активного поиска для отмены.")
        return
    
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")],
        [InlineKeyboardButton(text="💡 Ручной ввод", callback_data="manual_input")]
    ])
    await message.answer(
        "❌ Поиск отменён.\n"
        "Выберите действие:",
        reply_markup=kb
    )

async def main():
    # ===== 1. Проверка токена бота =====
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token or bot_token.strip() == "":
        logger.error("❌ BOT_TOKEN не задан в .env файле!")
        logger.error("Создайте файл .env с содержимым: BOT_TOKEN=ваш_токен_из_@BotFather")
        return

    # ===== 2. Подключение к Redis =====
    redis_enabled = False
    try:
        await redis_client.connect()
        if redis_client.is_enabled():
            redis_enabled = True
            logger.info("✅ Redis подключён")
        else:
            logger.warning("⚠️ Redis недоступен — работа без кэширования и отслеживания цен")
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к Redis: {e}")
        logger.warning("⚠️ Продолжаю работу без кэширования...")

    # ===== 3. Инициализация бота =====
    bot = Bot(
        token=bot_token.strip(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    # ===== 4. Инициализация диспетчера =====
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # ===== 5. Регистрация роутеров (важен порядок!) =====
    # Сначала глобальный роутер для отмены — чтобы работал из любого состояния
    dp.include_router(global_router)
    # Затем основные роутеры
    dp.include_router(start_router)
    dp.include_router(wizard_router)

    # ===== 6. Инициализация наблюдателя за ценами =====
    price_watcher = None
    watcher_task = None
    if redis_enabled:
        price_watcher = PriceWatcher(bot)
        watcher_task = asyncio.create_task(price_watcher.start())
        logger.info("✅ Наблюдатель за ценами запущен (проверка каждые 6 часов)")
    else:
        logger.warning("⚠️ Наблюдатель за ценами отключён (требуется Redis)")

    # ===== 7. Запуск поллинга =====
    logger.info("🚀 Бот запущен! Нажмите Ctrl+C для остановки")
    
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
        logger.info("✅ Redis соединение закрыто")

        # Закрытие сессии бота (освобождение ресурсов)
        await bot.session.close()
        logger.info("✅ Сессия бота закрыта")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Бот остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logger.exception(f"❌ Критическая ошибка при запуске бота: {e}")
        raise