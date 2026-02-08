import json
import asyncio
import os
from typing import Dict, Any, List, Tuple
from aiogram import Router
from uuid import uuid4 
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ===== Вспомогательные функции =====
def add_marker_to_url(url: str, marker: str, sub_id: str = "telegram") -> str:
    """Добавляет маркер и sub_id к ссылке"""
    if not marker or not url:
        return url
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    query_params['marker'] = [marker]
    query_params['sub_id'] = [sub_id]
    new_query = urlencode(query_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def format_user_date(date_str: str) -> str:
    """Форматирует дату для отображения пользователю"""
    try:
        d, m = map(int, date_str.split('.'))
        year = 2026
        if m < 2 or (m == 2 and d < 8):
            year = 2027
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

def build_passenger_desc(code: str) -> str:
    """Форматирует код пассажиров в читаемый вид"""
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

# ===== Основная логика поиска "Везде" =====

async def search_origin_everywhere(
    destination: str,
    dest_iata: str,
    depart_date: str,
    return_date: str,
    passengers_code: str,
    passenger_desc: str,
    state: FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск "Везде → Город": из всех хабов в конкретный город"""
    origins = GLOBAL_HUBS[:5]
    all_flights = []
    
    for orig in origins:
        if orig == dest_iata:
            continue
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None
        )
        # Фильтрация по точному пункту назначения (Дубай вместо Шарджи)
        flights = [f for f in flights if f.get("destination") == dest_iata]
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    
    return all_flights, "origin_everywhere"

async def search_destination_everywhere(
    origin: str,
    origin_iata: str,
    depart_date: str,
    return_date: str,
    passengers_code: str,
    passenger_desc: str,
    state: FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск "Город → Везде": из конкретного города во все хабы"""
    destinations = GLOBAL_HUBS[:5]
    all_flights = []
    
    for dest in destinations:
        if dest == origin_iata:
            continue
        flights = await search_flights(
            origin_iata,
            dest,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None
        )
        for f in flights:
            f["destination"] = dest
        all_flights.extend(flights)
        await asyncio.sleep(0.5)
    
    return all_flights, "destination_everywhere"

async def process_everywhere_search(
    callback: CallbackQuery,
    data: Dict[str, Any],
    all_flights: List[Dict],
    search_type: str
) -> bool:
    """
    Обрабатывает результаты поиска "везде" и отправляет топ-3 направлений
    Возвращает True если поиск успешен, иначе False
    """
    if not all_flights:
        return False
    
    # Сохраняем в кэш
    cache_id = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    display_return = format_user_date(data["return_date"]) if data.get("return_date") else None
    
    is_origin_everywhere = (search_type == "origin_everywhere")
    is_dest_everywhere = (search_type == "destination_everywhere")
    
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": data.get("dest_iata"),
        "is_roundtrip": data.get("need_return", False),
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": data["depart_date"],
        "original_return": data["return_date"],
        "passenger_desc": data["passenger_desc"],
        "passengers_code": data["passenger_code"],
        "origin_everywhere": is_origin_everywhere,
        "dest_everywhere": is_dest_everywhere
    })
    
    # Формируем топ-3 направлений
    if is_dest_everywhere:
        # Город → Везде: собираем уникальные направления
        dest_prices = {}
        for flight in all_flights:
            dest_iata = flight.get("destination")
            price = flight.get("value") or flight.get("price") or 999999
            if dest_iata not in dest_prices or price < dest_prices[dest_iata]["price"]:
                dest_prices[dest_iata] = {
                    "price": price,
                    "flight": flight,
                    "origin": flight.get("origin")
                }
        top_items = sorted(dest_prices.items(), key=lambda x: x[1]["price"])[:3]
        origin_name = data["origin_name"]
        title = f"✅ <b>Топ-3 самых дешёвых направлений из {origin_name}</b>"
        
    elif is_origin_everywhere:
        # Везде → Город: собираем уникальные пункты отправления
        origin_prices = {}
        for flight in all_flights:
            orig_iata = flight.get("origin")
            price = flight.get("value") or flight.get("price") or 999999
            if orig_iata not in origin_prices or price < origin_prices[orig_iata]["price"]:
                origin_prices[orig_iata] = {
                    "price": price,
                    "flight": flight,
                    "destination": flight.get("destination")
                }
        top_items = sorted(origin_prices.items(), key=lambda x: x[1]["price"])[:3]
        dest_name = data["dest_name"]
        title = f"✅ <b>Топ-3 самых дешёвых вариантов в {dest_name}</b>"
    
    # Формируем сообщение
    text = (
        f"{title}\n"
        f"📅 Вылет: {display_depart}\n"
        f"👥 Пассажиры: {data['passenger_desc']}\n\n"
    )
    
    kb_buttons = []
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    for i, (item_iata, info) in enumerate(top_items, 1):
        item_name = IATA_TO_CITY.get(item_iata, item_iata)
        price = info["price"]
        flight = info["flight"]
        
        # Форматируем время
        departure_time = flight.get("departure_at", "").split('T')[1][:5] if flight.get("departure_at") else "??:??"
        arrival_time = flight.get("return_at", "").split('T')[1][:5] if flight.get("return_at") else "??:??"
        
        text += (
            f"{i}. <b>{item_name}</b>\n"
            f"   💰 {price} ₽\n"
            f"   ⏰ {departure_time} → {arrival_time}\n\n"
        )
        
        # Генерируем ссылку для бронирования
        booking_link = flight.get("link") or flight.get("deep_link")
        if not booking_link or booking_link.startswith('/'):
            booking_link = generate_booking_link(
                flight,
                info.get("origin") or origin_iata,
                info.get("destination") or dest_iata,
                data["depart_date"],
                data.get("passengers_code", "1"),
                data["return_date"]
            )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
        
        if marker:
            booking_link = add_marker_to_url(booking_link, marker, sub_id)
        
        kb_buttons.append([
            InlineKeyboardButton(
                text=f"✈️ {item_name} — {price} ₽",
                url=booking_link
            )
        ])
    
    # Добавляем кнопку "Все варианты на Aviasales"
    if is_dest_everywhere:
        all_variants_link = f"https://www.aviasales.ru/search/{data['origin_iata']}//{data['depart_date'].replace('.','')}"
        if data.get("return_date"):
            all_variants_link += f"{data['return_date'].replace('.','')}"
        all_variants_link += f"{data.get('passengers_code', '1')}"
    else:
        all_variants_link = f"https://www.aviasales.ru/search//{data['dest_iata']}{data['depart_date'].replace('.','')}"
        if data.get("return_date"):
            all_variants_link += f"{data['return_date'].replace('.','')}"
        all_variants_link += f"{data.get('passengers_code', '1')}"
    
    if marker:
        all_variants_link = add_marker_to_url(all_variants_link, marker, sub_id)
    
    kb_buttons.append([
        InlineKeyboardButton(
            text="🌍 Все направления на Aviasales",
            url=all_variants_link
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
    """Обработка ручного ввода для поиска "везде" """
    if is_origin_everywhere:
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
        origin_iata = None
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
        dest_iata = None
    else:
        dest_iata = CITY_TO_IATA.get(dest_city.strip())
        if not dest_iata:
            await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return False
        destinations = [dest_iata]
        dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    
    passenger_desc = build_passenger_desc(passengers_code)
    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None
    
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
                normalize_date(return_date) if return_date else None
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
    
    # Сохраняем в кэш
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": bool(return_date),
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": depart_date,
        "original_return": return_date,
        "passenger_desc": passenger_desc,
        "passengers_code": passengers_code,
        "origin_everywhere": is_origin_everywhere,
        "dest_everywhere": is_dest_everywhere
    })
    
    # Формируем топ-3
    if is_dest_everywhere:
        dest_prices = {}
        for flight in all_flights:
            dest_iata = flight.get("destination")
            price = flight.get("value") or flight.get("price") or 999999
            if dest_iata not in dest_prices or price < dest_prices[dest_iata]["price"]:
                dest_prices[dest_iata] = {
                    "price": price,
                    "flight": flight,
                    "origin": flight.get("origin")
                }
        top_items = sorted(dest_prices.items(), key=lambda x: x[1]["price"])[:3]
        title = f"✅ <b>Топ-3 самых дешёвых направлений из {origin_name}</b>"
    else:
        origin_prices = {}
        for flight in all_flights:
            orig_iata = flight.get("origin")
            price = flight.get("value") or flight.get("price") or 999999
            if orig_iata not in origin_prices or price < origin_prices[orig_iata]["price"]:
                origin_prices[orig_iata] = {
                    "price": price,
                    "flight": flight,
                    "destination": flight.get("destination")
                }
        top_items = sorted(origin_prices.items(), key=lambda x: x[1]["price"])[:3]
        title = f"✅ <b>Топ-3 самых дешёвых вариантов в {dest_name}</b>"
    
    text = (
        f"{title}\n"
        f"📅 Вылет: {display_depart}\n"
        f"👥 Пассажиры: {passenger_desc}\n\n"
    )
    
    kb_buttons = []
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    for i, (item_iata, info) in enumerate(top_items, 1):
        item_name = IATA_TO_CITY.get(item_iata, item_iata)
        price = info["price"]
        flight = info["flight"]
        
        departure_time = flight.get("departure_at", "").split('T')[1][:5] if flight.get("departure_at") else "??:??"
        arrival_time = flight.get("return_at", "").split('T')[1][:5] if flight.get("return_at") else "??:??"
        
        text += (
            f"{i}. <b>{item_name}</b>\n"
            f"   💰 {price} ₽\n"
            f"   ⏰ {departure_time} → {arrival_time}\n\n"
        )
        
        booking_link = flight.get("link") or flight.get("deep_link")
        if not booking_link or booking_link.startswith('/'):
            booking_link = generate_booking_link(
                flight,
                info.get("origin") or origins[0],
                info.get("destination") or destinations[0],
                depart_date,
                passengers_code,
                return_date
            )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
        
        if marker:
            booking_link = add_marker_to_url(booking_link, marker, sub_id)
        
        kb_buttons.append([
            InlineKeyboardButton(
                text=f"✈️ {item_name} — {price} ₽",
                url=booking_link
            )
        ])
    
    # Кнопка "Все варианты"
    if is_dest_everywhere:
        all_variants_link = f"https://www.aviasales.ru/search/{origins[0]}//{depart_date.replace('.','')}"
        if return_date:
            all_variants_link += f"{return_date.replace('.','')}"
        all_variants_link += passengers_code
    else:
        all_variants_link = f"https://www.aviasales.ru/search//{destinations[0]}{depart_date.replace('.','')}"
        if return_date:
            all_variants_link += f"{return_date.replace('.','')}"
        all_variants_link += passengers_code
    
    if marker:
        all_variants_link = add_marker_to_url(all_variants_link, marker, sub_id)
    
    kb_buttons.append([
        InlineKeyboardButton(
            text="🌍 Все направления на Aviasales",
            url=all_variants_link
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