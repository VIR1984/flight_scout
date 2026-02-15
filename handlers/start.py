from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from handlers.everywhere_search import (
    search_origin_everywhere,
    search_destination_everywhere,
    process_everywhere_search,
    handle_everywhere_search_manual,
    format_user_date,
    build_passenger_desc
)
from services.flight_search import clean_aviasales_link, create_partner_link
import asyncio
import os
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

router = Router()
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
])

class FlightSearch(StatesGroup):
    route = State()
    depart_date = State()
    need_return = State()
    return_date = State()
    flight_type = State()
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
        hint + "\n📅 Введите дату вылета в формате <code>ДД.ММ</code>\n"
        "📌 <b>Пример:</b> 10.03",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.depart_date)

# ... остальные обработчики (depart_date, need_return, return_date, flight_type, passengers) без изменений ...

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
        search_type = "origin_everywhere"
        # Фильтрация для "Везде → Город"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]

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
        search_type = "destination_everywhere"
        # Фильтрация для "Город → Везде"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]

        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return

    # ... обычный поиск (без изменений) ...

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

    # === ОСНОВНАЯ ССЫЛКА ===
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

    # === АЛЬТЕРНАТИВНАЯ ССЫЛКА ===
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
        clean_booking = clean_aviasales_link(booking_link)
        booking_link = await create_partner_link(clean_booking, marker, trs, sub_id)

        clean_fallback = clean_aviasales_link(fallback_link)
        fallback_link = await create_partner_link(clean_fallback, marker, trs, sub_id)

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
            InlineKeyboardButton(text=f"🚖 Трансфер в {dest_name}", url=transfer_link)
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()