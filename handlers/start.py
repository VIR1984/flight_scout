# handlers/start.py
"""
Точка входа: /start, главное меню, кнопки нижней панели,
продолжение поиска, справка, подписки.
Вся бизнес-логика поиска — в flight_wizard и search_results.
"""
import asyncio

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
from handlers.flight_constants import CANCEL_KB, NAV_KB
from handlers.flight_fsm import FlightSearch, _get_metro, _airport_keyboard, _genitive
from handlers.flight_wizard import ask_flight_type, ask_adults, show_summary

router = Router()
_SEARCH_SEMAPHORE = asyncio.Semaphore(10)

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
    cancel_inactivity(message.chat.id)
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

FEEDBACK_CHAT_ID = None  # Замените на ваш chat_id или @username канала

@router.message(F.text == "💬 Обратная связь")
async def nav_feedback(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    current = await state.get_state()
    if current:
        await state.clear()
    await state.set_state(FeedbackState.waiting)
    await message.answer(
        "💬 <b>Обратная связь</b>\n\n"
        "Нашли баг или есть идея как улучшить бота?\n"
        "Напишите сюда — я передам команде 👇",
        parse_mode="HTML",
    )


class FeedbackState(StatesGroup):
    waiting = State()


@router.message(FeedbackState.waiting)
async def process_feedback(message: Message, state: FSMContext, bot):
    await state.clear()
    user = message.from_user
    user_info = f"@{user.username}" if user.username else f"id:{user.id}"
    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    # Сохраняем в redis для истории
    feedback_key = f"feedback:{user.id}:{int(message.date.timestamp())}"
    try:
        await redis_client.client.set(feedback_key, message.text, ex=60*60*24*90)
    except Exception:
        pass

    # Пересылаем владельцу бота если задан FEEDBACK_CHAT_ID
    if FEEDBACK_CHAT_ID:
        try:
            await bot.send_message(
                FEEDBACK_CHAT_ID,
                f"💬 <b>Обратная связь</b>\n"
                f"От: {full_name} ({user_info})\n"
                f"ID: <code>{user.id}</code>\n\n"
                f"{message.text}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    await message.answer(
        "✅ Спасибо! Ваше сообщение передано команде.\n\n"
        "Если нашли баг — постараемся исправить быстро 🔧",
    )

# ════════════════════════════════════════════════════════════════
# Fallback: текст вне FSM → быстрый поиск
# ════════════════════════════════════════════════════════════════

@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    # Команды не трогаем — у них свои хендлеры
    if message.text and message.text.startswith("/"):
        return
    current = await state.get_state()
    if current:
        return
    await handle_flight_request(message, state)