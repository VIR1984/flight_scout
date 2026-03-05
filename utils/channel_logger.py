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
    bot = _bot_instance.bot
    cid = _channel_id()
    if not bot:
        raise RuntimeError("bot instance не инициализирован — бот ещё не запущен")
    if not cid:
        raise RuntimeError(
            "ANALYTICS_CHANNEL_ID не задан в переменных окружения"
        )
    try:
        await bot.send_message(cid, text, parse_mode="HTML", disable_web_page_preview=True)
        return True
    except Exception as exc:
        # Пробрасываем наружу — пусть вызывающий код решает что делать
        raise RuntimeError(f"Telegram не принял сообщение в канал {cid}: {exc}") from exc


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
    try:
        await _send(msg)
    except Exception as e:
        logger.warning(f"[log_feedback] {e}")


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
    try:
        await _send(msg)
    except Exception as e:
        logger.warning(f"[log_event] {e}")


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
    try:
        await _send(msg)
    except Exception as e:
        logger.warning(f"[log_error] {e}")


async def log_stats(stats: dict):
    """
    Сводная статистика (простой формат — для обратной совместимости).
    """
    lines = [f"📊 <b>Статистика</b>  |  {_now()}\n"]
    for key, val in stats.items():
        lines.append(f"  • {key}: <b>{val}</b>")
    await _send("\n".join(lines))


async def send_daily_report(an: dict, triggered_by: str = "auto") -> bool:
    """
    Красивый ежедневный отчёт в канал.
    an — словарь из redis_client.get_analytics()
    triggered_by — 'auto' (планировщик) или 'admin' (ручной запрос)
    Отправляет несколько сообщений — по блокам.
    """

    def bar(count: int, max_count: int, width: int = 10) -> str:
        if not max_count:
            return "░" * width
        filled = round(width * count / max_count)
        return "█" * filled + "░" * (width - filled)

    label = "🕘 Ежедневный отчёт" if triggered_by == "auto" else "📤 Отчёт по запросу"
    header = (
        f"{'─' * 30}\n"
        f"{label}\n"
        f"📅 {_now()}\n"
        f"{'─' * 30}"
    )
    # Первая отправка — если упадёт, сразу узнаем о проблеме
    try:
        await _send(header)
    except Exception as exc:
        logger.error(f"[send_daily_report] Не удалось отправить в канал: {exc}")
        raise  # пробрасываем в _send_report → там поймает и залогирует

    # ── Блок 1: Ключевые метрики ─────────────────────────────────
    searches  = an.get("total_searches", 0)
    no_res    = an.get("total_no_results", 0)
    users     = an.get("total_users", 0)
    s_users   = an.get("searching_users", 0)
    subs      = an.get("active_subscriptions", 0)
    watches   = an.get("price_watches", 0)
    sr_rate   = f"{round((searches - no_res) / searches * 100)}%" if searches else "—"

    day_data  = an.get("searches_by_day", {})
    today_cnt = 0
    if day_data:
        from datetime import datetime, timezone
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_cnt = day_data.get(today_key, 0)

    b1 = (
        "👥 <b>Пользователи</b>\n"
        f"  Всего зарегистрировано:  <b>{users}</b>\n"
        f"  Выполняли поиск:         <b>{s_users}</b>\n"
        f"  Конверсия в поиск:       <b>{'—' if not users else str(round(s_users/users*100)) + '%'}</b>\n\n"
        "🔍 <b>Поиски</b>\n"
        f"  Всего поисков:           <b>{searches}</b>\n"
        f"  Сегодня:                 <b>{today_cnt}</b>\n"
        f"  Нашли рейсы:             <b>{sr_rate}</b>\n\n"
        "🔔 <b>Подписки и слежение</b>\n"
        f"  Активных подписок:       <b>{subs}</b>\n"
        f"  Отслеживают цены:        <b>{watches}</b>"
    )
    await _send(b1)

    # ── Блок 2: Топ-10 направлений ───────────────────────────────
    top_dest = an.get("top_destinations", [])
    if top_dest:
        max_d = top_dest[0][1] if top_dest else 1
        lines_d = ["🎯 <b>Топ-10 направлений</b>\n"]
        for i, (name, cnt) in enumerate(top_dest[:10], 1):
            lines_d.append(f"  {i:>2}. {name:<18} {bar(cnt, max_d, 8)}  {cnt}")
        await _send("\n".join(lines_d))

    # ── Блок 3: Города вылета ────────────────────────────────────
    top_orig = an.get("top_origins", [])
    if top_orig:
        max_o = top_orig[0][1] if top_orig else 1
        lines_o = ["🛫 <b>Топ города вылета</b>\n"]
        for i, (name, cnt) in enumerate(top_orig[:5], 1):
            lines_o.append(f"  {i}. {name:<18} {bar(cnt, max_o, 8)}  {cnt}")
        await _send("\n".join(lines_o))

    # ── Блок 4: Поведение пользователей ─────────────────────────
    def _dec(v) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    trip  = {_dec(k): _dec(v) for k, v in an.get("trip_type", {}).items()}
    pax   = {_dec(k): _dec(v) for k, v in an.get("passengers", {}).items()}
    stops = {_dec(k): _dec(v) for k, v in an.get("transfers", {}).items()}

    b4 = "🧠 <b>Поведение</b>\n"
    if trip:
        ow = int(trip.get("oneway", 0))
        rt = int(trip.get("roundtrip", 0))
        total = ow + rt or 1
        b4 += f"  ✈️ Только туда:     <b>{ow}</b>  ({round(ow/total*100)}%)\n"
        b4 += f"  🔄 Туда-обратно:    <b>{rt}</b>  ({round(rt/total*100)}%)\n"
    if stops:
        direct = int(stops.get("direct", 0))
        one    = int(stops.get("1_stop", 0))
        two    = int(stops.get("2plus_stops", 0))
        b4 += f"  ➡️ Прямые рейсы:   <b>{direct}</b>\n"
        b4 += f"  🔁 С пересадкой:   <b>{one + two}</b>\n"
    if pax:
        sorted_pax = sorted(pax.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99)
        pax_str = "  ".join(f"{k} пас: <b>{v}</b>" for k, v in sorted_pax)
        b4 += f"  👥 {pax_str}\n"
    if b4.strip() != "🧠 <b>Поведение</b>":
        await _send(b4.strip())

    # ── Блок 5: Ценовые сегменты ─────────────────────────────────
    price_b = an.get("price_buckets", [])
    if price_b:
        def _sort_key(item):
            try: return int(item[0].split("-")[0])
            except: return 999999
        sorted_pb = sorted(price_b, key=_sort_key)
        max_p = max(c for _, c in sorted_pb) if sorted_pb else 1
        lines_p = ["💰 <b>Ценовые сегменты</b>\n"]
        for bucket, cnt in sorted_pb:
            parts = bucket.split("-")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                label = f"{int(parts[0])//1000}–{int(parts[1])//1000}к ₽"
            else:
                label = bucket
            lines_p.append(f"  {label:<12} {bar(cnt, max_p, 8)}  {cnt}")
        await _send("\n".join(lines_p))

    # ── Блок 6: График активности по дням ───────────────────────
    if day_data:
        max_day = max(day_data.values()) or 1
        lines_cal = ["📅 <b>Активность за 7 дней</b>\n"]
        for day, cnt in sorted(day_data.items())[-7:]:
            short = day[5:]  # MM-DD
            lines_cal.append(f"  {short}  {bar(cnt, max_day, 10)}  <b>{cnt}</b>")
        await _send("\n".join(lines_cal))

    # ── Блок 7: Проблемные маршруты ──────────────────────────────
    no_results = an.get("top_no_results", [])
    if no_results:
        lines_nr = ["🚫 <b>Маршруты без рейсов</b>\n"]
        for route, cnt in no_results[:5]:
            lines_nr.append(f"  {route}   —   {cnt} раз")
        await _send("\n".join(lines_nr))

    # ── Блок 8: Типы поиска ───────────────────────────────────────
    search_types = an.get("search_types", {})
    if search_types:
        total_st = sum(search_types.values()) or 1
        type_labels = {
            "normal":           "🔍 Обычный",
            "everywhere_dest":  "🌍 Город→Везде",
            "everywhere_origin":"🌍 Везде→Город",
            "country":          "🗺 По стране",
            "multi":            "✈️ Мультиcity",
            "quick":            "⚡ Быстрый",
        }
        lines_st = ["🔎 <b>Типы поиска</b>\n"]
        for k, cnt in sorted(search_types.items(), key=lambda x: -x[1]):
            label = type_labels.get(k, k)
            pct = round(cnt / total_st * 100)
            lines_st.append(f"  {label:<20} <b>{cnt}</b>  ({pct}%)")
        await _send("\n".join(lines_st))

    # ── Блок 9: Клики по ссылкам ─────────────────────────────────
    total_clicks = an.get("total_link_clicks", 0)
    clicks_by_ctx = an.get("link_clicks_by_context", {})
    if total_clicks:
        ctx_labels = {
            "search_results":          "📋 Результат поиска",
            "search_results_fallback": "📋 Все варианты",
            "everywhere":              "🌍 Везде",
            "multi_search":            "✈️ Мультиcity",
            "unknown":                 "❓ Без контекста",
        }
        lines_cl = [f"🔗 <b>Переходы по ссылкам</b>  —  всего: <b>{total_clicks}</b>\n"]
        for k, cnt in sorted(clicks_by_ctx.items(), key=lambda x: -x[1]):
            label = ctx_labels.get(k, k)
            lines_cl.append(f"  {label:<26} <b>{cnt}</b>")
        await _send("\n".join(lines_cl))

    # ── Блок 10: Воронка поиска ───────────────────────────────────
    funnel = an.get("funnel", {})
    if funnel:
        funnel_steps = [
            ("1_route",      "1. Ввёл маршрут"),
            ("2_date",       "2. Выбрал дату"),
            ("4_flight_type","3. Тип рейса"),
            ("5_passengers", "4. Пассажиры"),
            ("6_confirm",    "5. Подтверждение"),
            ("5_result_shown","6. Увидел результат"),
        ]
        max_f = max(funnel.values()) or 1
        lines_f = ["🎯 <b>Воронка поиска</b>\n"]
        prev = None
        for key, label in funnel_steps:
            cnt = funnel.get(key, 0)
            drop = ""
            if prev and prev > 0 and cnt < prev:
                drop_pct = round((prev - cnt) / prev * 100)
                drop = f"  ↓{drop_pct}% отсев"
            lines_f.append(f"  {label:<26} <b>{cnt}</b>{drop}")
            prev = cnt
        await _send("\n".join(lines_f))

    # ── Блок 11: Типы подписок ────────────────────────────────────
    sub_types = an.get("sub_types", {})
    total_subs_created = an.get("total_subs_created", 0)
    if sub_types or total_subs_created:
        sub_labels = {
            "hot_deals":   "🔥 Горячие предложения",
            "digest":      "📰 Дайджест",
            "price_watch": "📉 Слежение за ценой",
        }
        lines_sub = [f"📬 <b>Подписки</b>  —  создано всего: <b>{total_subs_created}</b>\n"]
        for k, cnt in sorted(sub_types.items(), key=lambda x: -x[1]):
            label = sub_labels.get(k, k)
            lines_sub.append(f"  {label:<28} <b>{cnt}</b> активных")
        await _send("\n".join(lines_sub))

    # ── Итог ─────────────────────────────────────────────────────
    await _send("✅ <b>Отчёт завершён</b>")
    return True


# ── Вспомогательное ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Logging Handler для автоматической отправки ERROR-логов ──────────────────

class ChannelLogHandler(logging.Handler):
    """
    Добавь в main.py:
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
        # CRITICAL всегда отправляем немедленно — дебаунс не применяем
        if record.levelno < logging.CRITICAL:
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