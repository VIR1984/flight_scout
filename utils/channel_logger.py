# utils/channel_logger.py
"""
Отправка сообщений в приватный Telegram-канал аналитики.

Переменные окружения (.env):
  ANALYTICS_CHANNEL_ID  — ID или @username канала (например: -1001234567890)

Типы сообщений:
  log_feedback()   — обратная связь от пользователя
  log_event()      — аналитическое событие (новый юзер, поиск, подписка)
  log_error()      — ошибка/исключение (отдельный раздел)
  log_stats()      — периодический сводный отчёт
"""
import os
import asyncio
import logging
import traceback
from datetime import datetime, timezone

import utils.bot_instance as _bot_instance

logger = logging.getLogger(__name__)

# ── Настройка ─────────────────────────────────────────────────────────────────

def _channel_id():
    raw = os.getenv("ANALYTICS_CHANNEL_ID", "")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw  # @username тоже валидно


async def _send(text: str, topic: str | None = None) -> bool:
    """Низкоуровневая отправка. topic — для будущих тем/тредов."""
    bot  = _bot_instance.bot
    cid  = _channel_id()
    if not bot or not cid:
        return False
    try:
        await bot.send_message(cid, text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as exc:
        logger.warning(f"[channel_logger] Не удалось отправить в канал: {exc}")
        return False


# ── Публичный API ─────────────────────────────────────────────────────────────

async def log_feedback(user_id: int, username: str | None, full_name: str, text: str):
    """Сообщение обратной связи от пользователя."""
    mention = f"@{username}" if username else f"<a href='tg://user?id={user_id}'>{full_name}</a>"
    msg = (
        "💬 <b>Обратная связь</b>\n"
        f"👤 {mention}  |  ID: <code>{user_id}</code>\n"
        f"⏰ {_now()}\n\n"
        f"{text}"
    )
    await _send(msg)


async def log_event(event: str, user_id: int | None = None,
                    username: str | None = None, detail: str = ""):
    """
    Аналитическое событие.
    event: 'new_user' | 'search' | 'subscription' | 'everywhere_search' | 'multi_search' | custom
    """
    icons = {
        "new_user":        "🆕",
        "search":          "🔍",
        "subscription":    "🔔",
        "everywhere_search":"🌍",
        "multi_search":    "🗺",
        "price_alert":     "📉",
    }
    icon = icons.get(event, "📊")
    user_str = ""
    if user_id:
        uname = f"@{username}" if username else f"id:{user_id}"
        user_str = f"  |  {uname}"
    msg = (
        f"{icon} <b>{event}</b>{user_str}\n"
        f"⏰ {_now()}"
    )
    if detail:
        msg += f"\n{detail}"
    await _send(msg)


async def log_error(context: str, exc: Exception | None = None, extra: str = ""):
    """
    Ошибка / исключение.
    Отправляется как отдельное сообщение с пометкой 🚨.
    """
    tb = ""
    if exc:
        tb = "\n".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-6:])

    msg = (
        "🚨 <b>ОШИБКА</b>\n"
        f"⏰ {_now()}\n"
        f"📍 <code>{context}</code>"
    )
    if extra:
        msg += f"\n{extra}"
    if tb:
        # Telegram ограничение 4096 символов
        tb_short = tb[-1200:]
        msg += f"\n\n<pre>{_escape(tb_short)}</pre>"
    await _send(msg)


async def log_stats(stats: dict):
    """
    Сводная статистика.
    stats — произвольный dict с числовыми показателями.
    Пример: {"users": 100, "searches_today": 45, "subscriptions": 12}
    """
    lines = [f"📊 <b>Статистика</b>  |  {_now()}\n"]
    for key, val in stats.items():
        lines.append(f"  • {key}: <b>{val}</b>")
    await _send("\n".join(lines))


# ── Вспомогательное ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Logging Handler для автоматической отправки ERROR-логов ──────────────────

class ChannelLogHandler(logging.Handler):
    """
    Добавьте в main.py:
        from utils.channel_logger import ChannelLogHandler
        logging.getLogger().addHandler(ChannelLogHandler(level=logging.ERROR))

    Все ERROR и выше будут дублироваться в канал.
    Дебаунс: не чаще одного сообщения в 10 секунд на один logger-name.
    """
    _last_sent: dict[str, float] = {}
    DEBOUNCE = 10.0  # секунд

    def emit(self, record: logging.LogRecord):
        import time
        now = time.monotonic()
        key = record.name
        if now - self._last_sent.get(key, 0) < self.DEBOUNCE:
            return
        self._last_sent[key] = now

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self._async_emit(record))
        except RuntimeError:
            pass

    async def _async_emit(self, record: logging.LogRecord):
        level_icon = {"ERROR": "🔴", "CRITICAL": "💀", "WARNING": "🟡"}.get(record.levelname, "⚪")
        msg = (
            f"{level_icon} <b>{record.levelname}</b>  |  <code>{record.name}</code>\n"
            f"⏰ {_now()}\n\n"
            f"<pre>{_escape(self.format(record))[:1500]}</pre>"
        )
        await _send(msg)