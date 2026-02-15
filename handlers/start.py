from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from typing import Dict, Any, Optional
import asyncio
import os
import re
from uuid import uuid4
from services.flight_search import (
    search_flights,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    update_passengers_in_link,
    find_cheapest_flight_on_exact_date,
    clean_aviasales_link,
    create_partner_link
)
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from handlers.everywhere_search import (
    search_origin_everywhere,
    search_destination_everywhere,
    process_everywhere_search,
    handle_everywhere_search_manual,
    format_user_date,
    build_passenger_desc
)
from utils.logger import logger

router = Router()
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
])


class FlightSearch(StatesGroup):
    route = State()
    depart_date = State()
    need_return = State()
    return_date = State()
    flight_type = State()  # ← НОВЫЙ ШАГ: выбор типа рейса
    adults = State()
    children = State()
    infants = State()
    confirm = State()


def validate_route(text: str) -> tuple:
    text = text.strip().lower()
    if re.search(r'\s+[-→—>]+\s+', text):
        parts = re.split(r'\s+[-→—>]+\s+', text, maxsplit=1)
    elif any(sym in text for sym in ['→', '—', '>']):
        parts = re.split(r'[→—>]+', text, maxsplit=1)
    else:
        parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None, None
    origin = parts[0].strip()
    dest = parts[1].strip()
    origin = origin.replace("санкт петербург", "санкт-петербург")
    dest = dest.replace("ростов на дону", "ростов-на-дону")
    return origin, dest


def validate_date(date_str: str) -> bool:
    try:
        day, month = map(int, date_str.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except:
        return False


@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✈️ <b>Начнём поиск билетов!</b>\n"
        "📍 <b>Шаг 1 из 6:</b> Введите маршрут в формате:\n"
        "<code>Город отправления - Город прибытия</code>\n"
        "📌 <b>Примеры:</b>\n"
        "• Москва - Сочи\n"
        "• СПБ - Бангкок\n"
        "• Везде - Стамбул (поиск из всех городов России)\n"
        "• Стамбул - Везде (поиск из Стамбула → топ-3 направлений)\n"
        "💡 Можно писать через дефис или через пробел",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.route)
    await callback.answer()


@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    origin, dest = validate_route(message.text)
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n"
            "Попробуйте ещё раз: <code>Москва - Сочи</code>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return

    if origin != "везде":
        orig_iata = CITY_TO_IATA.get(origin)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город отправления: {origin}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        origin_name = IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = None
        origin_name = "Везде"

    if dest != "везде":
        dest_iata = CITY_TO_IATA.get(dest)
        if not dest_iata:
            await message.answer(f"❌ Не знаю город прибытия: {dest}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        dest_name = IATA_TO_CITY.get(dest_iata, dest.capitalize())
    else:
        dest_iata = None
        dest_name = "Везде"

    if origin == "везде" and dest == "везде":
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return

    await state.update_data(
        origin=origin,
        origin_iata=orig_iata,
        dest=dest,
        dest_iata=dest_iata,
        origin_name=origin_name,
        dest_name=dest_name
    )

    if dest == "везде" or origin == "везде":
        await state.update_data(need_return=False, return_date=None)
        await ask_flight_type(message, state)
        return

    await message.answer(
        f"✈️ Маршрут: <b>{origin_name} → {dest_name}</b>\n"
        "📅 <b>Шаг 2 из 6:</b> Введите дату вылета в формате <code>ДД.ММ</code>\n"
        "📌 <b>Пример:</b> 10.03",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.depart_date)


@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 10.03)",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    await state.update_data(depart_date=message.text)
    data = await state.get_data()
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    if is_origin_everywhere or is_dest_everywhere:
        await state.update_data(need_return=False, return_date=None)
        await ask_flight_type(message, state)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="need_return_yes")],
        [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="need_return_no")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    await message.answer(
        "🔄 Нужен ли обратный билет?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.need_return)


@router.callback_query(FlightSearch.need_return, F.data.startswith("need_return_"))
async def process_need_return(callback: CallbackQuery, state: FSMContext):
    need_return = callback.data == "need_return_yes"
    await state.update_data(need_return=need_return)
    if need_return:
        await callback.message.edit_text(
            "📅 <b>Шаг 4 из 6:</b> Введите дату возврата в формате <code>ДД.ММ</code>\n"
            "📌 <b>Пример:</b> 15.03",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await state.update_data(return_date=None)
        await ask_flight_type(callback.message, state)
    await callback.answer()


@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 15.03)",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    await state.update_data(return_date=message.text)
    await ask_flight_type(message, state)


async def ask_flight_type(message_or_callback, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✈️ Прямые", callback_data="flight_type_direct"),
            InlineKeyboardButton(text="🔄 С пересадкой", callback_data="flight_type_transfer"),
        ],
        [
            InlineKeyboardButton(text="📊 Все варианты", callback_data="flight_type_all")
        ],
        [
            InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")
        ]
    ])
    text = (
        "✈️ <b>Шаг 5 из 6:</b> Какие рейсы показывать?\n"
        "• <b>Прямые</b> — без пересадок (быстрее, часто дороже)\n"
        "• <b>С пересадкой</b> — 1+ пересадка (дешевле, дольше в пути)\n"
        "• <b>Все варианты</b> — покажу и те, и другие (рекомендуется)"
    )
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.flight_type)


@router.callback_query(FlightSearch.flight_type, F.data.startswith("flight_type_"))
async def process_flight_type(callback: CallbackQuery, state: FSMContext):
    flight_type = callback.data.split("_")[2]
    await state.update_data(flight_type=flight_type)
    await ask_adults(callback.message, state)
    await callback.answer()


async def ask_adults(message_or_callback, state: FSMContext):
    kb_buttons = []
    row = []
    for i in range(1, 10):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"adults_{i}"))
        if len(row) == 4:
            kb_buttons.append(row)
            row = []
    if row:
        kb_buttons.append(row)
    kb_buttons.append([InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    text = "👥 <b>Шаг 6 из 6:</b> Сколько взрослых пассажиров (от 12 лет)?\n(max. до 9 человек)"
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.adults)


@router.callback_query(FlightSearch.adults, F.data.startswith("adults_"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)
    data = await state.get_data()
    remaining = 9 - adults
    if remaining == 0:
        await state.update_data(children=0, infants=0)
        await show_summary(callback.message, state)
    else:
        kb_buttons = []
        row = []
        for i in range(0, min(remaining + 1, 10)):
            row.append(InlineKeyboardButton(text=str(i), callback_data=f"children_{i}"))
            if len(row) == 4:
                kb_buttons.append(row)
                row = []
        if row:
            kb_buttons.append(row)
        kb_buttons.append([InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        await callback.message.edit_text(
            "👶 Сколько детей (от 2-11 лет)?\n"
            "<i>Если у вас младенцы, укажете дальше</i>",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.children)
    await callback.answer()


@router.callback_query(FlightSearch.children, F.data.startswith("children_"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    children = int(callback.data.split("_")[1])
    await state.update_data(children=children)
    data = await state.get_data()
    adults = data["adults"]
    remaining = 9 - adults - children
    if remaining == 0:
        await state.update_data(infants=0)
        await show_summary(callback.message, state)
    else:
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
        kb_buttons.append([InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        await callback.message.edit_text(
            "🍼 Сколько младенцев (до 2 лет)?\n"
            "<i>Не больше, чем взрослых</i>",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.infants)
    await callback.answer()


@router.callback_query(FlightSearch.infants, F.data.startswith("infants_"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    infants = int(callback.data.split("_")[1])
    await state.update_data(infants=infants)
    await show_summary(callback.message, state)
    await callback.answer()


def format_passenger_desc(code: str) -> str:
    try:
        adults = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants = int(code[2]) if len(code) > 2 else 0
        parts = []
        if adults: parts.append(f"{adults} взр.")
        if children: parts.append(f"{children} реб.")
        if infants: parts.append(f"{infants} мл.")
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."


async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    children = data.get("children", 0)
    infants = data.get("infants", 0)

    print(f"[DEBUG] Перед вызовом build_passenger_code: adults={adults}, children={children}, infants={infants}")
    passenger_code = build_passenger_code(adults, children, infants)
    print(f"[DEBUG] Получен passenger_code: '{passenger_code}'")
    passenger_desc = format_passenger_desc(passenger_code)

    summary = (
        "📋 <b>Проверьте данные:</b>\n"
        f"📍 Маршрут: <b>{data['origin_name']} → {data['dest_name']}</b>\n"
        f"📅 Вылет: <b>{data['depart_date']}</b>"
    )
    if data.get("need_return") and data.get("return_date"):
        summary += f"\n📅 Возврат: <b>{data['return_date']}</b>"

    # Добавляем информацию о типе рейса в сводку
    flight_type = data.get("flight_type", "all")
    if flight_type == "direct":
        summary += "\n✈️ Тип рейса: <b>Прямые</b>"
    elif flight_type == "transfer":
        summary += "\n✈️ Тип рейса: <b>С пересадкой</b>"
    else:
        summary += "\n✈️ Тип рейса: <b>Все варианты</b>"

    summary += f"\n👥 Пассажиры: <b>{passenger_desc}</b>"
    summary += "\n🔍 Начать поиск?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать поиск", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="edit_route")],
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Изменить тип рейса", callback_data="edit_flight_type")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="edit_passengers")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
    ])

    await state.update_data(passenger_code=passenger_code, passenger_desc=passenger_desc)
    print(f"[DEBUG show_summary] После сохранения: passenger_code='{passenger_code}'")
    await message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)


@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    step = callback.data.split("_")[1]
    if step == "route":
        await callback.message.edit_text(
            "📍 Введите маршрут: <code>Город - Город</code>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.route)
    elif step == "dates":
        await callback.message.edit_text(
            "📅 Введите дату вылета: <code>ДД.ММ</code>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.depart_date)
    elif step == "flight_type":
        await ask_flight_type(callback, state)
    elif step == "passengers":
        await ask_adults(callback, state)
    await callback.answer()


@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    print(f"[DEBUG confirm_search] Состояние FSM перед вызовом API: {data}")
    await callback.message.edit_text("⏳ Ищу билеты...")

    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    flight_type = data.get("flight_type", "all")
    direct_only = (flight_type == "direct")
    transfers_only = (flight_type == "transfer")

    if is_origin_everywhere and not is_dest_everywhere:
        all_flights = await search_origin_everywhere(
            dest_iata=data["dest_iata"],
            depart_date=data["depart_date"],
            flight_type=data.get("flight_type", "all")
        )
        # Фильтрация для "Везде → Город"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]

        search_type = "origin_everywhere"
        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return

    elif not is_origin_everywhere and is_dest_everywhere:
        all_flights = await search_destination_everywhere(
            origin_iata=data["origin_iata"],
            depart_date=data["depart_date"],
            flight_type=data.get("flight_type", "all")
        )
        # Фильтрация для "Город → Везде"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]

        search_type = "destination_everywhere"
        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return

    origins = [data["origin_iata"]]
    destinations = [data["dest_iata"]]
    origin_name = data["origin_name"]
    dest_name = data["dest_name"]
    all_flights = []

    for orig in origins:
        for dest in destinations:
            if orig == dest:
                continue
            flights = await search_flights(
                orig,
                dest,
                normalize_date(data["depart_date"]),
                normalize_date(data["return_date"]) if data.get("return_date") else None,
                direct=direct_only
            )
            if direct_only:
                flights = [f for f in flights if f.get("transfers", 999) == 0]
            elif transfers_only:
                flights = [f for f in flights if f.get("transfers", 0) > 0]

            for f in flights:
                f["origin"] = orig
                f["destination"] = dest
            all_flights.extend(flights)
            await asyncio.sleep(0.5)

    if direct_only and not all_flights:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Показать рейсы с пересадками",
                    callback_data=f"retry_with_transfers_{callback.message.message_id}"
                )
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "😔 Прямых рейсов на эти даты не найдено.\n"
            "Хотите посмотреть варианты с пересадками? Они часто дешевле!",
            reply_markup=kb
        )
        await state.clear()
        return

    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(data["depart_date"])
        d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
        route = f"{origin_iata}{d1}{destinations[0]}{d2}1"
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            from services.flight_search import add_marker_to_url
            link = add_marker_to_url(link, marker)
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

    cache_id = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    display_return = format_user_date(data["return_date"]) if data.get("return_date") else None
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": data["dest_iata"],
        "is_roundtrip": data.get("need_return", False),
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": data["depart_date"],
        "original_return": data["return_date"],
        "passenger_desc": data["passenger_desc"],
        "passengers_code": data["passenger_code"],
        "origin_everywhere": False,
        "dest_everywhere": False,
        "flight_type": flight_type
    })

    top_flight = find_cheapest_flight_on_exact_date(
        all_flights,
        data["depart_date"],
        data.get("return_date")
    )
    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata = top_flight["origin"]
    dest_iata = top_flight.get("destination") or data["dest_iata"]
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)

    def format_datetime(dt_str):
        if not dt_str:
            return "??:??"
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
            return dt.strftime("%H:%M")
        except:
            return dt_str.split('T')[1][:5] if 'T' in dt_str else "??:??"

    def format_duration(minutes):
        if not minutes:
            return "—"
        hours = minutes // 60
        mins = minutes % 60
        parts = []
        if hours: parts.append(f"{hours}ч")
        if mins: parts.append(f"{mins}м")
        return " ".join(parts) if parts else "—"

    departure_time = format_datetime(top_flight.get("departure_at", ""))
    arrival_time = format_datetime(top_flight.get("return_at", ""))
    duration = format_duration(top_flight.get("duration", 0))
    transfers = top_flight.get("transfers", 0)

    AIRPORT_NAMES = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport = AIRPORT_NAMES.get(dest_iata, dest_iata)

    if transfers == 0:
        transfer_text = "✈️ Прямой рейс"
    elif transfers == 1:
        transfer_text = "✈️ 1 пересадка"
    else:
        transfer_text = f"✈️ {transfers} пересадки"

    # === ФОРМИРОВАНИЕ ТЕКСТА В ТРЕБУЕМОМ ПОРЯДКЕ ===
    text = "✅ <b>Самый дешёвый вариант</b>\n"

    # --- ЛОГИКА РАСЧЁТА ЦЕНЫ ---
    price_per_passenger = int(float(price)) if price != "?" else 0

    passengers_code = data.get("passenger_code", "1")
    try:
        num_adults = int(passengers_code[0]) if passengers_code and passengers_code[0].isdigit() else 1
    except (IndexError, ValueError):
        num_adults = 1

    estimated_total_price = price_per_passenger * num_adults if price != "?" else "?"

    if price != "?":
        text += f"💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
    else:
        text += f"💰 <b>Цена за 1 пассажира:</b> {price} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"

    # Обратный рейс (если есть)
    if data.get("need_return", False) and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"

    # Рейс
    text += f"\n🛫 <b>Рейс:</b> {origin_name} → {dest_name}"

    # Города и коды аэропортов
    text += f"\n📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})"

    # Продолжительность
    text += f"\n⏱️ <b>Продолжительность:</b> {duration}"

    # Тип рейса
    text += f"\n{transfer_text}"

    # Авиакомпания и номер рейса (если есть)
    airline = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
        text += f"\n✈️ <b>Авиакомпания и номер рейса:</b> {flight_display}"

    text += f"\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"

    # === ОСНОВНАЯ ССЫЛКА: flight["link"] с исправленным числом пассажиров ===
    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    passengers_code = data.get("passenger_code", "1")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=data["depart_date"],
            passengers_code=passengers_code,
            return_date=data["return_date"] if data.get("need_return") else None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    # === АЛЬТЕРНАТИВНАЯ ССЫЛКА: generate_booking_link() ===
    fallback_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data["depart_date"],
        passengers_code=passengers_code,
        return_date=data["return_date"] if data.get("need_return") else None
    )
    if not fallback_link.startswith(('http://', 'https://')):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    # === ГЕНЕРИРУЕМ ПАРТНЁРСКИЕ ССЫЛКИ ЧЕРЕЗ TRAVELPAYOUTS API ===
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    trs = os.getenv("TRS_ID", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram_bot_v2").strip()

    if marker and trs:
        # Очищаем и преобразуем основную ссылку
        clean_booking = clean_aviasales_link(booking_link)
        booking_link = await create_partner_link(clean_booking, marker, trs, sub_id)

        # Очищаем и преобразуем альтернативную ссылку
        clean_fallback = clean_aviasales_link(fallback_link)
        fallback_link = await create_partner_link(clean_fallback, marker, trs, sub_id)
    else:
        logger.warning("⚠️ TRAFFIC_SOURCE или TRS_ID не заданы — ссылки без партнёрского отслеживания")

    # === КНОПКИ ===
    kb_buttons = []
    if booking_link:
        kb_buttons.append([
            InlineKeyboardButton(text=f"✈️ Забронировать за {price} ₽", url=booking_link)
        ])
    kb_buttons.append([
        InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")
    ])

    SUPPORTED_TRANSFER_AIRPORTS = [
        "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
        "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
        "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK"
    ]
    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-2, [
            InlineKeyboardButton(
                text=f"🚖 Трансфер в {dest_name}",
                url=transfer_link
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()


# ===== ГЛОБАЛЬНЫЙ ОБРАБОТЧИК =====
@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await message.answer(
            "Пожалуйста, завершите текущий поиск или отмените его через кнопку ↩️ В меню",
            reply_markup=CANCEL_KB
        )
        return
    if message.text.startswith("/"):
        return
    await handle_flight_request(message)

@router.callback_query(F.data.startswith("unwatch_"))
async def handle_unwatch(callback: CallbackQuery):
    key = callback.data.split("unwatch_")[1]
    user_id = callback.from_user.id
    if f":{user_id}:" not in key:
        await callback.answer("❌ Это не ваше отслеживание!", show_alert=True)
        return
    await redis_client.remove_watch(user_id, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "✅ Отслеживание цены остановлено.\n"
        "Больше не буду присылать уведомления по этому маршруту.",
        reply_markup=kb
    )
    await callback.answer()

# ===== ОБРАБОТЧИК ПОВТОРНОГО ПОИСКА С ПЕРЕСАДКАМИ =====
@router.callback_query(F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    # Просто возвращаем пользователя в главное меню с подсказкой
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        # [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")]
    ])
    await callback.message.edit_text(
        "🔄 <b>Поиск рейсов с пересадками</b>\n\n"
        "Начните новый поиск и на шаге выбора типа рейса выберите:\n"
        "• <b>С пересадкой</b> — для поиска только рейсов с пересадками\n"
        "• <b>Все варианты</b> — для просмотра всех доступных рейсов",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()
