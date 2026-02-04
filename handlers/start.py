# handlers/start.py
import os
import re
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from services.flight_search import search_flights, generate_booking_link, get_hot_offers, normalize_date
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from aiogram.filters import Command

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    is_first = await redis_client.is_first_time_user(user_id)
    welcome = (
        "👋 Привет! Я — бот для поиска авиабилетов.\n"
        "🔍 <b>Как я работаю:</b>\n"
        "1. Напишите мне маршрут (например): <code>Москва - Сочи 10.03</code>\n"
        "2. Можете указать пассажиров: <code>2 взр., 1 реб.</code>\n"
        "3. Получите список билетов и удобную ссылку для бронирования\n"
        "💡 Совет: используйте <code>Везде - Сочи 10.03</code>, чтобы найти самый дешёвый вылет из любого города.\n"
        "Или выберите тип поиска:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Только туда", callback_data="type_oneway")],
        [InlineKeyboardButton(text="🔁 Туда-обратно", callback_data="type_roundtrip")],
        [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_offers")]
    ])
    await message.answer(welcome, reply_markup=kb, parse_mode="HTML")

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

def build_passenger_desc(code: str):
    try:
        ad = int(code[0])
        ch = int(code[1]) if len(code) > 1 else 0
        inf = int(code[2]) if len(code) > 2 else 0
        parts = []
        if ad: parts.append(f"{ad} взр.")
        if ch: parts.append(f"{ch} реб.")
        if inf: parts.append(f"{inf} мл.")
        return parts or ["1 взр."]
    except:
        return ["1 взр."]

def format_user_date(date_str: str) -> str:
    try:
        d, m = map(int, date_str.split('.'))
        year = 2026
        if m < 2 or (m == 2 and d < 3): year = 2027
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

async def handle_flight_request(message: Message):
    text = message.text.strip().lower()
    match = re.match(
        r"^([а-яёa-z\s]+?)\s*[-→>—\s]+\s*([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})(?:\s*[-–]\s*(\d{1,2}\.\d{1,2}))?\s*(.*)?$",
        text, re.IGNORECASE
    )
    if not match:
        await message.answer("Неверный формат. Пример:\n<code>Орск - Пермь 10.03</code>", parse_mode="HTML")
        return

    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    is_roundtrip = bool(return_date)

    dest_iata = CITY_TO_IATA.get(dest_city.strip())
    if not dest_iata:
        await message.answer(f"Не знаю город прилёта: {dest_city.strip()}")
        return

    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = ", ".join(build_passenger_desc(passengers_code))

    origin_clean = origin_city.strip()
    if origin_clean == "везде":
        origins = GLOBAL_HUBS
    else:
        orig_iata = CITY_TO_IATA.get(origin_clean)
        if not orig_iata:
            await message.answer(f"Не знаю город вылета: {origin_clean}")
            return
        origins = [orig_iata]

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

    if not all_flights:
        origin_iata = origins[0]
        d1 = depart_date.replace('.', '')
        d2 = return_date.replace('.', '') if return_date else ''
        route = f"{origin_iata}{d1}{dest_iata}{d2}1"
        marker = os.getenv("TRAFFIC_SOURCE", "")
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link += f"?marker={marker}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales (с пересадками)", url=link)]
        ])
        await message.answer(
            "Билеты не найдены через API 😢\n"
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
        "passenger_desc": passenger_desc
    })

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Самое дешёвое", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все предложения", callback_data=f"show_all_{cache_id}")]
    ])
    await message.answer("Отлично! Билеты найдены:", reply_markup=kb)

# === ОБЯЗАТЕЛЬНЫЕ ОБРАБОТЧИКИ КНОПОК ===
@router.callback_query(F.data.startswith("show_top_"))
async def show_top_offer(callback: CallbackQuery):
    cache_id = callback.data.split("_")[-1]
    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return

    top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_name = IATA_TO_CITY.get(top_flight["origin"], top_flight["origin"])
    dest_name = IATA_TO_CITY.get(data["dest_iata"], data["dest_iata"])

    text = f"✅ Самое дешёвое ({data['passenger_desc']}):\n"
    text += f'✈️ {origin_name} → {dest_name} — {price} ₽ (за 1 взрослого) — {data["display_depart"]}\n'
    if data["is_roundtrip"] and data["display_return"]:
        text += f'   ↩️ Обратно: {data["display_return"]}\n'

    link = generate_booking_link(
        top_flight,
        top_flight["origin"],
        data["dest_iata"],
        data["original_depart"],
        "1",
        data["original_return"]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✈️ Посмотреть предложение ({price} ₽)", url=link)]
    ])
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("show_all_"))
async def show_all_offers(callback: CallbackQuery):
    cache_id = callback.data.split("_")[-1]
    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return

    flights = sorted(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
    if not flights:
        await callback.message.answer("Нет рейсов.")
        return

    min_price = flights[0].get("value") or flights[0].get("price") or "?"
    origin_iata = flights[0]["origin"]
    dest_iata = data["dest_iata"]
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
    depart_date_disp = data["display_depart"]
    return_date_disp = data["display_return"]

    d1 = data["original_depart"].replace('.', '')
    d2 = data["original_return"].replace('.', '') if data["original_return"] else ''
    route = f"{origin_iata}{d1}{dest_iata}{d2}1" if data["original_return"] else f"{origin_iata}{d1}{dest_iata}1"
    marker = os.getenv("TRAFFIC_SOURCE")
    link = f"https://www.aviasales.ru/search/{route}"
    if marker:
        link += f"?marker={marker}"

    text = (
        f"📋 Все предложения ({data['passenger_desc']}):\n"
        f"• Маршрут: <b>{origin_name} → {dest_name}</b>\n"
        f"• Стоимость от: <b>{min_price} ₽</b>\n"
        f"• Дата вылета: <b>{depart_date_disp}</b>\n"
    )
    if data["is_roundtrip"] and return_date_disp:
        text += f"• Дата возврата: <b>{return_date_disp}</b>\n"
    text += (
        f"• Цены указаны <i>за 1 взрослого</i> (без учета детей/младенцев)\n"
        f"🔗 <a href='{link}'>Перейти на Aviasales — просмотреть все доступные рейсы</a>\n"
        f"💡 Включая рейсы с пересадками!"
    )
    await callback.message.answer(text, parse_mode="HTML", disable_web_page_preview=True)
    await callback.answer()

# === Обработка обычных сообщений ===
@router.message(F.text)
async def handle_any_message(message: Message):
    user_id = message.from_user.id
    is_first = await redis_client.is_first_time_user(user_id)
    if is_first:
        await cmd_start(message)
    else:
        await handle_flight_request(message)