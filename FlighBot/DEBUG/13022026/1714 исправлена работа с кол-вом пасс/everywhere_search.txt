# handlers/everywhere_search.py
import json
import asyncio
import os
from typing import Dict, Any, List, Tuple
from uuid import uuid4
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from services.flight_search import (
    search_flights,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    update_passengers_in_link,  # ← ДОБАВЛЕНО: для корректного обновления пассажиров в ссылках
    add_marker_to_url           # ← ДОБАВЛЕНО: единая функция для маркера
)
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client

def format_user_date(date_str: str) -> str:
    try:
        d, m = map(int, date_str.split('.'))
        year = 2026
        if m < 2 or (m == 2 and d < 8):
            year = 2027
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

def build_passenger_desc(code: str) -> str:
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

async def search_origin_everywhere(
    dest_iata: str,
    depart_date: str,
    flight_type: str = "all"
) -> List[Dict]:
    origins = GLOBAL_HUBS[:5]
    all_flights = []
    for orig in origins:
        if orig == dest_iata:
            continue
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            None
        )
        # Фильтрация по типу рейса
        if flight_type == "direct":
            flights = [f for f in flights if f.get("transfers", 999) == 0]
        elif flight_type == "transfer":
            flights = [f for f in flights if f.get("transfers", 0) > 0]
        
        flights = [f for f in flights if f.get("destination") == dest_iata]
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    return all_flights

async def search_destination_everywhere(
    origin_iata: str,
    depart_date: str,
    flight_type: str = "all"
) -> List[Dict]:
    destinations = GLOBAL_HUBS[:5]
    all_flights = []
    for dest in destinations:
        if dest == origin_iata:
            continue
        flights = await search_flights(
            origin_iata,
            dest,
            normalize_date(depart_date),
            None
        )
        # Фильтрация по типу рейса
        if flight_type == "direct":
            flights = [f for f in flights if f.get("transfers", 999) == 0]
        elif flight_type == "transfer":
            flights = [f for f in flights if f.get("transfers", 0) > 0]
        
        for f in flights:
            f["destination"] = dest
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    return all_flights

async def process_everywhere_search(
    callback: CallbackQuery,
    data: Dict[str, Any],
    all_flights: List[Dict],
    search_type: str
) -> bool:
    if not all_flights:
        return False
    
    cache_id = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    is_origin_everywhere = (search_type == "origin_everywhere")
    is_dest_everywhere = (search_type == "destination_everywhere")
    
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": data.get("dest_iata"),
        "is_roundtrip": False,
        "display_depart": display_depart,
        "display_return": None,
        "original_depart": data["depart_date"],
        "original_return": None,
        "passenger_desc": data["passenger_desc"],
        "passengers_code": data["passenger_code"],
        "origin_everywhere": is_origin_everywhere,
        "dest_everywhere": is_dest_everywhere,
        "flight_type": data.get("flight_type", "all")  # ← ДОБАВЛЕНО
    })
    
    cheapest_flight = min(all_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
    price = cheapest_flight.get("value") or cheapest_flight.get("price") or "?"
    origin_iata = cheapest_flight["origin"]
    dest_iata = cheapest_flight.get("destination")
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
    departure_time = cheapest_flight.get("departure_at", "").split('T')[1][:5] if cheapest_flight.get("departure_at") else "??:??"
    arrival_time = cheapest_flight.get("return_at", "").split('T')[1][:5] if cheapest_flight.get("return_at") else "??:??"
    
    if is_dest_everywhere:
        text = (
            f"✅ <b>Самый дешёвый вариант из города {data['origin_name']}</b>\n"
            f"📅 Вылет: {display_depart}\n"
            f"👥 Пассажиры: {data['passenger_desc']}\n"
            f"🛬 <b>{dest_name}</b>\n"
            f"💰 {price} ₽\n"
            f"⏰ {departure_time} → {arrival_time}\n"
        )
    else:
        text = (
            f"✅ <b>Самый дешёвый вариант в город {data['dest_name']}</b>\n"
            f"📅 Вылет: {display_depart}\n"
            f"👥 Пассажиры: {data['passenger_desc']}\n"
            f"🛫 <b>{origin_name}</b>\n"
            f"💰 {price} ₽\n"
            f"⏰ {departure_time} → {arrival_time}\n"
        )
    
    # === КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: обновляем пассажиров в ссылке из API ===
    booking_link = cheapest_flight.get("link") or cheapest_flight.get("deep_link")
    passengers_code = data.get("passengers_code", "1")
    
    if booking_link:
        # Обновляем количество пассажиров в ссылке от API
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        # Fallback на генерацию ссылки
        booking_link = generate_booking_link(
            flight=cheapest_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=data["depart_date"],
            passengers_code=passengers_code,
            return_date=None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    
    # Добавляем маркер к ссылке
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
    
    kb_buttons = []
    kb_buttons.append([
        InlineKeyboardButton(
            text=f"✈️ Забронировать {price} ₽",
            url=booking_link
        )
    ])
    
    # Кнопка "Все направления" только для "Город → Везде"
    if is_dest_everywhere:
        d1 = format_avia_link_date(data["depart_date"])
        map_link = f"https://www.aviasales.ru/map?params={data['origin_iata']}{d1}{passengers_code}"
        if marker:
            map_link = add_marker_to_url(map_link, marker, sub_id)
        kb_buttons.append([
            InlineKeyboardButton(
                text="🌍 Все направления",
                url=map_link
            )
        ])
    
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценами", callback_data=f"watch_all_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()
    return True

async def handle_everywhere_search_manual(
    message: Message,
    origin_city: str,
    dest_city: str,
    depart_date: str,
    return_date: str,
    passengers_code: str,
    is_origin_everywhere: bool,
    is_dest_everywhere: bool
) -> bool:
    
    orig_iata = None
    dest_iata = None
    
    if is_origin_everywhere:
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
    else:
        orig_iata = CITY_TO_IATA.get(origin_city.strip())
        if not orig_iata:
            await message.answer(f"Не знаю город вылета: {origin_city.strip()}", reply_markup=CANCEL_KB)
            return False
        origins = [orig_iata]
        origin_name = IATA_TO_CITY.get(orig_iata, origin_city.strip().capitalize())
    
    if is_dest_everywhere:
        destinations = GLOBAL_HUBS[:5]
        dest_name = "Везде"
    else:
        dest_iata = CITY_TO_IATA.get(dest_city.strip())
        if not dest_iata:
            await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return False
        destinations = [dest_iata]
        dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    
    passenger_desc = build_passenger_desc(passengers_code)
    display_depart = format_user_date(depart_date)
    
    await message.answer("Ищу билеты (включая с пересадками)...")
    all_flights = []
    
    for orig in origins:
        for dest in destinations:
            if orig == dest:
                continue
            flights = await search_flights(
                orig,
                dest,
                normalize_date(depart_date),
                None
            )
            if not is_dest_everywhere and dest == dest_iata:
                flights = [f for f in flights if f.get("destination") == dest]
            for f in flights:
                f["origin"] = orig
                f["destination"] = dest
            all_flights.extend(flights)
            await asyncio.sleep(0.5)
    
    if not all_flights:
        return False
    
    cache_id = str(uuid4())
    
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": False,
        "display_depart": display_depart,
        "display_return": None,
        "original_depart": depart_date,
        "original_return": None,
        "passenger_desc": passenger_desc,
        "passengers_code": passengers_code,
        "origin_everywhere": is_origin_everywhere,
        "dest_everywhere": is_dest_everywhere,
        "flight_type": "all"  # ← ДОБАВЛЕНО
    })
    
    cheapest_flight = min(all_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
    price = cheapest_flight.get("value") or cheapest_flight.get("price") or "?"
    origin_iata = cheapest_flight["origin"]
    dest_iata = cheapest_flight.get("destination")
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
    departure_time = cheapest_flight.get("departure_at", "").split('T')[1][:5] if cheapest_flight.get("departure_at") else "??:??"
    arrival_time = cheapest_flight.get("return_at", "").split('T')[1][:5] if cheapest_flight.get("return_at") else "??:??"
    
    if is_dest_everywhere:
        text = (
            f"✅ <b>Самый дешёвый вариант из города {origin_name}</b>\n"
            f"📅 Вылет: {display_depart}\n"
            f"👥 Пассажиры: {passenger_desc}\n"
            f"🛬 <b>{dest_name}</b>\n"
            f"💰 {price} ₽\n"
            f"⏰ {departure_time} → {arrival_time}\n"
        )
    else:
        text = (
            f"✅ <b>Самый дешёвый вариант в город {dest_name}</b>\n"
            f"📅 Вылет: {display_depart}\n"
            f"👥 Пассажиры: {passenger_desc}\n"
            f"🛫 <b>{origin_name}</b>\n"
            f"💰 {price} ₽\n"
            f"⏰ {departure_time} → {arrival_time}\n"
        )
    
    # === КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: обновляем пассажиров в ссылке из API ===
    booking_link = cheapest_flight.get("link") or cheapest_flight.get("deep_link")
    
    if booking_link:
        # Обновляем количество пассажиров в ссылке от API
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        # Fallback на генерацию ссылки
        booking_link = generate_booking_link(
            flight=cheapest_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_date,
            passengers_code=passengers_code,
            return_date=None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    
    # Добавляем маркер к ссылке
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
    
    kb_buttons = []
    kb_buttons.append([
        InlineKeyboardButton(
            text=f"✈️ Забронировать {price} ₽",
            url=booking_link
        )
    ])
    
    # Кнопка "Все направления" только для "Город → Везде"
    if is_dest_everywhere:
        d1 = format_avia_link_date(depart_date)
        map_link = f"https://www.aviasales.ru/map?params={origins[0]}{d1}{passengers_code}"
        if marker:
            map_link = add_marker_to_url(map_link, marker, sub_id)
        kb_buttons.append([
            InlineKeyboardButton(
                text="🌍 Все направления",
                url=map_link
            )
        ])
    
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценами", callback_data=f"watch_all_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    return True