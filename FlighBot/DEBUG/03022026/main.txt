import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from handlers.start import router as start_router
from utils.logger import logger
import logging

logging.basicConfig(level=logging.DEBUG)

load_dotenv()

async def main():
    bot = Bot(
        token=os.getenv("BOT_TOKEN"),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(start_router)

    logger.info("Бот запущен!")
    await dp.start_polling(bot)



if __name__ == "__main__":
    asyncio.run(main())