# handlers/nav_router.py
"""
Роутер навигационных кнопок — регистрируется ПЕРВЫМ в main.py.
Гарантирует что нажатие любой кнопки нав-панели всегда сбросит
любое FSM-состояние и вызовет правильный хендлер, независимо от
того в каком состоянии находится пользователь.

ТАКЖЕ перехватывает команды (/start, /stats и т.д.) при активном
FSM-состоянии — без этого они попадают в FSM-хендлеры роутеров
которые стоят раньше start_router в main.py.
"""
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext

from utils.smart_reminder import cancel_inactivity, mark_fsm_inactive
from handlers.start import (
    cmd_start, cmd_stats, cmd_sendstats, cmd_feedback_log,
    nav_search, nav_multi_search, nav_hot, nav_subs, nav_feedback,
)
from handlers.help import show_help

router = Router()


async def _reset_state(message: Message, state: FSMContext):
    """Сбрасываем любое FSM-состояние перед nav-действием."""
    current = await state.get_state()
    if current:
        await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)


# ════════════════════════════════════════════════════════════════
# Команды — перехватываем при ЛЮБОМ состоянии FSM
# ════════════════════════════════════════════════════════════════

@router.message(CommandStart())
async def nav_cmd_start(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await cmd_start(message, state)


@router.message(Command("stats"))
async def nav_cmd_stats(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await cmd_stats(message)


@router.message(Command("sendstats"))
async def nav_cmd_sendstats(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await cmd_sendstats(message)


@router.message(Command("feedback_log"))
async def nav_cmd_feedback_log(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await cmd_feedback_log(message)


# ════════════════════════════════════════════════════════════════
# Кнопки навигационной панели
# ════════════════════════════════════════════════════════════════

@router.message(F.text == "✈️ Поиск")
async def _nav_search(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_search(message, state)


@router.message(F.text == "🗺 Маршрут")
async def _nav_multi(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_multi_search(message, state)


@router.message(F.text == "🔥 Горячие")
async def _nav_hot(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_hot(message, state)


@router.message(F.text == "📋 Подписки")
async def _nav_subs(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_subs(message, state)


@router.message(F.text == "❓ Помощь")
async def _nav_help(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await show_help(message)


@router.message(F.text == "💬 Обратная связь")
async def _nav_feedback(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_feedback(message, state)


# ════════════════════════════════════════════════════════════════
# Команды из меню Telegram
# ════════════════════════════════════════════════════════════════

@router.message(Command("search"))
async def nav_cmd_search(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_search(message, state)


@router.message(Command("hot"))
async def nav_cmd_hot(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_hot(message, state)


@router.message(Command("subs"))
async def nav_cmd_subs(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_subs(message, state)


@router.message(Command("feedback"))
async def nav_cmd_feedback(message: Message, state: FSMContext):
    await _reset_state(message, state)
    await nav_feedback(message, state)