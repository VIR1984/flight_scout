# utils/daily_stats.py
"""
Фоновый сервис ежедневной отправки статистики в канал.

Запускается один раз при старте бота (main.py).
Каждый день в DAILY_REPORT_HOUR:00 UTC отправляет полный отчёт.
"""
import asyncio
import logging
from datetime import datetime, timezone, date, timedelta

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


async def cleanup_expired_months():
    """
    Удаляет из всех горячих подписок месяцы которые уже прошли.
    Вызывается раз в сутки из фонового цикла daily_stats.
    """
    from utils.redis_client import redis_client
    from datetime import date

    if not redis_client.client:
        return

    today     = date.today()
    cur_year  = today.year
    cur_month = today.month
    removed_total = 0

    try:
        all_subs = await redis_client.get_all_hot_subs()
    except Exception as exc:
        logger.warning(f"[cleanup_months] Не удалось получить подписки: {exc}")
        return

    for user_id, sub_id, sub in all_subs:
        months = sub.get("travel_months", [])
        if not months:
            continue

        fresh = []
        for mk in months:
            try:
                m, y = map(int, mk.split("_"))
                if (y, m) >= (cur_year, cur_month):
                    fresh.append(mk)
            except Exception:
                fresh.append(mk)

        removed = len(months) - len(fresh)
        if removed > 0:
            sub["travel_months"] = fresh
            try:
                await redis_client.update_hot_sub(user_id, sub_id, sub)
                removed_total += removed
            except Exception as exc:
                logger.warning(f"[cleanup_months] Ошибка sub={sub_id}: {exc}")

    if removed_total:
        logger.info(f"[cleanup_months] ✅ Удалено {removed_total} устаревших месяцев из подписок")


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

            # Чистим устаревшие месяцы из подписок раз в сутки
            try:
                await cleanup_expired_months()
            except Exception as _ce:
                logger.warning(f"[DailyStats] cleanup_months: {_ce}")

            # Небольшая пауза чтобы не запустить дважды в одну минуту
            await asyncio.sleep(70)
    except asyncio.CancelledError:
        logger.info("[DailyStats] Задача остановлена")
        raise
    except Exception as exc:
        import traceback
        logger.critical(f"[DailyStats] Фатальная ошибка — сервис остановлен: {exc}\n{traceback.format_exc()}")


async def health_check() -> dict:
    """
    Проверяет состояние всех ключевых функций бота.
    Возвращает словарь {component: status_str}.
    Вызывается раз в сутки, результат идёт в канал как отдельный блок.
    """
    from utils.redis_client import redis_client
    from services.flight_search import search_flights, normalize_date
    from datetime import date, timedelta
    import time

    results = {}

    # 1. Redis
    try:
        if redis_client.client:
            await redis_client.client.ping()
            results["Redis"] = "✅ OK"
        else:
            results["Redis"] = "❌ Нет подключения"
    except Exception as e:
        results["Redis"] = f"❌ {e}"

    # 2. Aviasales API (тестовый запрос MOW→LED через 30 дней)
    try:
        t0 = time.monotonic()
        test_date = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
        flights = await search_flights("MOW", "LED", test_date, None)
        ms = int((time.monotonic() - t0) * 1000)
        if flights:
            results["Aviasales API"] = f"✅ OK  ({len(flights)} рейсов, {ms}ms)"
        else:
            results["Aviasales API"] = f"⚠️ Ответ пустой ({ms}ms)"
    except Exception as e:
        results["Aviasales API"] = f"❌ {e}"

    # 3. Travelpayouts (partner link) — просто проверяем переменные
    import os
    tp_token = os.getenv("TRAVELPAYOUTS_API_TOKEN") or os.getenv("AVIASALES_TOKEN", "")
    results["Travelpayouts"] = "✅ Токен задан" if tp_token else "❌ Токен не задан"

    # 4. Подписки горячих предложений
    try:
        all_subs = await redis_client.get_all_hot_subs()
        results["Горячие подписки"] = f"✅ {len(all_subs)} активных"
    except Exception as e:
        results["Горячие подписки"] = f"❌ {e}"

    # 5. Слежение за ценами
    try:
        watches = await redis_client.get_all_watch_keys()
        results["Слежение за ценами"] = f"✅ {len(watches)} активных"
    except Exception as e:
        results["Слежение за ценами"] = f"❌ {e}"

    # 6. Канал аналитики
    channel_id = os.getenv("ANALYTICS_CHANNEL_ID", "")
    results["Канал аналитики"] = "✅ Задан" if channel_id else "❌ Не задан"

    return results


async def _send_report():
    """Собирает аналитику, health check и отправляет отчёт."""
    from utils.redis_client import redis_client
    from utils.channel_logger import send_daily_report, _send

    logger.info("[DailyStats] Собираю аналитику...")

    if not redis_client.client:
        raise RuntimeError("Redis недоступен — аналитика не собрана")

    an = await redis_client.get_analytics()
    if not an:
        raise RuntimeError("get_analytics() вернул пустой результат")

    ok = await send_daily_report(an, triggered_by="auto")
    if not ok:
        raise RuntimeError("Отчёт не отправлен — проверь ANALYTICS_CHANNEL_ID и что бот добавлен в канал как администратор")

    # Health check — отдельным сообщением после основного отчёта
    try:
        hc = await health_check()
        lines = ["🏥 <b>Health Check</b>\n"]
        all_ok = all("✅" in v for v in hc.values())
        for component, status in hc.items():
            lines.append(f"  {status}  —  {component}")
        lines.append("")
        lines.append("✅ Все системы в норме" if all_ok else "⚠️ Есть проблемы — проверь выше")
        await _send("\n".join(lines))
        logger.info("[DailyStats] ✅ Health check отправлен")
    except Exception as e:
        logger.warning(f"[DailyStats] Health check не отправлен: {e}")

    logger.info("[DailyStats] ✅ Ежедневный отчёт отправлен в канал")


async def send_now():
    """Немедленная отправка отчёта по запросу (из команды /sendstats)."""
    await _send_report()