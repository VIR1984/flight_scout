# handlers/flight_wizard.py
"""
FSM-шаги пошагового поиска: маршрут → аэропорт → даты →
тип рейса → пассажиры → сводка → редактирование.
"""
import asyncio

from aiogram import Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.context import FSMContext

from utils.cities_loader import (
    get_iata, get_city_name, fuzzy_get_iata,
    CITY_TO_IATA, IATA_TO_CITY, _normalize_name, get_country_cities,
)
from utils.smart_reminder import schedule_inactivity, cancel_inactivity, mark_fsm_inactive
from utils.logger import logger
from handlers.flight_constants import CANCEL_KB, MULTI_AIRPORT_CITIES, AIRPORT_NAMES
from handlers.everywhere_search import format_user_date, build_passenger_desc
from handlers.flight_fsm import (
    FlightSearch, validate_route, validate_date,
    _get_metro, _has_multi_airports, _airport_keyboard,
    build_choices_summary, build_passenger_code, _flight_type_text_to_code,
    _genitive,
)
from handlers.country_search import _ask_country_city, _finalize_route
from services.flight_search import normalize_date
from utils.date_hints import hint_depart, hint_return

router = Router()

# ════════════════════════════════════════════════════════════════
# Перехват кнопок нижней панели во всех состояниях FSM
# ════════════════════════════════════════════════════════════════

NAV_TEXTS = {"✈️ Поиск", "🗺 Маршрут", "🔥 Горячие", "💬 Обратная связь", "📋 Подписки", "❓ Помощь"}

@router.message(FlightSearch.route, F.text.in_(NAV_TEXTS))
@router.message(FlightSearch.depart_date, F.text.in_(NAV_TEXTS))
@router.message(FlightSearch.return_date, F.text.in_(NAV_TEXTS))
@router.message(FlightSearch.confirm, F.text.in_(NAV_TEXTS))
async def fsm_nav_button(message: Message, state: FSMContext):
    """Если нажата кнопка навигации во время FSM — сбрасываем и перенаправляем."""
    await state.clear()
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    # Перенаправляем на нужный обработчик из start.py
    if message.text == "🔥 Горячие":
        from handlers.start import nav_hot
        await nav_hot(message, state)
    elif message.text == "🗺 Маршрут":
        from handlers.start import nav_multi_search
        await nav_multi_search(message, state)
    elif message.text == "💬 Обратная связь":
        from handlers.start import nav_feedback
        await nav_feedback(message, state)
    elif message.text == "📋 Подписки":
        from handlers.start import nav_subs
        await nav_subs(message, state)
    elif message.text == "❓ Помощь":
        from handlers.start import nav_help
        await nav_help(message, state)
    else:
        from handlers.start import nav_search
        await nav_search(message, state)



@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    data_pre = await state.get_data()  # нужен для сохранения dest перед _ask_country_city
    origin, dest = validate_route(message.text)
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    if origin != "везде":
        orig_iata = get_iata(origin) or CITY_TO_IATA.get(_normalize_name(origin))
        if not orig_iata:
            # Проверяем — не страна ли это?
            country_cities = get_country_cities(origin)
            if country_cities:
                # Сохраняем dest (в т.ч. "везде") в state ДО выхода,
                # иначе после выбора города страны dest будет потерян
                await state.update_data(
                    dest=dest,
                    dest_iata=None if dest == "везде" else data_pre.get("dest_iata"),
                    dest_name="Везде" if dest == "везде" else dest,
                )
                await _ask_country_city(message, state, origin, country_cities, role="origin")
                return
            # П.2: пробуем нечёткий поиск
            fuzzy_iata, fuzzy_name = fuzzy_get_iata(origin)
            if fuzzy_iata:
                await message.answer(
                    f"❓ Не нашёл «{origin}» — может быть, вы имели в виду <b>{fuzzy_name}</b>?\n"
                    f"Напишите маршрут ещё раз с правильным названием.\n\n"
                    f"<i>Пример: {fuzzy_name} - Сочи</i>",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            else:
                await message.answer(
                    f"❌ Не знаю город отправления: <b>{origin}</b>\n"
                    f"Проверьте написание и попробуйте ещё раз.",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            return
        origin_name = get_city_name(orig_iata) or IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = origin_name = None

    if dest != "везде":
        dest_iata = get_iata(dest) or CITY_TO_IATA.get(_normalize_name(dest))
        if not dest_iata:
            # Проверяем — не страна ли это?
            country_cities = get_country_cities(dest)
            if country_cities:
                # Сохраняем уже распознанный origin, чтобы не потерять его
                await state.update_data(
                    origin=origin,
                    origin_iata=orig_iata,
                    origin_name="Везде" if origin == "везде" else origin_name,
                )
                await _ask_country_city(message, state, dest, country_cities, role="dest")
                return
            # нечёткий поиск
            fuzzy_iata, fuzzy_name = fuzzy_get_iata(dest)
            if fuzzy_iata:
                await message.answer(
                    f"❓ Не нашёл «{dest}» — может быть, вы имели в виду <b>{fuzzy_name}</b>?\n"
                    f"Напишите маршрут ещё раз с правильным названием.\n\n"
                    f"<i>Пример: Москва - {fuzzy_name}</i>",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            else:
                await message.answer(
                    f"❌ Не знаю город прибытия: <b>{dest}</b>\n"
                    f"Проверьте написание и попробуйте ещё раз.",
                    parse_mode="HTML", reply_markup=CANCEL_KB,
                )
            return
        dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest.capitalize())
    else:
        dest_iata = dest_name = None

    if origin == "везде" and dest == "везде":
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\nУкажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB,
        )
        return

    if orig_iata and dest_iata and orig_iata == dest_iata:
        await message.answer(
            "❌ Город вылета и прибытия не могут совпадать.\nПожалуйста, выберите разные города.",
            reply_markup=CANCEL_KB,
        )
        return

    if origin == "везде": origin_name = "Везде"
    if dest   == "везде": dest_name   = "Везде"

    await state.update_data(
        origin=origin, origin_iata=orig_iata,
        dest=dest,     dest_iata=dest_iata,
        origin_name=origin_name, dest_name=dest_name,
    )

    data = await state.get_data()
    cancel_inactivity(message.chat.id)

    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        if orig_iata and _has_multi_airports(orig_iata):
            metro = _get_metro(orig_iata)
            await state.update_data(_edit_mode=True, origin_airports=None, origin_airport_label=None)
            await message.answer(
                f"Вы выбрали: <b>{origin_name}</b>\n\n"
                f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
                parse_mode="HTML",
                reply_markup=_airport_keyboard(metro, origin_name),
            )
            await state.set_state(FlightSearch.choose_airport)
            return
        await show_summary(message, state)
        return

    # Мульти-аэропорт
    if orig_iata and _has_multi_airports(orig_iata):
        metro = _get_metro(orig_iata)
        await state.update_data(origin_airports=None, origin_airport_label=None)
        await message.answer(
            f"Вы выбрали: <b>{origin_name}</b>\n\n"
            f"Из {_genitive(origin_name)} летают из нескольких аэропортов — выберите нужный:",
            parse_mode="HTML",
            reply_markup=_airport_keyboard(metro, origin_name),
        )
        await state.set_state(FlightSearch.choose_airport)
        schedule_inactivity(message.chat.id, message.from_user.id)
        return

    await message.answer(
        f"✈️ <b>Шаг 2/6</b> — Дата вылета\n\n"
        f"Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: {hint_depart()}</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.depart_date)
    schedule_inactivity(message.chat.id, message.from_user.id)


# ════════════════════════════════════════════════════════════════
# FSM: выбор аэропорта
# ════════════════════════════════════════════════════════════════

@router.callback_query(FlightSearch.choose_airport, F.data.startswith("ap_pick_"))
async def process_airport_pick(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    ap_iata = callback.data.replace("ap_pick_", "")
    metro   = _get_metro(ap_iata) or ap_iata
    ap_label = next(
        (lbl for code, lbl in MULTI_AIRPORT_CITIES.get(metro, []) if code == ap_iata),
        ap_iata,
    )
    await state.update_data(origin_iata=ap_iata, origin_airports=[ap_iata], origin_airport_label=ap_label)
    # Редактируем сообщение с кнопками — убираем список, показываем выбор
    await callback.message.edit_text(f"✈️ Выбран аэропорт: <b>{ap_label}</b>", parse_mode="HTML")
    await callback.answer()
    await _after_airport_pick(callback, state)


@router.callback_query(FlightSearch.choose_airport, F.data.startswith("ap_any_"))
async def process_airport_any(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    metro_iata = callback.data.replace("ap_any_", "")
    all_iatas  = [ap for ap, _ in MULTI_AIRPORT_CITIES.get(metro_iata, [])]
    data = await state.get_data()
    city_name = data.get("origin_name", "")
    await state.update_data(
        origin_iata=metro_iata,
        origin_airports=all_iatas,
        origin_airport_label="Любой аэропорт",
    )
    # Редактируем сообщение с кнопками — убираем список, показываем выбор
    city_str = f" {city_name}" if city_name else ""
    await callback.message.edit_text(f"🔀 Любой аэропорт{city_str}", parse_mode="HTML")
    await callback.answer()
    await _after_airport_pick(callback, state)


async def _after_airport_pick(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(callback.message, state)
        return
    # Отправляем новое сообщение с вопросом о дате (не редактируем — выбор остаётся виден)
    await callback.message.answer(
        f"✈️ <b>Шаг 2/6</b> — Дата вылета\n\n"
        f"Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: {hint_depart()}</i>",
        parse_mode="HTML", reply_markup=CANCEL_KB,
    )
    await state.set_state(FlightSearch.depart_date)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)


# ════════════════════════════════════════════════════════════════
# FSM: дата вылета
# ════════════════════════════════════════════════════════════════

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            f"❌ Неверный формат даты.\n<i>Пример: {hint_depart()}</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    cancel_inactivity(message.chat.id)
    await state.update_data(depart_date=message.text)
    data = await state.get_data()
    is_everywhere = data["origin"] == "везде" or data["dest"] == "везде"

    if data.get("_edit_mode"):
        if is_everywhere:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
            return
        if data.get("need_return"):
            await message.answer(
                f"✏️ Введите новую дату обратного рейса в формате <code>ДД.ММ</code>\n<i>Пример: {hint_return(hint_depart())}</i>",
                parse_mode="HTML", reply_markup=CANCEL_KB,
            )
            await state.set_state(FlightSearch.return_date)
        else:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
        return

    if is_everywhere:
        await state.update_data(need_return=False, return_date=None)
        await ask_flight_type(message, state)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен",    callback_data="return_yes"),
         InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="return_no")],
    ])
    await message.answer("✈️ <b>Шаг 3/6</b> — Обратный билет\n\nНужен ли обратный билет?",
                         parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.need_return)


# ════════════════════════════════════════════════════════════════
# FSM: обратный билет
# ════════════════════════════════════════════════════════════════

@router.callback_query(FlightSearch.need_return, F.data.in_({"return_yes", "return_no"}))
async def process_need_return(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    need_return = callback.data == "return_yes"
    await state.update_data(need_return=need_return)
    if need_return:
        await callback.message.edit_text(
            f"✈️ <b>Шаг 3/6</b> — Дата возврата\n\n"
            f"Введите дату возврата в формате <code>ДД.ММ</code>\n<i>Пример: {hint_return(hint_depart())}</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await state.update_data(return_date=None)
        await ask_flight_type(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.need_return, F.text == "В начало")
async def need_return_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# FSM: дата возврата
# ════════════════════════════════════════════════════════════════

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            f"❌ Неверный формат даты.\n<i>Пример: {hint_return(hint_depart())}</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        return

    data = await state.get_data()
    norm_depart = normalize_date(data.get("depart_date", ""))
    norm_return = normalize_date(message.text)
    if norm_return and norm_depart and norm_return <= norm_depart:
        await message.answer(
            "❌ Дата возврата не может быть раньше или равна дате вылета.\n"
            "Укажите правильную дату возврата.",
            reply_markup=CANCEL_KB,
        )
        return

    cancel_inactivity(message.chat.id)
    await state.update_data(return_date=message.text)

    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(message, state)
        return
    await ask_flight_type(message, state)


# ════════════════════════════════════════════════════════════════
# FSM: тип рейса
# ════════════════════════════════════════════════════════════════

async def ask_flight_type(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Прямые",       callback_data="ft_direct"),
         InlineKeyboardButton(text="🔀 С пересадкой", callback_data="ft_transfer")],
        [InlineKeyboardButton(text="🔍 Все варианты", callback_data="ft_all")],
    ])
    await message.answer("✈️ <b>Шаг 4/6</b> — Тип рейса\n\nКакие рейсы показывать?",
                         parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.flight_type)


@router.callback_query(FlightSearch.flight_type, F.data.in_({"ft_direct", "ft_transfer", "ft_all"}))
async def process_flight_type(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    code_map = {"ft_direct": "direct", "ft_transfer": "transfer", "ft_all": "all"}
    await state.update_data(flight_type=code_map[callback.data])
    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(callback.message, state)
    else:
        await ask_adults(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.flight_type, F.text == "В начало")
async def flight_type_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# FSM: пассажиры
# ════════════════════════════════════════════════════════════════

async def ask_adults(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(i), callback_data=f"adults_{i}") for i in range(1, 5)],
        [InlineKeyboardButton(text=str(i), callback_data=f"adults_{i}") for i in range(5, 9)],
        [InlineKeyboardButton(text="9",    callback_data="adults_9")],
    ])
    await message.answer("✈️ <b>Шаг 5/6</b> — Пассажиры\n\nСколько взрослых пассажиров (от 12 лет)?",
                         parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.adults)


@router.callback_query(FlightSearch.adults, F.data.regexp(r"^adults_[1-9]$"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)
    if adults == 9:
        await state.update_data(children=0, infants=0, passenger_desc="9 взр.", passenger_code="9")
        await show_summary(callback.message, state)
    else:
        await ask_has_children(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.adults, F.text == "В начало")
async def adults_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_has_children(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👶 Да", callback_data="hc_yes"),
         InlineKeyboardButton(text="✅ Нет", callback_data="hc_no")],
    ])
    await message.answer("С вами летят дети?", reply_markup=kb)
    await state.set_state(FlightSearch.has_children)


@router.callback_query(FlightSearch.has_children, F.data.in_({"hc_yes", "hc_no"}))
async def process_has_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    if callback.data == "hc_yes":
        await ask_children(callback.message, state)
    else:
        data = await state.get_data()
        adults = data["adults"]
        pd = f"{adults} взр."
        await state.update_data(children=0, infants=0, passenger_desc=pd, passenger_code=str(adults))
        await show_summary(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.has_children, F.text == "В начало")
async def has_children_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_children(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    max_ch = 9 - adults
    nums = list(range(0, max_ch + 1))
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"ch_{n}") for n in nums[i:i+5]]
            for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer(
        "Сколько детей (от 2 до 11 лет)?\nЕсли у вас младенцы, укажете дальше.",
        reply_markup=kb,
    )
    await state.set_state(FlightSearch.children)


@router.callback_query(FlightSearch.children, F.data.regexp(r"^ch_\d+$"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    adults = data["adults"]
    children = int(callback.data.split("_")[1])
    if children < 0 or children > 9 - adults:
        await callback.answer()
        return
    await state.update_data(children=children)
    if 9 - adults - children == 0:
        pd = f"{adults} взр." + (f", {children} дет." if children else "")
        await state.update_data(infants=0, passenger_desc=pd,
                                passenger_code=build_passenger_code(adults, children, 0))
        await show_summary(callback.message, state)
    else:
        await ask_infants(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.children, F.text == "В начало")
async def children_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


async def ask_infants(message: Message, state: FSMContext):
    data = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    max_inf  = min(adults, 9 - adults - children)
    nums = list(range(0, max_inf + 1))
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"inf_{n}") for n in nums[i:i+5]]
            for i in range(0, len(nums), 5)]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("Сколько младенцев? (младше 2 лет без места)", reply_markup=kb)
    await state.set_state(FlightSearch.infants)


@router.callback_query(FlightSearch.infants, F.data.regexp(r"^inf_\d+$"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    adults   = data["adults"]
    children = data.get("children", 0)
    infants  = int(callback.data.split("_")[1])
    if infants < 0 or infants > min(adults, 9 - adults - children):
        await callback.answer()
        return
    await state.update_data(infants=infants)
    await show_summary(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.infants, F.text == "В начало")
async def infants_to_menu(message: Message, state: FSMContext):
    cancel_inactivity(message.chat.id)
    mark_fsm_inactive(message.chat.id)
    await state.clear()
    await message.answer("Используйте кнопки навигации внизу.")


# ════════════════════════════════════════════════════════════════
# Экран подтверждения (summary)
# ════════════════════════════════════════════════════════════════

async def show_summary(message, state: FSMContext):
    await state.update_data(_edit_mode=False)
    data = await state.get_data()
    adults   = data.get("adults", 1)
    children = data.get("children", 0)
    infants  = data.get("infants", 0)

    passenger_code = build_passenger_code(adults, children, infants)
    passenger_desc = f"{adults} взр."
    if children: passenger_desc += f", {children} дет."
    if infants:  passenger_desc += f", {infants} мл."

    await state.update_data(passenger_code=passenger_code, passenger_desc=passenger_desc)
    data = await state.get_data()

    summary = "✈️ <b>Шаг 6/6</b> — Подтверждение\n\nПроверьте даты и данные:\n\n" + build_choices_summary(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",   callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",       callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",  callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",  callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",   callback_data="main_menu")],
    ])

    await message.answer(summary, parse_mode="HTML")
    await message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

    chat_id = message.chat.id if hasattr(message, "chat") else message.from_user.id
    schedule_inactivity(chat_id, message.from_user.id)


# ════════════════════════════════════════════════════════════════
# Редактирование из summary
# ════════════════════════════════════════════════════════════════

async def _do_edit_action(callback: CallbackQuery, state: FSMContext, action: str):
    if action == "route":
        await state.update_data(_edit_mode=True, origin_airports=None, origin_airport_label=None)
        await callback.message.edit_text(
            "✏️ Введите новый маршрут:\n<b>Город вылета - Город прибытия</b>\n\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.route)

    elif action == "dates":
        await state.update_data(_edit_mode=True)
        data = await state.get_data()
        hint = "\n(затем введёте дату обратного рейса)" if data.get("need_return") else ""
        await callback.message.edit_text(
            f"✏️ Введите новую дату вылета в формате <code>ДД.ММ</code>{hint}\n<i>Пример: {hint_depart()}</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.depart_date)

    elif action == "flight_type":
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_flight_type(callback.message, state)

    elif action == "passengers":
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_adults(callback.message, state)


@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    action = callback.data[len("edit_"):]
    await _do_edit_action(callback, state, action)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Подтверждение и запуск поиска
# ════════════════════════════════════════════════════════════════