# handlers/start.py
"""
Точка входа: /start, главное меню, кнопки нижней панели,
продолжение поиска, справка, подписки.
Вся бизнес-логика поиска — в flight_wizard и search_results.
"""
import asyncio
import os

from aiogram import Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup

from utils.redis_client import redis_client
from utils.smart_reminder import schedule_inactivity, cancel_inactivity, mark_fsm_inactive
from handlers.quick_search import handle_flight_request
from utils.channel_logger import log_feedback, log_event, log_error
from handlers.flight_constants import CANCEL_KB, NAV_KB
from handlers.flight_fsm import FlightSearch, _get_metro, _airport_keyboard, _genitive
from handlers.flight_wizard import ask_flight_type, ask_adults, show_summary

router = Router()
_SEARCH_SEMAPHORE = asyncio.Semaphore(10)


class FeedbackState(StatesGroup):
    waiting = State()

MAIN_MENU_TEXT = (
    "Привет! Я помогу тебе летать выгодно.\n\n"
    "✈️ <b>Поиск</b> — простой маршрут туда и обратно.\n"
    "🗺 <b>Маршрут</b> — составной поиск по нескольким городам.\n"
    "🔥 <b>Горячие</b> — уведомления о супер-ценах.\n\n"
    "Жми кнопки внизу, чтобы начать"
)

# ════════════════════════════════════════════════════════════════
# /start и главное меню
# ════════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(MAIN_MENU_TEXT, parse_mode="HTML", reply_markup=NAV_KB)
    # Аналитика: новый пользователь
    u = message.from_user
    asyncio.create_task(log_event(
        "new_user", user_id=u.id, username=u.username,
        detail=f"Имя: {(u.first_name or '') + ' ' + (u.last_name or '')}".strip()
    ))


@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)
    if state:
        await state.clear()
    try:
        await callback.message.edit_text("Выберите раздел в нижней панели навигации.")
    except Exception:
        await callback.message.answer("Выберите раздел в нижней панели навигации.")
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Кнопки нижней панели
# ════════════════════════════════════════════════════════════════

@router.message(F.text == "✈️ Поиск")
async def nav_search(message: Message, state: FSMContext):
    current = await state.get_state()
    if current and not current.startswith("FlyStackTrack"):
        await state.clear()
    cancel_inactivity(message.chat.id)
    await message.answer(
        "✈️ <b>Шаг 1/6</b> — Маршрут\n\n"
        "<b>Напишите маршрут:</b> Город отправления — Город или страну прибытия\n\n"
        "<i>Примеры:\n"
        "• Москва — Сочи\n"
        "• Москва — Таиланд\n"
        "• Казань — Египет</i>\n\n"
        "Если ещё не решили откуда или куда — напишите «Везде».",
        parse_mode="HTML",
    )
    await state.set_state(FlightSearch.route)
    schedule_inactivity(message.chat.id, message.from_user.id)


@router.message(F.text == "🗺 Маршрут")
async def nav_multi_search(message: Message, state: FSMContext):
    current = await state.get_state()
    if current and not current.startswith("FlyStackTrack"):
        await state.clear()
    cancel_inactivity(message.chat.id)
    from handlers.multi_search import start_multi_search
    await start_multi_search(message, state)


@router.callback_query(F.data == "search_simple")
async def handle_search_simple(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    await callback.message.edit_text(
        "✈️ <b>Шаг 1/6</b> — Маршрут\n\n"
        "<b>Напишите маршрут:</b> Город отправления — Город или страну прибытия\n\n"
        "<i>Примеры:\n"
        "• Москва — Сочи\n"
        "• Москва — Таиланд\n"
        "• Казань — Египет</i>\n\n"
        "Если ещё не решили откуда или куда — напишите «Везде».",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.route)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data == "search_multi")
async def handle_search_multi(callback: CallbackQuery, state: FSMContext):
    from handlers.multi_search import start_multi_search
    cancel_inactivity(callback.message.chat.id)
    await callback.answer()
    await start_multi_search(callback.message, state)


@router.message(F.text == "🔥 Горячие")
async def nav_hot(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    await state.clear()
    user_id = message.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    text = (
        "🔥 <b>Горячие предложения</b>\n\n"
        "Укажите направления и бюджет — бот сам следит за ценами "
        "и пришлёт уведомление, когда появятся выгодные билеты."
    )
    buttons = [[InlineKeyboardButton(text="⚙️ Настроить подписку", callback_data="hd_new_sub")]]
    if subs:
        buttons.append([InlineKeyboardButton(
            text=f"📋 Мои подписки ({len(subs)})", callback_data="hd_my_subs"
        )])
    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(F.text == "📋 Подписки")
async def nav_subs(message: Message, state: FSMContext):
    await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    user_id = message.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    if not subs:
        await message.answer(
            "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.\n"
            "Хотите настроить уведомления о горячих ценах?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔥 Создать подписку", callback_data="hd_new_sub")]
            ]),
        )
    else:
        from handlers.hot_deals import hd_my_subs_text_kb
        text, kb = await hd_my_subs_text_kb(user_id, subs)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "❓ Помощь")
async def nav_help(message: Message, state: FSMContext):
    await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await message.answer(
        "❓ <b>Как пользоваться</b>\n\n"
        "✈️ <b>Поиск</b> — простой маршрут туда и/или обратно.\n\n"
        "🗺 <b>Маршрут</b> — составной поиск: несколько перелётов по разным городам.\n\n"
        "🔥 <b>Горячие</b> — подпишитесь на направления, "
        "и я напишу когда цена упадёт на 10%+.\n\n"
        "📋 <b>Подписки</b> — просмотр и управление активными подписками.\n\n"
        "💬 <b>Обратная связь</b> — сообщить о баге или предложить улучшение.\n\n"
        "<i>Нажмите «✈️ Поиск» чтобы начать.</i>",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════════════
# Справка и подписки (callback)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "help_info")
async def handle_help(callback: CallbackQuery):
    text = (
        "❓ <b>Как пользоваться</b>\n\n"
        "✈️ <b>Поиск билетов</b> — маршрут, даты, пассажиры → лучшие цены.\n\n"
        "🔥 <b>Горячие предложения</b> — подпишитесь, и я напишу когда цена упадёт на 10%+.\n\n"
        "📋 <b>Подписки</b> — просмотр и управление.\n\n"
        "📉 <b>Следить за ценой</b> — уведомление при изменении цены на маршруте."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
    ])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "my_subscriptions")
async def handle_my_subscriptions(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    subs = await redis_client.get_hot_subs(user_id)
    if not subs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Создать подписку", callback_data="hd_new_sub")],
        ])
        try:
            await callback.message.edit_text(
                "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.\n"
                "Хотите настроить уведомления о горячих ценах?",
                parse_mode="HTML", reply_markup=kb,
            )
        except Exception:
            await callback.message.answer(
                "📋 <b>Мои подписки</b>\n\nАктивных подписок нет.",
                parse_mode="HTML", reply_markup=kb,
            )
        await callback.answer()
        return
    from handlers.hot_deals import hd_my_subs
    await hd_my_subs(callback, state)


# ════════════════════════════════════════════════════════════════
# Продолжение поиска (умное возобновление FSM)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "continue_search")
async def handle_continue_search(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    current = await state.get_state()
    data    = await state.get_data()

    if not current or not current.startswith("FlightSearch"):
        hint = (
            "Начнём поиск 👌\n\n"
            "<b>Напишите маршрут:</b> Город отправления - Город прибытия\n\n"
            "<i>Примеры: Москва - Сочи, Москва - Таиланд, Казань - Египет</i>\n\n"
            "💡 Не знаете точный город? Напишите страну — предложу варианты."
        )
        try:
            await callback.message.edit_text(hint, parse_mode="HTML", reply_markup=CANCEL_KB)
        except Exception:
            await callback.message.answer(hint, parse_mode="HTML", reply_markup=CANCEL_KB)
        await state.set_state(FlightSearch.route)
        schedule_inactivity(callback.message.chat.id, callback.from_user.id)
        await callback.answer()
        return

    await callback.answer("▶️ Продолжаем!")

    if current == FlightSearch.route.state:
        origin = data.get("origin_name", "")
        hint = f"\n<i>Последний ввод: {origin}</i>" if origin else ""
        await callback.message.answer(
            f"<b>Маршрут:</b> Город отправления - Город прибытия{hint}\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
    elif current == FlightSearch.choose_airport.state:
        orig_iata   = data.get("origin_iata", "")
        origin_name = data.get("origin_name", "")
        metro = _get_metro(orig_iata) if orig_iata else None
        if metro:
            await callback.message.answer(
                f"Вы выбрали: <b>{origin_name}</b>\n\n"
                f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
                parse_mode="HTML", reply_markup=_airport_keyboard(metro, origin_name),
            )
        else:
            await callback.message.answer("Выберите аэропорт:", reply_markup=CANCEL_KB)
    elif current == FlightSearch.depart_date.state:
        existing = data.get("depart_date", "")
        hint = f"\n<i>Последний ввод: {existing}</i>" if existing else ""
        await callback.message.answer(
            f"Введите дату вылета в формате <code>ДД.ММ</code>{hint}\n<i>Пример: 10.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
    elif current == FlightSearch.need_return.state:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, нужен",    callback_data="return_yes"),
            InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="return_no"),
        ]])
        await callback.message.answer("Нужен ли обратный билет?", reply_markup=kb)
    elif current == FlightSearch.return_date.state:
        await callback.message.answer(
            "Введите дату возврата в формате <code>ДД.ММ</code>\n<i>Пример: 20.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
    elif current == FlightSearch.flight_type.state:
        await ask_flight_type(callback.message, state)
    elif current in (FlightSearch.adults.state, FlightSearch.has_children.state,
                     FlightSearch.children.state, FlightSearch.infants.state):
        await ask_adults(callback.message, state)
    elif current == FlightSearch.confirm.state:
        await show_summary(callback.message, state)
    else:
        await callback.message.answer("Используйте кнопки внизу экрана.")
        return

    schedule_inactivity(callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    await callback.message.edit_text(
        "Начнём поиск билетов 👌\n\n"
        "<b>Напишите маршрут в формате: Город отправления - Город прибытия</b>\n\n"
        "<i>Примеры: Москва - Сочи, Москва - Таиланд, Казань - Египет</i>\n\n"
        "💡 Не знаете точный город? Напишите страну — предложу популярные варианты.\n\n"
        "Если ещё не решили, откуда или куда полетите, напишите слово «Везде».",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.route)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()



# ════════════════════════════════════════════════════════════════
# Обратная связь
# ════════════════════════════════════════════════════════════════

@router.message(F.text == "💬 Обратная связь")
async def nav_feedback(message: Message, state: FSMContext):
    """Нажатие кнопки «Обратная связь» на нав-панели."""
    await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.set_state(FeedbackState.waiting)
    await message.answer(
        "💬 <b>Обратная связь</b>\n\n"
        "Напишите ваше сообщение — баг, пожелание или вопрос.\n"
        "Оно придёт напрямую команде разработки.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖ Отмена", callback_data="cancel_feedback")]
        ])
    )


@router.callback_query(F.data == "cancel_feedback")
async def cancel_feedback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.message(FeedbackState.waiting)
async def process_feedback(message: Message, state: FSMContext):
    await state.clear()
    user      = message.from_user
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    # Сохраняем в redis для истории
    feedback_key = f"feedback:{user.id}:{int(message.date.timestamp())}"
    try:
        await redis_client.client.set(feedback_key, message.text, ex=60*60*24*90)
    except Exception:
        pass

    # Отправляем в канал аналитики
    asyncio.create_task(log_feedback(
        user_id=user.id, username=user.username,
        full_name=full_name, text=message.text,
    ))

    await message.answer(
        "✅ Спасибо! Ваше сообщение передано команде.\n\n"
        "Постараемся ответить или исправить как можно быстрее 🔧",
    )

# ════════════════════════════════════════════════════════════════
# Секретные команды аналитики (только для ADMIN_USER_ID)
# ════════════════════════════════════════════════════════════════

def _is_admin(user_id: int) -> bool:
    admin_id = os.getenv("ADMIN_USER_ID", "")
    return bool(admin_id) and str(user_id) == admin_id


def _dec(v) -> str:
    """Декодирует bytes из Redis в строку."""
    return v.decode() if isinstance(v, bytes) else str(v)


def _bar(count: int, max_count: int, width: int = 8) -> str:
    """Мини-бар из символов для визуализации."""
    if not max_count:
        return ""
    filled = round(width * count / max_count)
    return "█" * filled + "░" * (width - filled)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Полная аналитика — отправляет в канал и показывает в чат."""
    if not _is_admin(message.from_user.id):
        return
    await message.answer("\u23f3 \u0421\u043e\u0431\u0438\u0440\u0430\u044e \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0443...")
    try:
        an = await redis_client.get_analytics()
        msgs = _build_stats_messages(an)
        from utils.channel_logger import log_stats
        # Отправляем каждый блок в канал отдельным постом
        for block_title, block_text in msgs:
            flat = {block_title: block_text}
            await log_stats(flat)
        # Краткий ответ в чат
        summary = (
            "\U0001f4ca <b>\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430 \u0432 \u043a\u0430\u043d\u0430\u043b</b>\n\n"
            f"\U0001f465 \u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: <b>{an.get('total_users', 0)}</b>\n"
            f"\U0001f50d \u041f\u043e\u0438\u0441\u043a\u043e\u0432: <b>{an.get('total_searches', 0)}</b>\n"
            f"\U0001f514 \u041f\u043e\u0434\u043f\u0438\u0441\u043e\u043a: <b>{an.get('active_subscriptions', 0)}</b>"
        )
        await message.answer(summary, parse_mode="HTML")
    except Exception as exc:
        from utils.channel_logger import log_error
        await log_error("/stats", exc)
        await message.answer(f"\u274c {exc}")


def _build_stats_messages(an: dict) -> list[tuple[str, str]]:
    """Строит список (заголовок, текст) блоков статистики."""
    blocks = []

    # ── Блок 1: Общая сводка ────────────────────────────────────
    searches = an.get("total_searches", 0)
    no_res   = an.get("total_no_results", 0)
    sr_rate  = f"{round((searches - no_res) / searches * 100)}%" if searches else "—"
    day_data = an.get("searches_by_day", {})
    avg_day  = round(sum(day_data.values()) / len(day_data)) if day_data else 0
    b1 = (
        f"\U0001f465 \u0412\u0441\u0435\u0433\u043e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0435\u0439: <b>{an.get('total_users', 0)}</b>\n"
        f"\U0001f50d \u041f\u043e\u0438\u0441\u043a\u043e\u0432 \u0432\u0441\u0435\u0433\u043e: <b>{searches}</b>\n"
        f"\u2705 \u041d\u0430\u0448\u043b\u0438 \u0440\u0435\u0439\u0441\u044b: <b>{sr_rate}</b>\n"
        f"\U0001f4c5 \u0421\u0440\u0435\u0434\u043d\u0435 \u0432 \u0434\u0435\u043d\u044c: <b>{avg_day}</b>\n"
        f"\U0001f514 \u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a: <b>{an.get('active_subscriptions', 0)}</b>\n"
        f"\U0001f4c9 \u041e\u0442\u0441\u043b\u0435\u0436\u0438\u0432\u0430\u043d\u0438\u0439 \u0446\u0435\u043d: <b>{an.get('price_watches', 0)}</b>"
    )
    blocks.append(("\U0001f4ca \u041e\u0431\u0449\u0430\u044f \u0441\u0432\u043e\u0434\u043a\u0430", b1))

    # ── Блок 2: Топ направления ──────────────────────────────────
    top_dest = an.get("top_destinations", [])
    if top_dest:
        max_d = top_dest[0][1] if top_dest else 1
        lines = []
        for i, (name, cnt) in enumerate(top_dest[:10], 1):
            lines.append(f"{i}. {name}  {_bar(cnt, max_d)}  <b>{cnt}</b>")
        blocks.append(("\U0001f3af \u0422\u043e\u043f-10 \u043d\u0430\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0439", "\n".join(lines)))

    # ── Блок 3: Топ города вылета ────────────────────────────────
    top_orig = an.get("top_origins", [])
    if top_orig:
        max_o = top_orig[0][1] if top_orig else 1
        lines = []
        for i, (name, cnt) in enumerate(top_orig[:5], 1):
            lines.append(f"{i}. {name}  {_bar(cnt, max_o)}  <b>{cnt}</b>")
        blocks.append(("\U0001f6eb \u0413\u043e\u0440\u043e\u0434\u0430 \u0432\u044b\u043b\u0435\u0442\u0430", "\n".join(lines)))

    # ── Блок 4: Ценовые сегменты ─────────────────────────────────
    price_b = an.get("price_buckets", [])
    if price_b:
        # Сортируем по нижней границе диапазона
        def _sort_key(item):
            try:
                return int(item[0].split("-")[0])
            except Exception:
                return 999999
        sorted_pb = sorted(price_b, key=_sort_key)
        max_p = max(c for _, c in sorted_pb) if sorted_pb else 1
        lines = []
        for bucket, cnt in sorted_pb:
            lo, hi = bucket.split("-") if "-" in bucket else (bucket, "")
            label = f"{int(lo)//1000}–{int(hi)//1000}к ₽" if lo.isdigit() and hi.isdigit() else bucket
            lines.append(f"{label}  {_bar(cnt, max_p)}  <b>{cnt}</b>")
        blocks.append(("\U0001f4b0 \u0426\u0435\u043d\u043e\u0432\u044b\u0435 \u0441\u0435\u0433\u043c\u0435\u043d\u0442\u044b", "\n".join(lines)))

    # ── Блок 5: Поведение пользователей ─────────────────────────
    def _hmap(d: dict) -> dict:
        return {_dec(k): _dec(v) for k, v in d.items()}

    trip  = _hmap(an.get("trip_type", {}))
    pax   = _hmap(an.get("passengers", {}))
    stops = _hmap(an.get("transfers", {}))
    ftype = _hmap(an.get("flight_types", {}))

    total_tt = sum(int(v) for v in trip.values()) or 1
    total_px = sum(int(v) for v in pax.values()) or 1
    total_st = sum(int(v) for v in stops.values()) or 1

    b5 = ""
    if trip:
        ow = int(trip.get("oneway", 0))
        rt = int(trip.get("roundtrip", 0))
        b5 += f"\u2708\ufe0f \u0422\u043e\u043b\u044c\u043a\u043e \u0442\u0443\u0434\u0430: <b>{ow}</b> ({round(ow/total_tt*100)}%)  |  \U0001f501 \u0422\u0443\u0434\u0430-\u043e\u0431\u0440\u0430\u0442\u043d\u043e: <b>{rt}</b> ({round(rt/total_tt*100)}%)\n"
    if stops:
        direct = int(stops.get("direct", 0))
        one    = int(stops.get("1_stop", 0))
        two    = int(stops.get("2plus_stops", 0))
        b5 += f"\u2192 \u041f\u0440\u044f\u043c\u044b\u0435: <b>{direct}</b>  |  1 \u043f\u0435\u0440\u0435\u0441: <b>{one}</b>  |  2+: <b>{two}</b>\n"
    if pax:
        sorted_pax = sorted(pax.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99)
        pax_str = "  ".join(f"{k} \u043f\u0430\u0441: <b>{v}</b>" for k, v in sorted_pax)
        b5 += f"\U0001f465 {pax_str}\n"
    if ftype:
        ft_str = "  ".join(f"{k}: <b>{v}</b>" for k, v in ftype.items())
        b5 += f"\U0001f6e9 {ft_str}"

    if b5:
        blocks.append(("\U0001f9e0 \u041f\u043e\u0432\u0435\u0434\u0435\u043d\u0438\u0435", b5.strip()))

    # ── Блок 6: Активность по дням ───────────────────────────────
    if day_data:
        max_day = max(day_data.values()) or 1
        lines = []
        for day, cnt in sorted(day_data.items()):
            short = day[5:]  # MM-DD
            lines.append(f"{short}  {_bar(cnt, max_day)}  <b>{cnt}</b>")
        blocks.append(("\U0001f4c6 \u0410\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c (\u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 7 \u0434\u043d\u0435\u0439)", "\n".join(lines)))

    # ── Блок 7: Маршруты без результатов ────────────────────────
    no_results = an.get("top_no_results", [])
    if no_results:
        lines = [f"{r}: {c}" for r, c in no_results]
        blocks.append(("\U0001f6ab \u041c\u0430\u0440\u0448\u0440\u0443\u0442\u044b \u0431\u0435\u0437 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u043e\u0432", "\n".join(lines)))

    return blocks


@router.message(Command("feedback_log"))
async def cmd_feedback_log(message: Message):
    """Показывает последние 5 отзывов из Redis."""
    if not _is_admin(message.from_user.id):
        return
    try:
        keys = await redis_client.client.keys("*feedback:*")
        if not keys:
            await message.answer("\u041d\u0435\u0442 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d\u043d\u044b\u0445 \u043e\u0442\u0437\u044b\u0432\u043e\u0432.")
            return
        keys = sorted(keys, reverse=True)[:5]
        lines = ["\U0001f4ac <b>\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 \u043e\u0442\u0437\u044b\u0432\u044b:</b>\n"]
        for key in keys:
            val = await redis_client.client.get(key)
            if val:
                k_str = key.decode() if isinstance(key, bytes) else key
                v_str = val.decode() if isinstance(val, bytes) else val
                lines.append(f"<code>{k_str}</code>\n{v_str}\n")
        await message.answer("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"\u274c {exc}")


# ════════════════════════════════════════════════════════════════
# Fallback: текст вне FSM → быстрый поиск
# ════════════════════════════════════════════════════════════════

# Тексты кнопок навигационной панели — обрабатываются выше своими хендлерами
_NAV_BUTTON_TEXTS = {
    "✈️ Поиск", "🗺 Маршрут", "🔥 Горячие",
    "📋 Подписки", "❓ Помощь", "💬 Обратная связь",
}

@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    text = message.text or ""
    # Команды не трогаем — у них свои хендлеры
    if text.startswith("/"):
        return
    # Кнопки навигации не трогаем — у них свои хендлеры выше
    if text in _NAV_BUTTON_TEXTS:
        return
    current = await state.get_state()
    if current:
        return
    # handle_flight_request принимает только message (без state)
    await handle_flight_request(message)