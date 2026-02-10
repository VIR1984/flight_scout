import json
import asyncio
import os
import re
from uuid import uuid4
from typing import Dict, Any
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from services.flight_search import search_flights, generate_booking_link, normalize_date, format_avia_link_date, find_cheapest_flight_on_exact_date
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from handlers.everywhere_search import (
    search_origin_everywhere,
    search_destination_everywhere,
    process_everywhere_search,
    handle_everywhere_search_manual,
    add_marker_to_url,
    format_user_date,
    build_passenger_desc
)

router = Router()
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
])

class FlightSearch(StatesGroup):
    route = State()
    depart_date = State()
    need_return = State()
    return_date = State()
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

def build_passenger_code(adults: int, children: int = 0, infants: int = 0) -> str:
    adults = max(1, adults)
    total = adults + children + infants
    if total > 9:
        remaining = 9 - adults
        if children + infants > remaining:
            children = min(children, remaining)
            infants = max(0, remaining - children)
    if infants > adults:
        infants = adults
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    return code

@router.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")]
    ])
    await message.answer(
        "👋 Привет! Я найду вам дешёвые авиабилеты.\n"
        "Выберите способ поиска:",
        reply_markup=kb
    )

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📖 Справка", callback_data="show_help")]
    ])
    try:
        await callback.message.edit_text(
            "👋 Привет! Я найду вам дешёвые авиабилеты.\n"
            "Выберите способ поиска:",
            reply_markup=kb
        )
    except:
        await callback.message.answer(
            "👋 Привет! Я найду вам дешёвые авиабилеты.\n"
            "Выберите способ поиска:",
            reply_markup=kb
        )
    await callback.answer()

@router.callback_query(F.data == "show_help")
async def show_help(callback: CallbackQuery):
    help_text = (
        "📖 <b>Справка по использованию</b>\n"
        "✈️ <b>Пошаговый поиск (рекомендуется):</b>\n"
        "1. Нажмите «Найти билеты»\n"
        "2. Следуйте инструкциям бота:\n"
        "   • Укажите маршрут (город отправления — город прибытия)\n"
        "   • Введите дату вылета в формате <code>ДД.ММ</code>\n"
        "   • Укажите, нужен ли обратный билет\n"
        "   • Выберите количество пассажиров кнопками\n"
        "3. Получите результат и перейдите к бронированию\n"
        "\n"
        "✍️ <b>Ручной ввод:</b>\n"
        "Можно ввести всё одной строкой в формате:\n"
        "<code>Город - Город ДД.ММ</code>\n"
        "\n"
        "📌 <b>Примеры:</b>\n"
        "• <code>Москва - Сочи 10.03</code>\n"
        "• <code>Москва - Сочи 10.03 - 15.03</code>\n"
        "• <code>Москва - Бангкок 20.03 2 взр</code>\n"
        "• <code>Везде - Стамбул 10.03</code>  ← поиск из всех городов России в Стамбул\n"
        "• <code>Стамбул - Везде 10.03</code>  ← поиск из Стамбула во все популярные города мира (топ-3 направлений)\n"
        "• <code>СПБ - Анталия 05.06</code>\n"
        "\n"
        "💡 <b>Важно:</b>\n"
        "• Даты указывайте в формате <code>ДД.ММ</code> (например: 10.03)\n"
        "• Для обратного билета укажите 2 даты через дефис/пробел\n"
        "• Можно писать «Везде» вместо города вылета ИЛИ прибытия (но не оба сразу)\n"
        "• Максимум 9 пассажиров в бронировании\n"
        "• Младенцев не больше, чем взрослых"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(help_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✈️ <b>Начнём поиск билетов!</b>\n"
        "📍 <b>Шаг 1 из 5:</b> Введите маршрут в формате:\n"
        "<code>Город отправления - Город прибытия</code>\n"
        "\n"
        "📌 <b>Примеры:</b>\n"
        "• Москва - Сочи\n"
        "• СПБ - Бангкок\n"
        "• Везде - Стамбул (поиск из всех городов России)\n"
        "• Стамбул - Везде (поиск из Стамбула → топ-3 направлений)\n"
        "\n"
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
    if dest == "везде":
        hint = f"✈️ Буду искать рейсы из <b>{origin_name}</b> во все популярные города мира (покажу топ-3)"
    elif origin == "везде":
        hint = f"✈️ Буду искать рейсы из всех городов России в <b>{dest_name}</b>"
    else:
        hint = f"✈️ Маршрут: <b>{origin_name} → {dest_name}</b>"
    await message.answer(
        hint + "\n"
        "📅 <b>Шаг 2 из 5:</b> Введите дату вылета в формате <code>ДД.ММ</code>\n"
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
        await ask_adults(message, state)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="need_return_yes")],
        [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="need_return_no")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
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
    need_return = callback.data == "need_return_yes"
    await state.update_data(need_return=need_return)
    if need_return:
        await callback.message.edit_text(
            "📅 <b>Шаг 4 из 5:</b> Введите дату возврата в формате <code>ДД.ММ</code>\n"
            "📌 <b>Пример:</b> 15.03",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await state.update_data(return_date=None)
        await ask_adults(callback.message, state)
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
    await ask_adults(message, state)

async def ask_adults(message_or_callback, state: FSMContext):
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
            InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")
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
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)
    if adults == 9:
        await state.update_data(children=0, infants=0)
        await show_summary(callback.message, state)
    else:
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
        kb_buttons.append([InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")])
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
    infants = int(callback.data.split("_")[1])
    await state.update_data(infants=infants)
    await show_summary(callback.message, state)
    await callback.answer()

async def show_summary(message, state: FSMContext):
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
        [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
    ])
    await state.update_data(
        passenger_code=passenger_code,
        passenger_desc=passenger_desc
    )
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
    elif step == "passengers":
        await ask_adults(callback, state)
    await callback.answer()

def _update_passengers_in_link(link: str, passengers_code: str) -> str:
    """
    Полностью заменяет пассажирский блок в ссылке Aviasales.
    Работает для:
    - one way
    - round trip
    - любого количества пассажиров (1, 2, 21, 211 и т.д.)
    """

    if not link or not passengers_code or not passengers_code.isdigit():
        return link

    # Разбираем URL
    if link.startswith('/'):
        parsed = None
        path = link
        query = ""
        if "?" in path:
            path, query = path.split("?", 1)
    else:
        parsed = urlparse(link)
        path = parsed.path
        query = parsed.query

    if not path.startswith("/search/"):
        return link

    route = path.replace("/search/", "")

    # Удаляем существующий пассажирский код (все цифры в конце маршрута)
    route = re.sub(r"\d+$", "", route)

    # Добавляем новый пассажирский код
    new_route = route + passengers_code

    new_path = f"/search/{new_route}"

    if query:
        new_path += f"?{query}"

    # Собираем обратно
    if parsed:
        return urlunparse(parsed._replace(path=new_path))
    else:
        return new_path


@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ Ищу билеты (включая с пересадками)...")
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"

    if is_origin_everywhere and not is_dest_everywhere:
        all_flights, search_type = await search_origin_everywhere(
            destination=data["dest_name"],
            dest_iata=data["dest_iata"],
            depart_date=data["depart_date"],
            return_date=None,
            passengers_code=data["passenger_code"],
            passenger_desc=data["passenger_desc"],
            state=state
        )
        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return
    elif not is_origin_everywhere and is_dest_everywhere:
        all_flights, search_type = await search_destination_everywhere(
            origin=data["origin_name"],
            origin_iata=data["origin_iata"],
            depart_date=data["depart_date"],
            return_date=None,
            passengers_code=data["passenger_code"],
            passenger_desc=data["passenger_desc"],
            state=state
        )
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
                normalize_date(data["return_date"]) if data.get("return_date") else None
            )
            for f in flights:
                f["origin"] = orig
                f["destination"] = dest
            all_flights.extend(flights)
        await asyncio.sleep(0.5)

    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(data["depart_date"])
        d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
        route = f"{origin_iata}{d1}{destinations[0]}{d2}1"
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
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
        "dest_everywhere": False
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

    header = f"✅ <b>Самый дешёвый вариант на {display_depart} ({data['passenger_desc']}):</b>"
    route_line = f"🛫 <b>Рейс: {origin_name}</b> → <b>{dest_name}</b>"
    text = (
        f"{header}\n"
        f"{route_line}\n"
        f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
        f"📅 Дата вылета: {display_depart}\n"
        f"⏱️ Продолжительность полета: {duration}\n"
        f"{transfer_text}\n"
    )

    airline = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
        text += f"✈️ {flight_display}\n"
    text += f"\n💰 <b>Цена от:</b> {price} ₽"
    if data.get("need_return", False) and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"
    text += f"\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"

    # === ОСНОВНАЯ ССЫЛКА: flight["link"] с исправленным числом пассажиров ===
    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    passengers_code = data.get("passengers_code", "1")
    if booking_link:
        booking_link = _update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        # Fallback на generate_booking_link
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

    # === ДОБАВЛЯЕМ МАРКЕР К ОБЕИМ ССЫЛКАМ ===
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
        fallback_link = add_marker_to_url(fallback_link, marker, sub_id)

    # === КНОПКИ ===
    kb_buttons = []

    # Основная кнопка — если есть link
    if booking_link:
        kb_buttons.append([
            InlineKeyboardButton(text=f"✈️ Забронировать за {price} ₽", url=booking_link)
        ])

    # Альтернативная кнопка — всегда
    kb_buttons.append([
        InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)
    ])

    # Отслеживание — привязано к cache_id (все рейсы, но используется cheapest)
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")
    ])

    # Меню
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

# ===== Ручной ввод =====
def parse_passengers(s: str) -> str:
    if not s: return "1"
    if s.isdigit(): return s
    adults = children = infants = 0
    for part in s.split(","):
        part = part.strip().lower()
        n = int(re.search(r"\d+", part).group()) if re.search(r"\d+", part) else 1
        if "взр" in part or "взросл" in part: adults = n
        elif "реб" in part or "дет" in part: children = n
        elif "мл" in part or "млад" in part: infants = n
    return str(adults) + (str(children) if children else "") + (str(infants) if infants else "")

async def handle_flight_request(message: Message):
    text = message.text.strip().lower()
    match = re.match(
        r"^([а-яёa-z\s]+?)\s*[-→>—\s]+\s*([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})(?:\s*[-–]\s*(\d{1,2}\.\d{1,2}))?\s*(.*)?$",
        text, re.IGNORECASE
    )
    if not match:
        await message.answer(
            "Неверный формат. Пример:\n<code>Орск - Пермь 10.03</code>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    is_roundtrip = bool(return_date)
    is_origin_everywhere = origin_city.strip() == "везде"
    is_dest_everywhere = dest_city.strip() == "везде"
    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return
    if is_origin_everywhere or is_dest_everywhere:
        passengers_code = parse_passengers((passengers_part or "").strip())
        success = await handle_everywhere_search_manual(
            message=message,
            origin_city=origin_city,
            dest_city=dest_city,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code,
            is_origin_everywhere=is_origin_everywhere,
            is_dest_everywhere=is_dest_everywhere
        )
        if success:
            return
    dest_iata = CITY_TO_IATA.get(dest_city.strip())
    if not dest_iata:
        await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
        return
    origin_clean = origin_city.strip()
    orig_iata = CITY_TO_IATA.get(origin_clean)
    if not orig_iata:
        await message.answer(f"Не знаю город вылета: {origin_clean}", reply_markup=CANCEL_KB)
        return
    origins = [orig_iata]
    origin_name = IATA_TO_CITY.get(orig_iata, origin_clean.capitalize())
    dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = build_passenger_desc(passengers_code)
    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None
    await message.answer("Ищу билеты (включая с пересадками)...")
    all_flights = []
    for orig in origins:
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None
        )
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(depart_date)
        d2 = format_avia_link_date(return_date) if return_date else ""
        route = f"{origin_iata}{d1}{dest_iata}{d2}{passengers_code}"
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link = add_marker_to_url(link, marker)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales (с пересадками)", url=link)],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        await message.answer(
            "Билеты не найдены 😢\n"
            "На Aviasales отображаются рейсы с пересадками — попробуйте:",
            reply_markup=kb
        )
        return

    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": is_roundtrip,
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": depart_date,
        "original_return": return_date,
        "passenger_desc": passenger_desc,
        "passengers_code": passengers_code,
        "origin_everywhere": False,
        "dest_everywhere": False
    })

    top_flight = find_cheapest_flight_on_exact_date(
        all_flights,
        depart_date,
        return_date
    )

    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata = top_flight["origin"]
    dest_iata = dest_iata
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

    header = f"✅ <b>Самый дешёвый вариант на {display_depart} ({passenger_desc}):</b>"
    route_line = f"🛫 Рейс: <b>{origin_name}</b> → <b>{dest_name}</b>"
    text = (
        f"{header}\n"
        f"{route_line}\n"
        f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
        f"📅 Дата вылета: {display_depart}\n"
        f"⏱️ Продолжительность полета: {duration}\n"
        f"{transfer_text}\n"
    )

    airline = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
        text += f"✈️ {flight_display}\n"
    text += f"\n💰 <b>Цена от:</b> {price} ₽"
    if is_roundtrip and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"
    text += f"\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"

    # === ОСНОВНАЯ ССЫЛКА: flight["link"] с исправленным числом пассажиров ===
    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    passengers_code = passengers_code
    if booking_link:
        booking_link = _update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_date,
            passengers_code=passengers_code,
            return_date=return_date if is_roundtrip else None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    # === АЛЬТЕРНАТИВНАЯ ССЫЛКА: generate_booking_link() ===
    fallback_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=depart_date,
        passengers_code=passengers_code,
        return_date=return_date if is_roundtrip else None
    )
    if not fallback_link.startswith(('http://', 'https://')):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    # === ДОБАВЛЯЕМ МАРКЕР К ОБЕИМ ССЫЛКАМ ===
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
        fallback_link = add_marker_to_url(fallback_link, marker, sub_id)

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
        InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")
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
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

# ===== Отслеживание цен =====
@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery):
    parts = callback.data.split("_")
    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        is_origin_everywhere = data.get("origin_everywhere", False)
        is_dest_everywhere = data.get("dest_everywhere", False)
        if is_dest_everywhere:
            origin = data["flights"][0]["origin"]
            dest = None
        elif is_origin_everywhere:
            origin = None
            dest = data.get("dest_iata") or data["flights"][0].get("destination")
        else:
            origin = data["flights"][0]["origin"]
            dest = data.get("dest_iata") or data["flights"][0].get("destination")
        min_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        price = min_flight.get("value") or min_flight.get("price")
        depart_date = data["original_depart"]
        return_date = data["original_return"]
    else:
        cache_id = parts[1]
        price = int(parts[2])
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        origin = top_flight["origin"]
        dest = data.get("dest_iata") or top_flight.get("destination")
        depart_date = data["original_depart"]
        return_date = data["original_return"]

    origin_name = IATA_TO_CITY.get(origin, origin) if origin else "Везде"
    dest_name = IATA_TO_CITY.get(dest, dest) if dest else "Везде"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 Любое изменение цены", callback_data=f"set_threshold:0:{cache_id}:{price}")],
        [InlineKeyboardButton(text="📉 Изменение на сотни ₽", callback_data=f"set_threshold:100:{cache_id}:{price}")],
        [InlineKeyboardButton(text="📉 Изменение на тысячи ₽", callback_data=f"set_threshold:1000:{cache_id}:{price}")],
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])
    await callback.message.answer(
        f"🔔 <b>Выберите условия уведомлений</b>\n"
        f"📍 Маршрут: {origin_name} → {dest_name}\n"
        f"📅 Вылет: {data['display_depart']}\n"
        f"💰 Текущая цена: {price} ₽",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("set_threshold:"))
async def handle_set_threshold(callback: CallbackQuery):
    _, threshold_str, cache_id, price_str = callback.data.split(":", 3)
    threshold = int(threshold_str)
    price = int(price_str)
    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return
    top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
    origin = top_flight["origin"]
    dest = data.get("dest_iata") or top_flight.get("destination")
    is_origin_everywhere = data.get("origin_everywhere", False)
    is_dest_everywhere = data.get("dest_everywhere", False)
    watch_key = await redis_client.save_price_watch(
        user_id=callback.from_user.id,
        origin=origin if not is_origin_everywhere else None,
        dest=dest if not is_dest_everywhere else None,
        depart_date=data["original_depart"],
        return_date=data["original_return"],
        current_price=price,
        passengers=data.get("passengers_code", "1"),
        threshold=threshold
    )
    origin_name = IATA_TO_CITY.get(origin, origin)
    dest_name = IATA_TO_CITY.get(dest, dest)
    if threshold == 0:
        condition_text = "любом изменении"
    elif threshold == 100:
        condition_text = "изменении на сотни ₽"
    else:
        condition_text = "изменении на тысячи ₽"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])
    response_text = (
        f"✅ <b>Отлично! Я буду следить за ценами</b>\n"
        f"📍 Маршрут: {origin_name} → {dest_name}\n"
        f"📅 Вылет: {data['display_depart']}\n"
    )
    if data.get('display_return'):
        response_text += f"📅 Возврат: {data['display_return']}\n"
    response_text += (
        f"💰 Текущая цена: {price} ₽\n"
        f"📉 Уведомлять при: {condition_text}\n"
        f"📲 Пришлю уведомление, если цена изменится!"
    )
    await callback.message.edit_text(response_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

# ===== Трансферы =====
transfer_context: Dict[int, Dict[str, Any]] = {}

@router.callback_query(F.data.startswith("ask_transfer_"))
async def handle_ask_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return
    airport_iata = context["airport_iata"]
    airport_names = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    airport_name = airport_names.get(airport_iata, airport_iata)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, покажи варианты", callback_data=f"show_transfer_{user_id}")],
        [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data=f"decline_transfer_{user_id}")],
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])
    await callback.message.answer(
        f"🚖 <b>Нужен трансфер из аэропорта {airport_name}?</b>\n"
        f"Я могу найти для вас варианты трансфера по лучшим ценам.\n"
        f"Показать предложения?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("decline_transfer_"))
async def handle_decline_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    if user_id in transfer_context:
        del transfer_context[user_id]
    if redis_client.client:
        decline_key = f"declined_transfer:{user_id}"
        await redis_client.client.setex(decline_key, 86400 * 7, "1")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "Хорошо! Если передумаете — просто выполните новый поиск билетов. ✈️",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data.startswith("show_transfer_"))
async def handle_show_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    if redis_client.client:
        decline_key = f"declined_transfer:{user_id}"
        declined = await redis_client.client.get(decline_key)
        if declined:
            await callback.answer(
                "Вы недавно отказались от трансферов. Предложения снова появятся через несколько дней.",
                show_alert=True
            )
            return
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return
    airport_iata = context["airport_iata"]
    transfer_date = context["transfer_date"]
    depart_date = context["depart_date"]
    dest_iata = context["dest_iata"]
    await callback.message.edit_text("Ищу варианты трансфера... 🚖")
    transfers = await search_transfers(
        airport_iata=airport_iata,
        transfer_date=transfer_date,
        adults=1
    )
    if not transfers:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "К сожалению, трансферы для этого аэропорта временно недоступны. 😢\n"
            "Попробуйте проверить позже или забронировать на сайте напрямую.",
            reply_markup=kb
        )
        return
    airport_names = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    airport_name = airport_names.get(airport_iata, airport_iata)
    message_text = (
        f"🚀 <b>Варианты трансфера {depart_date}</b>\n"
        f"📍 <b>{airport_name}</b> → центр города\n"
    )
    buttons = []
    for i, transfer in enumerate(transfers[:3], 1):
        price = transfer.get("price", 0)
        vehicle = transfer.get("vehicle", "Economy")
        duration = transfer.get("duration_minutes", 0)
        message_text += (
            f"\n<b>{i}. {vehicle}</b>\n"
            f"💰 {price} ₽\n"
            f"⏱️ ~{duration} мин в пути"
        )
        transfer_link = generate_transfer_link(
            transfer_id=str(transfer.get("id", "")),
            marker=os.getenv("TRAFFIC_SOURCE", ""),
            sub_id=f"telegram_{user_id}"
        )
        buttons.append([
            InlineKeyboardButton(text=f"🚀 Вариант {i}: {price} ₽", url=transfer_link)
        ])
    buttons.append([
        InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(message_text, parse_mode="HTML", reply_markup=kb)
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