# utils/daily_stats.py
"""
Фоновый сервис ежедневной отправки статистики в канал.

Запускается один раз при старте бота (main.py).
Каждый день в DAILY_REPORT_HOUR:00 UTC отправляет полный отчёт.
"""
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Время отправки (UTC). Можно переопределить через .env
import os
DAILY_REPORT_HOUR = int(os.getenv("DAILY_STATS_HOUR", "9"))   # 09:00 UTC по умолчанию


async def _seconds_until_next_report() -> float:
    """Сколько секунд до следующего DAILY_REPORT_HOUR:00 UTC."""
    now = datetime.now(timezone.utc)
    next_run = now.replace(hour=DAILY_REPORT_HOUR, minute=0, second=0, microsecond=0)
    if next_run <= now:
        # Уже прошло сегодня — ждём завтра
        from datetime import timedelta
        next_run += timedelta(days=1)
    delta = (next_run - now).total_seconds()
    return delta


async def start():
    """
    Основной цикл. Вызывать через asyncio.create_task(daily_stats.start()).
    """
    logger.info(f"[DailyStats] Сервис запущен. Отчёт каждый день в {DAILY_REPORT_HOUR:02d}:00 UTC")

    try:
        while True:
            wait = await _seconds_until_next_report()
            hours = int(wait // 3600)
            mins  = int((wait % 3600) // 60)
            logger.info(f"[DailyStats] Следующий отчёт через {hours}ч {mins}м")

            await asyncio.sleep(wait)

            try:
                await _send_report()
            except Exception as exc:
                import traceback
                logger.error(f"[DailyStats] Ошибка при отправке отчёта: {exc}\n{traceback.format_exc()}")
                # Уведомляем в канал об ошибке
                try:
                    from utils.channel_logger import log_error
                    await log_error("DailyStats", exc)
                except Exception:
                    pass

            # Небольшая пауза чтобы не запустить дважды в одну минуту
            await asyncio.sleep(70)
    except asyncio.CancelledError:
        logger.info("[DailyStats] Задача остановлена")
        raise
    except Exception as exc:
        import traceback
        logger.critical(f"[DailyStats] Фатальная ошибка — сервис остановлен: {exc}\n{traceback.format_exc()}")


async def _send_report():
    """Собирает аналитику и отправляет отчёт."""
    from utils.redis_client import redis_client
    from utils.channel_logger import send_daily_report

    logger.info("[DailyStats] Собираю аналитику...")

    if not redis_client.client:
        raise RuntimeError("Redis недоступен — аналитика не собрана")

    an = await redis_client.get_analytics()
    if not an:
        raise RuntimeError("get_analytics() вернул пустой результат")

    ok = await send_daily_report(an, triggered_by="auto")
    if not ok:
        raise RuntimeError("Отчёт не отправлен — проверь ANALYTICS_CHANNEL_ID и что бот добавлен в канал как администратор")

    logger.info("[DailyStats] ✅ Ежедневный отчёт отправлен в канал")


async def send_now():
    """Немедленная отправка отчёта по запросу (из команды /sendstats)."""
    await _send_report()