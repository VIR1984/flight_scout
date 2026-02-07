# handlers/flight_wizard.py
"""
Пошаговый мастер поиска авиабилетов (без дублирования с start.py)
"""
import asyncio
from uuid import uuid4
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from states.flight_states import FlightSearch
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from utils.validators import (
    validate_route,
    validate_date,
    build_passenger_code,
    build_passenger_desc,
    format_user_date
)

router = Router()

# ===== Глобальная команда отмены (работает из любого состояния) =====
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена любого активного поиска"""
    current_state = await state.get_state()
    if not current_state:
        await message.answer("ℹ️ Нет активного поиска для отмены.")
        return
    
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")],
        [InlineKeyboardButton(text="💡 Ручной ввод", callback_data="manual_input")]
    ])
    await message.answer(
        "❌ Поиск отменён.\n"
        "Выберите действие:",
        reply_markup=kb
    )

# ===== Обработчики шагов пошагового поиска =====

@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    """Начало пошагового поиска"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
    ])
    await callback.message.edit_text(
        "✈️ <b>Начнём поиск билетов!</b>\n"
        "📍 <b>Шаг 1 из 5:</b> Введите маршрут в формате:\n"
        "<code>Город отправления - Город прибытия</code>\n"
        "📌 <b>Примеры:</b>\n"
        "• Москва - Сочи\n"
        "• СПБ - Бангкок (работает!)\n"
        "• Питер - Стамбул (работает!)\n"
        "• Везде - Стамбул (поиск из всех городов)\n"
        "💡 Можно писать через дефис или через пробел",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.route)
    await callback.answer()

@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    """Обработка маршрута"""
    origin, dest = validate_route(message.text)
    if not origin or not dest:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await message.answer(
            "❌ Неверный формат маршрута.\n"
            "Попробуйте ещё раз: <code>Москва - Сочи</code>",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    # Проверяем города
    if origin != "везде":
        orig_iata = CITY_TO_IATA.get(origin)
        if not orig_iata:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
            ])
            await message.answer(
                f"❌ Не знаю город отправления: <b>{origin}</b>\n"
                "Попробуйте: Москва, СПБ, Питер, Мск, Екб, Нск",
                parse_mode="HTML",
                reply_markup=kb
            )
            return
        origin_name = IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = None
        origin_name = "Везде"

    dest_iata = CITY_TO_IATA.get(dest)
    if not dest_iata:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await message.answer(
            f"❌ Не знаю город прибытия: <b>{dest}</b>\n"
            "Попробуйте другой город",
            parse_mode="HTML",
            reply_markup=kb
        )
        return
    dest_name = IATA_TO_CITY.get(dest_iata, dest.capitalize())

    # Сохраняем данные
    await state.update_data(
        origin=origin,
        origin_iata=orig_iata,
        dest=dest,
        dest_iata=dest_iata,
        origin_name=origin_name,
        dest_name=dest_name
    )

    # Переходим к дате вылета
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
    ])
    await message.answer(
        f"✅ Маршрут: <b>{origin_name} → {dest_name}</b>\n"
        "📅 <b>Шаг 2 из 5:</b> Введите дату вылета в формате <code>ДД.ММ</code>\n"
        "📌 <b>Пример:</b> 10.03",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.depart_date)

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    """Обработка даты вылета"""
    if not validate_date(message.text):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 10.03)",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    await state.update_data(depart_date=message.text)

    # Спрашиваем про обратный билет
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="need_return_yes")],
        [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="need_return_no")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
    ])

    await message.answer(
        f"✅ Дата вылета: <b>{message.text}</b>\n"
        "🔄 <b>Шаг 3 из 5:</b> Нужен ли обратный билет?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.need_return)

@router.callback_query(FlightSearch.need_return, F.data.startswith("need_return_"))
async def process_need_return(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора обратного билета"""
    need_return = callback.data == "need_return_yes"
    await state.update_data(need_return=need_return)

    if need_return:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await callback.message.edit_text(
            "📅 <b>Шаг 4 из 5:</b> Введите дату возврата в формате <code>ДД.ММ</code>\n"
            "📌 <b>Пример:</b> 15.03",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.return_date)
    else:
        # Пропускаем дату возврата, переходим к пассажирам
        await state.update_data(return_date=None)
        await ask_adults(callback.message, state)

    await callback.answer()

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    """Обработка даты возврата"""
    if not validate_date(message.text):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 15.03)",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    await state.update_data(return_date=message.text)
    # Переходим к пассажирам
    await ask_adults(message, state)

async def ask_adults(message_or_callback, state: FSMContext):
    """Запрашиваем количество взрослых"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data="adults_1"),
            InlineKeyboardButton(text="2", callback_data="adults_2"),
            InlineKeyboardButton(text="3", callback_data="adults_3"),
            InlineKeyboardButton(text="4", callback_data="adults_4"),
        ],
        [
            InlineKeyboardButton(text="5", callback_data="adults_5"),
            InlineKeyboardButton(text="6", callback_data="adults_6"),
            InlineKeyboardButton(text="7", callback_data="adults_7"),
            InlineKeyboardButton(text="8", callback_data="adults_8"),
        ],
        [
            InlineKeyboardButton(text="9", callback_data="adults_9"),
        ],
        [
            InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")
        ]
    ])

    text = "👥 <b>Шаг 5 из 5:</b> Сколько взрослых пассажиров (от 12 лет)?\n(max. до 9 человек)"

    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)

    await state.set_state(FlightSearch.adults)

@router.callback_query(FlightSearch.adults, F.data.startswith("adults_"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    """Обработка количества взрослых"""
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)

    # Если 9 взрослых - пропускаем детей и младенцев
    if adults == 9:
        await state.update_data(children=0, infants=0)
        await show_summary(callback.message, state)
    else:
        # Спрашиваем про детей
        max_children = 9 - adults
        kb_buttons = []
        row = []
        for i in range(0, max_children + 1):
            row.append(InlineKeyboardButton(text=str(i), callback_data=f"children_{i}"))
            if len(row) == 4:
                kb_buttons.append(row)
                row = []
        if row:
            kb_buttons.append(row)
        kb_buttons.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

        await callback.message.edit_text(
            f"👥 Взрослых: <b>{adults}</b>\n"
            f"👶 Сколько детей (от 2-11 лет)?\n"
            f"<i>Если у вас младенцы, укажете дальше</i>",
            parse_mode="HTML",
            reply_markup=kb
        )

    await state.set_state(FlightSearch.children)
    await callback.answer()

@router.callback_query(FlightSearch.children, F.data.startswith("children_"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    """Обработка количества детей"""
    children = int(callback.data.split("_")[1])
    await state.update_data(children=children)

    data = await state.get_data()
    adults = data["adults"]
    remaining = 9 - adults - children

    # Если места закончились - пропускаем младенцев
    if remaining == 0:
        await state.update_data(infants=0)
        await show_summary(callback.message, state)
    else:
        # Спрашиваем про младенцев (не больше взрослых)
        max_infants = min(adults, remaining)
        kb_buttons = []
        row = []
        for i in range(0, max_infants + 1):
            row.append(InlineKeyboardButton(text=str(i), callback_data=f"infants_{i}"))
            if len(row) == 4:
                kb_buttons.append(row)
                row = []
        if row:
            kb_buttons.append(row)
        kb_buttons.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

        await callback.message.edit_text(
            f"👥 Взрослых: <b>{adults}</b>\n"
            f"👶 Детей: <b>{children}</b>\n"
            f"🍼 Сколько младенцев? (младше 2-х лет без места)",
            parse_mode="HTML",
            reply_markup=kb
        )

    await state.set_state(FlightSearch.infants)
    await callback.answer()

@router.callback_query(FlightSearch.infants, F.data.startswith("infants_"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    """Обработка количества младенцев"""
    infants = int(callback.data.split("_")[1])
    await state.update_data(infants=infants)
    await show_summary(callback.message, state)
    await callback.answer()

async def show_summary(message, state: FSMContext):
    """Показываем сводку и подтверждаем поиск"""
    data = await state.get_data()
    adults = data["adults"]
    children = data.get("children", 0)
    infants = data.get("infants", 0)

    passenger_code = build_passenger_code(adults, children, infants)
    passenger_desc = build_passenger_desc(passenger_code)

    summary = (
        "📋 <b>Проверьте данные:</b>\n"
        f"📍 Маршрут: <b>{data['origin_name']} → {data['dest_name']}</b>\n"
        f"📅 Вылет: <b>{data['depart_date']}</b>\n"
    )
    if data.get("need_return") and data.get("return_date"):
        summary += f"📅 Возврат: <b>{data['return_date']}</b>\n"
    summary += f"👥 Пассажиры: <b>{passenger_desc}</b>\n"
    summary += "🔍 Начать поиск?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать поиск", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="edit_route")],
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="edit_passengers")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")]
    ])

    await state.update_data(
        passenger_code=passenger_code,
        passenger_desc=passenger_desc
    )

    await message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и запуск поиска"""
    data = await state.get_data()
    await callback.message.edit_text("⏳ Ищу билеты (включая с пересадками)...")

    # Определяем пункты вылета
    if data["origin"] == "везде":
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
    else:
        origins = [data["origin_iata"]]
        origin_name = data["origin_name"]

    dest_iata = data["dest_iata"]
    dest_name = data["dest_name"]

    # Запросы к API
    all_flights = []
    for i, orig in enumerate(origins):
        if i > 0:
            await asyncio.sleep(1)
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(data["depart_date"]),
            normalize_date(data["return_date"]) if data.get("return_date") else None
        )
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)

    if not all_flights:
        origin_iata = origins[0]
        d1 = data["depart_date"].replace('.', '')
        d2 = data["return_date"].replace('.', '') if data.get("return_date") else ''
        route = f"{origin_iata}{d1}{dest_iata}{d2}1"
        from dotenv import load_dotenv
        import os
        load_dotenv()
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link += f"?marker={marker}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales", url=link)],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "😔 Билеты не найдены.\n"
            "На Aviasales могут быть рейсы с пересадками — попробуйте:",
            reply_markup=kb
        )
        await state.clear()
        return

    # Сохраняем в кэш
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": data.get("need_return", False),
        "display_depart": format_user_date(data["depart_date"]),
        "display_return": format_user_date(data["return_date"]) if data.get("return_date") else None,
        "original_depart": data["depart_date"],
        "original_return": data["return_date"],
        "passenger_desc": data["passenger_desc"],
        "passengers_code": data["passenger_code"]
    })

    # Расчет минимальной цены
    min_price = min([f.get("value") or f.get("price") or 999999 for f in all_flights])
    total_flights = len(all_flights)

    # Формируем сообщение
    text = (
        f"✅ <b>Билеты найдены!</b>\n"
        f"📍 <b>Маршрут:</b> {origin_name} → {dest_name}\n"
        f"📅 <b>Дата вылета:</b> {format_user_date(data['depart_date'])}\n"
    )
    if data.get("need_return") and data.get("return_date"):
        text += f"📅 <b>Дата возврата:</b> {format_user_date(data['return_date'])}\n"
    text += (
        f"👥 <b>Пассажиры:</b> {data['passenger_desc']}\n"
        f"💰 <b>Самая низкая цена от:</b> {min_price} ₽/чел.\n"
        f"📊 <b>Всего вариантов:</b> {total_flights}\n"
        f"Выберите, как хотите посмотреть билеты:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✈️ Самый дешёвый ({min_price} ₽)",
                callback_data=f"show_top_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"📋 Все варианты ({total_flights})",
                callback_data=f"show_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="📉 Следить за ценой",
                callback_data=f"watch_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="↩️ В меню",
                callback_data="main_menu"
            )
        ]
    ])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

# ===== Обработчики редактирования =====

@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    """Возврат к редактированию шага"""
    step = callback.data.split("_")[1]
    if step == "route":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await callback.message.edit_text(
            "📍 Введите маршрут: <code>Город - Город</code>",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.route)
    elif step == "dates":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
        ])
        await callback.message.edit_text(
            "📅 Введите дату вылета: <code>ДД.ММ</code>",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.depart_date)
    elif step == "passengers":
        await ask_adults(callback, state)
    await callback.answer()

# ===== Глобальный обработчик отмены (должен быть зарегистрирован в основном роутере) =====
@router.callback_query(F.data == "cancel_search")
async def cancel_search(callback: CallbackQuery, state: FSMContext):
    """Глобальный обработчик отмены (работает из любого состояния)"""
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")],
        [InlineKeyboardButton(text="💡 Ручной ввод", callback_data="manual_input")]
    ])
    try:
        await callback.message.edit_text(
            "❌ Поиск отменён.\n"
            "Выберите действие:",
            reply_markup=kb
        )
    except:
        await callback.message.answer(
            "❌ Поиск отменён.\n"
            "Выберите действие:",
            reply_markup=kb
        )
    await callback.answer()