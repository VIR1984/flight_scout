# utils/smart_reminder.py
"""
Умные напоминания при бездействии в FSM и после поиска.

Правила:
  • Таймер ЗАПУСКАЕТСЯ только когда бот ждёт ввода от пользователя.
  • Таймер СБРАСЫВАЕТСЯ при любом действии: кнопка, текст, смена шага.
  • Таймер ОТМЕНЯЕТСЯ при завершении FSM (успех / отмена / главное меню).
  • Напоминание НЕ отправляется, если FSM уже завершён.
  • Вау-цены НЕ предлагаются:
      — если пользователь уже подписан на hot_deals
      — чаще одного раза в 24 часа на пользователя
"""

import asyncio
import logging
import time
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# ── Состояние таймеров ────────────────────────────────────────────────────────
# chat_id → asyncio.Task
_inactivity_tasks: dict[int, asyncio.Task] = {}

# chat_id → True если пользователь сейчас в FSM
_fsm_active: dict[int, bool] = {}

# user_id → timestamp последнего напоминания про вау-цены
_last_hot_deals_reminder: dict[int, float] = {}

_HOT_DEALS_COOLDOWN = 24 * 3600  # 24 часа


# ── Управление FSM-флагом ────────────────────────────────────────────────────

def mark_fsm_active(chat_id: int) -> None:
    """Пометить: бот ждёт ввода от пользователя."""
    _fsm_active[chat_id] = True


def mark_fsm_inactive(chat_id: int) -> None:
    """Пометить: FSM завершён (успех / отмена)."""
    _fsm_active[chat_id] = False


# ── Управление таймером ──────────────────────────────────────────────────────

def cancel_inactivity(chat_id: int) -> None:
    """
    Отменить текущий таймер бездействия.
    Вызывать при ЛЮБОМ действии пользователя: кнопка, текст, смена шага.
    """
    task = _inactivity_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def schedule_inactivity(chat_id: int, user_id: int,
                         delay_1_min: int = 15,
                         delay_2_min: int = 20) -> None:
    """
    Запустить / перезапустить таймер бездействия.

    delay_1_min  — первое напоминание (продолжить поиск), default 15 мин.
    delay_2_min  — второе напоминание (вау-цены если актуально), default 20 мин.

    Вызывать только когда бот ждёт ввода.
    """
    cancel_inactivity(chat_id)
    mark_fsm_active(chat_id)
    task = asyncio.create_task(
        _inactivity_reminder(chat_id, user_id, delay_1_min, delay_2_min)
    )
    _inactivity_tasks[chat_id] = task


async def _inactivity_reminder(chat_id: int, user_id: int,
                                delay_1_min: int, delay_2_min: int) -> None:
    """Двухэтапное напоминание при бездействии."""
    try:
        # ── Этап 1: напоминание продолжить ────────────────────────
        await asyncio.sleep(delay_1_min * 60)

        if not _fsm_active.get(chat_id):
            return  # FSM завершён — не беспокоим

        bot = await _get_bot()
        if not bot:
            return

        kb1 = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить поиск",    callback_data="continue_search")],
            [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
            [InlineKeyboardButton(text="↩️ В начало",            callback_data="main_menu")],
        ])
        await bot.send_message(
            chat_id,
            "👋 Ещё ищешь билеты? Продолжим?",
            reply_markup=kb1,
        )
        logger.info(f"[SmartReminder] Напоминание #1 → chat_id={chat_id}")

        # ── Этап 2: вау-цены (только если актуально) ──────────────
        await asyncio.sleep((delay_2_min - delay_1_min) * 60)

        if not _fsm_active.get(chat_id):
            return

        if not await _should_send_hot_deals(user_id):
            return

        bot = await _get_bot()
        if not bot:
            return

        kb2 = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Подписаться на вау-цены", callback_data="hot_deals_menu")],
            [InlineKeyboardButton(text="✈️ Найти билеты",            callback_data="start_search")],
        ])
        await bot.send_message(
            chat_id,
            "💡 Пока думаешь — можно подписаться на вау-цены: "
            "я сам сообщу, когда появятся горячие билеты по интересным направлениям.",
            reply_markup=kb2,
        )
        _last_hot_deals_reminder[user_id] = time.time()
        logger.info(f"[SmartReminder] Вау-цены #2 → chat_id={chat_id}")

    except asyncio.CancelledError:
        pass  # пользователь активен — нормально
    except Exception as e:
        logger.debug(f"[SmartReminder] Ошибка: {e}")
    finally:
        _inactivity_tasks.pop(chat_id, None)


# ── Напоминание после завершения поиска ─────────────────────────────────────

async def remind_after_search(chat_id: int, user_id: int,
                               delay_min: int = 15) -> None:
    """
    Через delay_min минут после успешного поиска предлагаем вау-цены.
    Только если пользователь ещё не подписан и не получал напоминание сегодня.
    """
    await asyncio.sleep(delay_min * 60)

    if not await _should_send_hot_deals(user_id):
        return

    bot = await _get_bot()
    if not bot:
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Подписаться на вау-цены", callback_data="hot_deals_menu")],
        [InlineKeyboardButton(text="✈️ Новый поиск",             callback_data="start_search")],
    ])
    try:
        await bot.send_message(
            chat_id,
            "💡 Хочешь узнавать о горячих ценах первыми? "
            "Укажи направления — и я сообщу, как только появятся выгодные билеты.",
            reply_markup=kb,
        )
        _last_hot_deals_reminder[user_id] = time.time()
        logger.info(f"[SmartReminder] After-search вау-цены → user_id={user_id}")
    except Exception as e:
        logger.debug(f"[SmartReminder] After-search ошибка: {e}")


# ── Вспомогательные ──────────────────────────────────────────────────────────

async def _should_send_hot_deals(user_id: int) -> bool:
    """
    True если можно отправить напоминание про вау-цены:
      — не получал в последние 24 часа
      — не подписан на горячие предложения
    """
    # Проверка cooldown
    last = _last_hot_deals_reminder.get(user_id, 0)
    if time.time() - last < _HOT_DEALS_COOLDOWN:
        return False

    # Проверка подписки в Redis
    try:
        from utils.redis_client import redis_client
        all_subs = await redis_client.get_all_hot_subs()
        user_sub_ids = [uid for uid, _, _ in all_subs]
        if user_id in user_sub_ids:
            return False
    except Exception:
        pass  # Redis недоступен — разрешаем

    return True


async def _get_bot():
    """Получить синглтон бота."""
    try:
        from utils.bot_instance import bot as _bot
        return _bot
    except Exception:
        return None