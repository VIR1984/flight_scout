# handlers/everywhere_search.py
import json
import asyncio
import os
import re
from typing import Dict, Any, List, Tuple
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from services.flight_search import (
    search_flights,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    update_passengers_in_link,
    format_duration as format_duration_helper
)
from utils.cities_loader import get_iata, get_city_name, CITY_TO_IATA, IATA_TO_CITY, _normalize_name
from utils.cities import GLOBAL_HUBS
from utils.redis_client import redis_client
from utils.logger import logger
from utils.link_converter import convert_to_partner_link

# ← ДОБАВЛЕНО: CANCEL_KB для кнопок отмены
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
])

# ← ДОБАВЛЕНО: Router для этого модуля
router = Router()

def format_user_date(date_str: str) -> str:
    """Форматирует дату ДД.ММ в ДД.ММ.ГГГГ"""
    try:
        d, m = map(int, date_str.split('.'))
        year = 2026
        if m < 2 or (m == 2 and d < 8):
            year = 2027
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

def build_passenger_desc(code: str) -> str:
    """Форматирует код пассажиров в читаемое описание"""
    try:
        adults = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants = int(code[2]) if len(code) > 2 else 0
        parts = []
        if adults:
            parts.append(f"{adults} взр.")
        if children:
            parts.append(f"{children} реб.")
        if infants:
            parts.append(f"{infants} мл.")
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."

async def search_origin_everywhere(
    dest_iata: str,
    depart_date: str,
    flight_type: str = "all"
) -> List[Dict]:
    """Поиск рейсов из всех городов в указанный"""
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
    """Поиск рейсов из указанного города во все направления"""
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
    """Обработка результатов поиска 'Везде'"""
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
        "flight_type": data.get("flight_type", "all")
    })
    
    cheapest_flight = min(all_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
    price = cheapest_flight.get("value") or cheapest_flight.get("price") or "?"
    origin_iata = cheapest_flight["origin"]
    dest_iata = cheapest_flight.get("destination")
    origin_name = get_city_name(origin_iata) or IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest_iata)
    
    # Форматирование продолжительности полета
    duration_minutes = cheapest_flight.get("duration", 0)
    duration = format_duration_helper(duration_minutes)
    
    # Определение количества пересадок
    transfers = cheapest_flight.get("transfers", 0)
    if transfers == 0:
        transfer_text = "✈️ Прямой рейс"
    elif transfers == 1:
        transfer_text = "✈️ 1 пересадка"
    else:
        transfer_text = f"✈️ {transfers} пересадки"
    
    # Названия аэропортов
    AIRPORT_NAMES = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport = AIRPORT_NAMES.get(dest_iata, dest_iata)
    
    # Авиакомпания
    airline = cheapest_flight.get("airline", "")
    flight_number = cheapest_flight.get("flight_number", "")
    airline_display = ""
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
    
    # Расчет цены
    passengers_code = data.get("passenger_code", "1")
    try:
        num_adults = int(passengers_code[0]) if passengers_code and passengers_code[0].isdigit() else 1
    except (IndexError, ValueError):
        num_adults = 1
    
    price_per_passenger = int(float(price)) if price != "?" else 0
    estimated_total_price = price_per_passenger * num_adults if price != "?" else "?"
    
    # Формирование текста в зависимости от типа поиска
    if is_dest_everywhere:
        # Поиск из конкретного города во все направления
        header = f"✅ <b>Самый дешёвый вариант из {data['origin_name']}</b>"
        route_line = f"🛫 <b>{origin_name}</b> → <b>{dest_name}</b>"
        text = (
            f"{header}\n"
            f"{route_line}\n"
            f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
            f"📅 Дата вылета: {display_depart}\n"
            f"⏱️ Продолжительность полета: {duration}\n"
            f"{transfer_text}\n"
        )
        if airline_display:
            text += f"✈️ {flight_display}\n"
        
        # Цена
        if price != "?":
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        else:
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        
        text += f"\n👥 <b>Пассажиры:</b> {data['passenger_desc']}"
    else:
        # Поиск из всех городов в конкретное направление
        header = f"✅ <b>Самый дешёвый вариант в {data['dest_name']}</b>"
        route_line = f"🛫 <b>{origin_name}</b> → <b>{dest_name}</b>"
        text = (
            f"{header}\n"
            f"{route_line}\n"
            f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
            f"📅 Дата вылета: {display_depart}\n"
            f"⏱️ Продолжительность полета: {duration}\n"
            f"{transfer_text}\n"
        )
        if airline_display:
            text += f"✈️ {flight_display}\n"
        
        # Цена
        if price != "?":
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        else:
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        
        text += f"\n👥 <b>Пассажиры:</b> {data['passenger_desc']}"
    
    text += f"\n\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"
    
    # === ФОРМИРОВАНИЕ ССЫЛОК ===
    booking_link = cheapest_flight.get("link") or cheapest_flight.get("deep_link")
    passengers_code = data.get("passenger_code", "1")
    
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
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
    
    booking_link = await convert_to_partner_link(booking_link)
    
    # === КНОПКИ ===
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
        map_link = await convert_to_partner_link(map_link)
        kb_buttons.append([
            InlineKeyboardButton(text="🌍 Все направления", url=map_link)
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
    """Обработка ручного ввода с 'Везде'"""
    orig_iata = None
    dest_iata = None
    
    if is_origin_everywhere:
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
    else:
        # ← ИСПОЛЬЗУЕМ get_iata() + fallback
        orig_iata = get_iata(origin_city.strip()) or CITY_TO_IATA.get(_normalize_name(origin_city.strip()))
        if not orig_iata:
            await message.answer(f"Не знаю город вылета: {origin_city.strip()}", reply_markup=CANCEL_KB)
            return False
        origins = [orig_iata]
        # ← ИСПОЛЬЗУЕМ get_city_name() + fallback
        origin_name = get_city_name(orig_iata) or IATA_TO_CITY.get(orig_iata, origin_city.strip().capitalize())
    
    if is_dest_everywhere:
        destinations = GLOBAL_HUBS[:5]
        dest_name = "Везде"
    else:
        # ← ИСПОЛЬЗУЕМ get_iata() + fallback
        dest_iata = get_iata(dest_city.strip()) or CITY_TO_IATA.get(_normalize_name(dest_city.strip()))
        if not dest_iata:
            await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return False
        destinations = [dest_iata]
        # ← ИСПОЛЬЗУЕМ get_city_name() + fallback
        dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    
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
        "flight_type": "all"
    })
    
    cheapest_flight = min(all_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
    price = cheapest_flight.get("value") or cheapest_flight.get("price") or "?"
    origin_iata = cheapest_flight["origin"]
    dest_iata = cheapest_flight.get("destination")
    origin_name = get_city_name(origin_iata) or IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest_iata)
    
    # Форматирование продолжительности полета
    duration_minutes = cheapest_flight.get("duration", 0)
    duration = format_duration_helper(duration_minutes)
    
    # Определение количества пересадок
    transfers = cheapest_flight.get("transfers", 0)
    if transfers == 0:
        transfer_text = "✈️ Прямой рейс"
    elif transfers == 1:
        transfer_text = "✈️ 1 пересадка"
    else:
        transfer_text = f"✈️ {transfers} пересадки"
    
    # Названия аэропортов
    AIRPORT_NAMES = {
        "SVO": "Шереметьево", "DME": "Домодедово", "VKO": "Внуково", "ZIA": "Жуковский",
        "LED": "Пулково", "AER": "Адлер", "KZN": "Казань", "OVB": "Новосибирск",
        "ROV": "Ростов", "KUF": "Курумоч", "UFA": "Уфа", "CEK": "Челябинск",
        "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск", "BAX": "Барнаул",
        "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала", "VOG": "Волгоград"
    }
    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport = AIRPORT_NAMES.get(dest_iata, dest_iata)
    
    # Авиакомпания
    airline = cheapest_flight.get("airline", "")
    flight_number = cheapest_flight.get("flight_number", "")
    airline_display = ""
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
    
    # Расчет цены
    try:
        num_adults = int(passengers_code[0]) if passengers_code and passengers_code[0].isdigit() else 1
    except (IndexError, ValueError):
        num_adults = 1
    
    price_per_passenger = int(float(price)) if price != "?" else 0
    estimated_total_price = price_per_passenger * num_adults if price != "?" else "?"
    
    # Формирование текста в зависимости от типа поиска
    if is_dest_everywhere:
        header = f"✅ <b>Самый дешёвый вариант из {origin_name}</b>"
        route_line = f"🛫 <b>{origin_name}</b> → <b>{dest_name}</b>"
        text = (
            f"{header}\n"
            f"{route_line}\n"
            f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
            f"📅 Дата вылета: {display_depart}\n"
            f"⏱️ Продолжительность полета: {duration}\n"
            f"{transfer_text}\n"
        )
        if airline_display:
            text += f"✈️ {flight_display}\n"
        
        if price != "?":
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        else:
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        
        text += f"\n👥 <b>Пассажиры:</b> {passenger_desc}"
    else:
        header = f"✅ <b>Самый дешёвый вариант в {dest_name}</b>"
        route_line = f"🛫 <b>{origin_name}</b> → <b>{dest_name}</b>"
        text = (
            f"{header}\n"
            f"{route_line}\n"
            f"📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})\n"
            f"📅 Дата вылета: {display_depart}\n"
            f"⏱️ Продолжительность полета: {duration}\n"
            f"{transfer_text}\n"
        )
        if airline_display:
            text += f"✈️ {flight_display}\n"
        
        if price != "?":
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        else:
            text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
            if num_adults > 1:
                text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
        
        text += f"\n👥 <b>Пассажиры:</b> {passenger_desc}"
    
    text += f"\n\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"
    
    # === ОБНОВЛЕНИЕ ПАССАЖИРОВ В ССЫЛКЕ ===
    booking_link = cheapest_flight.get("link") or cheapest_flight.get("deep_link")
    
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
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
    
    booking_link = await convert_to_partner_link(booking_link)
    
    # === КНОПКИ ===
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
        map_link = await convert_to_partner_link(map_link)
        kb_buttons.append([
            InlineKeyboardButton(text="🌍 Все направления", url=map_link)
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