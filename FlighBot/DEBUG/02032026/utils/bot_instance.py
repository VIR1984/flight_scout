# utils/bot_instance.py
"""
Синглтон экземпляра бота.
Инициализируется в main.py: bot_instance.bot = Bot(...)
Используется в фоновых задачах (напоминания, уведомления) для отправки сообщений.
"""
from typing import Optional
from aiogram import Bot

bot: Optional[Bot] = None