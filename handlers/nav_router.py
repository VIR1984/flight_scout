# handlers/nav_router.py
"""
Роутер навигационных кнопок — регистрируется ПЕРВЫМ в main.py.
Гарантирует что нажатие любой кнопки нав-панели всегда сбросит
любое FSM-состояние и вызовет правильный хендлер, независимо от
того в каком состоянии находится пользователь.
"""
from aiogram import Router, F
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from utils.smart_reminder import cancel_inactivity, mark_fsm_inactive

router = Router()


async def _reset_state(message: Message, state: FSMContext):
    """Сбрасываем любое FSM-состояние перед nav-действием."""
    current = await state.get_state()
    if current:
        await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)


@router.message(F.text == "✈️ Поиск")
async def nav_search(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_search as _handler
    await _handler(message, state)


@router.message(F.text == "🗺 Маршрут")
async def nav_multi(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_multi_search as _handler
    await _handler(message, state)


@router.message(F.text == "🔥 Горячие")
async def nav_hot(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_hot as _handler
    await _handler(message, state)


@router.message(F.text == "📋 Подписки")
async def nav_subs(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_subs as _handler
    await _handler(message, state)


@router.message(F.text == "❓ Помощь")
async def nav_help(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_help as _handler
    await _handler(message, state)


@router.message(F.text == "💬 Обратная связь")
async def nav_feedback(message: Message, state: FSMContext):
    await _reset_state(message, state)
    from handlers.start import nav_feedback as _handler
    await _handler(message, state)