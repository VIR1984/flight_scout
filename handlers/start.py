# handlers/start.py
import os
import re
from datetime import datetime, timedelta
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from services.flight_search import (
    search_flights,
    search_cheapest_flights,
    generate_booking_link,
    get_hot_offers,
    normalize_date
)
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY, smart_parse_route
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
        "1. Напишите маршрут: <code>Москва - Сочи 10.03</code>\n"
        "2. Можно без даты: <code>Москва - Сочи</code> (найду самые дешёвые)\n"
        "3. Укажите пассажиров: <code>2 взр., 1 реб.</code>\n"
        "4. Или <code>везде - Сочи</code> — поиск из всех городов\n\n"
        "💡 Совет: используйте обычный дефис <code>-</code> между городами"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Только туда", callback_data="type_oneway")],
        [InlineKeyboardButton(text="🔁 Туда-обратно", callback_data="type_roundtrip")],
        [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_offers")],
        [InlineKeyboardButton(text="💰 Дешёвые билеты", callback_data="cheap_flights")]
    ])
    await message.answer(welcome, reply_markup=kb, parse_mode="HTML")

def parse_passengers(s: str) -> str:
    if not s or not s.strip():
        return "1"
    if s.isdigit():
        return s
    adults = children = infants = 0
    for part in s.split(","):
        part = part.strip().lower()
        n = int(re.search(r"\d+", part).group()) if re.search(r"\d+", part) else 1
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
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
        year = datetime.now().year
        if datetime.now().month == 2 and datetime.now().day == 4:
            year = 2026
        if m < datetime.now().month or (m == datetime.now().month and d < datetime.now().day):
            year += 1
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str

def format_transfers(transfers: int) -> str:
    if transfers == 0:
        return "✈️ Прямой"
    elif transfers == 1:
        return "🔄 1 пересадка"
    elif 2 <= transfers <= 4:
        return f"🔄 {transfers} пересадки"
    else:
        return f"🔄 {transfers} пересадок"

@router.callback_query(F.data == "type_oneway")
async def handle_oneway(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Отправьте запрос в формате:\n"
        "<code>Город вылета - Город прилёта ДД.ММ</code>\n\n"
        "Примеры:\n"
        "<code>Москва - Сочи 10.03</code>\n"
        "<code>Москва - Сочи 10.03 2 взр., 1 реб.</code>\n"
        "<code>везде - Сочи 10.03</code>",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "type_roundtrip")
async def handle_roundtrip(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Отправьте запрос в формате:\n"
        "<code>Город вылета - Город прилёта ДД.ММ - ДД.ММ</code>\n\n"
        "Примеры:\n"
        "<code>Москва - Сочи 10.03 - 17.03</code>\n"
        "<code>Москва - Сочи 10.03 - 17.03 2 взр.</code>\n"
        "<code>везде - Сочи 10.03 - 17.03</code>",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "cheap_flights")
async def handle_cheap_flights(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "✈️ <b>Поиск самых дешёвых билетов</b>\n\n"
        "Напишите маршрут <u>без даты</u>:\n"
        "<code>Город вылета - Город прилёта</code>\n\n"
        "Примеры:\n"
        "<code>Москва - Сочи</code>\n"
        "<code>Москва - Бангкок</code>\n"
        "<code>везде - Стамбул</code>",
        parse_mode="HTML"
    )

@router.callback_query(F.data == "hot_offers")
async def handle_hot_offers(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer("Ищу горячие предложения...")
    offers = await get_hot_offers(limit=15)
    valid_offers = []
    for item in offers:
        if not item.get("departure_at") or not item.get("value"):
            continue
        try:
            dep_dt = datetime.fromisoformat(item["departure_at"].replace("Z", "+00:00"))
            if dep_dt.date() >= datetime.now().date():
                valid_offers.append(item)
            if len(valid_offers) >= 7:
                break
        except:
            continue
    if not valid_offers:
        await callback.message.answer("Нет актуальных предложений 😢")
        return
    text = "🔥 Горячие предложения:\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for item in valid_offers:
        origin = IATA_TO_CITY.get(item["origin"], item["origin"])
        dest = IATA_TO_CITY.get(item["destination"], item["destination"])
        price = item["value"]
        try:
            dt = datetime.fromisoformat(item["departure_at"].replace("Z", "+00:00"))
            dep_ddmm = f"{dt.day:02d}.{dt.month:02d}"
        except:
            dep_ddmm = "??"
        text += f"• {origin} - {dest} — от {price} ₽ — {dep_ddmm}\n"
        mmdd = item["departure_at"][5:7] + item["departure_at"][8:10]
        link = f"https://www.aviasales.ru/search/{item['origin']}{mmdd}{item['destination']}1"
        btn_text = f"✈️ {origin}-{dest} ({price} ₽)"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=btn_text, url=link)])
    await callback.message.answer(text)
    await callback.message.answer("Выберите рейс:", reply_markup=keyboard)

async def handle_flight_request(message: Message):
    text = message.text.strip().lower()
    
    # Умный парсинг маршрута (обрабатывает опечатки, слитные города, разные разделители)
    route_info = smart_parse_route(text)
    
    if not route_info["success"]:
        await message.answer(route_info["error"], parse_mode="HTML")
        return
    
    origin_city = route_info["origin"]
    dest_city = route_info["dest"]
    depart_date = route_info["depart_date"]
    return_date = route_info["return_date"]
    passengers_part = route_info["passengers"]
    is_cheap_search = route_info["is_cheap_search"]
    
    # Поиск дешёвых билетов без даты
    if is_cheap_search:
        await handle_cheap_request(message, origin_city, dest_city)
        return
    
    dest_iata = CITY_TO_IATA.get(dest_city)
    if not dest_iata:
        await message.answer(f"❌ Не знаю город: <b>{dest_city}</b>\nПроверьте написание или выберите другой город.", parse_mode="HTML")
        return
    
    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = ", ".join(build_passenger_desc(passengers_code))
    
    if origin_city == "везде":
        origins = GLOBAL_HUBS
    else:
        orig_iata = CITY_TO_IATA.get(origin_city)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город: <b>{origin_city}</b>\nПроверьте написание или выберите другой город.", parse_mode="HTML")
            return
        origins = [orig_iata]
    
    display_depart = format_user_date(depart_date) if depart_date else "ближайшие даты"
    display_return = format_user_date(return_date) if return_date else None
    
    await message.answer("Ищу билеты...")
    all_flights = []
    for orig in origins:
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date) if depart_date else None,
            normalize_date(return_date) if return_date else None
        )
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
    
    if not all_flights:
        suggestion = "\n💡 Совет: попробуйте поиск без даты — напишите просто <code>Москва - Сочи</code>, и я найду самые дешёвые билеты на ближайшие даты." if depart_date else ""
        await message.answer(f"Билеты не найдены 😢{suggestion}", parse_mode="HTML")
        return
    
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": bool(return_date),
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": depart_date or "",
        "original_return": return_date or "",
        "passenger_desc": passenger_desc
    })
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Самое дешёвое", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все предложения", callback_data=f"show_all_{cache_id}")],
        [InlineKeyboardButton(text="🚫 Только прямые", callback_data=f"show_direct_{cache_id}")]
    ])
    await message.answer("✅ Отлично! Билеты найдены:", reply_markup=kb)

async def handle_cheap_request(message: Message, origin_city: str, dest_city: str):
    """Поиск самых дешёвых билетов на ближайшие даты"""
    dest_iata = CITY_TO_IATA.get(dest_city)
    if not dest_iata:
        await message.answer(f"❌ Не знаю город: <b>{dest_city}</b>", parse_mode="HTML")
        return
    
    if origin_city == "везде":
        origins = GLOBAL_HUBS
    else:
        orig_iata = CITY_TO_IATA.get(origin_city)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город: <b>{origin_city}</b>", parse_mode="HTML")
            return
        origins = [orig_iata]
    
    await message.answer("Ищу самые дешёвые билеты на ближайшие 30 дней...")
    
    all_flights = []
    for orig in origins:
        flights = await search_cheapest_flights(orig, dest_iata)
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
    
    if not all_flights:
        await message.answer(
            "Билеты не найдены 😢\n"
            "💡 Попробуйте:\n"
            "• Указать конкретную дату: <code>Москва - Сочи 15.03</code>\n"
            "• Выбрать другой город прибытия",
            parse_mode="HTML"
        )
        return
    
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": False,
        "display_depart": "ближайшие даты",
        "passenger_desc": "1 взр."
    })
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Самое дешёвое", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все предложения", callback_data=f"show_all_{cache_id}")],
        [InlineKeyboardButton(text="🚫 Только прямые", callback_data=f"show_direct_{cache_id}")]
    ])
    await message.answer("✅ Нашёл самые дешёвые билеты:", reply_markup=kb)

# === Обработчики кнопок ===
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
    transfers = top_flight.get("transfers", 0)
    transfer_text = format_transfers(transfers)
    
    text = f"✅ Самое дешёвое ({data['passenger_desc']}):\n"
    text += f'{transfer_text} — {price} ₽\n'
    text += f'✈️ {origin_name} - {dest_name}\n'
    
    if data.get("display_depart") and data["display_depart"] != "ближайшие даты":
        text += f'📅 Вылет: {data["display_depart"]}\n'
        if data.get("is_roundtrip") and data.get("display_return"):
            text += f'   ↩️ Обратно: {data["display_return"]}\n'
    else:
        try:
            dep_dt = datetime.fromisoformat(top_flight["departure_at"].replace("Z", "+00:00"))
            text += f'📅 Вылет: {dep_dt.day:02d}.{dep_dt.month:02d}.{dep_dt.year}\n'
        except:
            pass
    
    link = generate_booking_link(
        top_flight,
        top_flight["origin"],
        data["dest_iata"],
        data.get("original_depart", ""),
        "1",
        data.get("original_return", "")
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✈️ Забронировать ({price} ₽)", url=link)]
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
    
    text = (
        f"📋 Все предложения ({data['passenger_desc']}):\n"
        f"• Маршрут: <b>{origin_name} - {dest_name}</b>\n"
        f"• Стоимость от: <b>{min_price} ₽</b>\n"
    )
    
    if data.get("display_depart") and data["display_depart"] != "ближайшие даты":
        text += f"• Дата вылета: <b>{data['display_depart']}</b>\n"
        if data.get("is_roundtrip") and data.get("display_return"):
            text += f"• Дата возврата: <b>{data['display_return']}</b>\n"
    
    text += f"• Варианты: <b>{len(flights)}</b> рейсов (прямые и с пересадками)\n"
    text += "━━━━━━━━━━━━━━━━━━━━━\n"
    
    # Показываем топ-5 рейсов с информацией о пересадках
    for i, f in enumerate(flights[:5], 1):
        price = f.get("value") or f.get("price") or "?"
        transfers = f.get("transfers", 0)
        transfer_text = "✈️" if transfers == 0 else f"🔄×{transfers}"
        try:
            dep_dt = datetime.fromisoformat(f["departure_at"].replace("Z", "+00:00"))
            dep_time = f"{dep_dt.day:02d}.{dep_dt.month:02d}"
        except:
            dep_time = "??"
        text += f"{i}. {transfer_text} {price} ₽ — {dep_time}\n"
    
    if len(flights) > 5:
        text += f"\n... и ещё {len(flights) - 5} вариантов\n"
    
    text += "\n💡 <i>Aviasales показывает все доступные рейсы: прямые и с пересадками. Выбирайте удобный вариант!</i>"
    
    # Генерация ссылки на Aviasales
    d1 = data.get("original_depart", "").replace('.', '') if data.get("original_depart") else ""
    d2 = data.get("original_return", "").replace('.', '') if data.get("original_return") else ""
    route = f"{origin_iata}{d1}{dest_iata}{d2}1" if d2 else f"{origin_iata}{d1}{dest_iata}1"
    marker = os.getenv("TRAFFIC_SOURCE")
    link = f"https://www.aviasales.ru/search/{route}"
    if marker:
        link += f"?marker={marker}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Посмотреть все рейсы на Aviasales", url=link)],
        [InlineKeyboardButton(text="🚫 Показать только прямые", callback_data=f"show_direct_{cache_id}")]
    ])
    
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("show_direct_"))
async def show_direct_flights(callback: CallbackQuery):
    cache_id = callback.data.split("_")[-1]
    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return
    
    # Фильтруем только прямые рейсы (0 пересадок)
    direct_flights = [f for f in data["flights"] if f.get("transfers", 999) == 0]
    
    if not direct_flights:
        await callback.message.answer(
            "❌ Прямые рейсы не найдены.\n"
            "Попробуйте поискать рейсы с 1 пересадкой — часто они дешевле и не сильно дольше!",
            parse_mode="HTML"
        )
        return
    
    top_flight = min(direct_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_name = IATA_TO_CITY.get(top_flight["origin"], top_flight["origin"])
    dest_name = IATA_TO_CITY.get(data["dest_iata"], data["dest_iata"])
    
    text = f"✅ Прямой рейс ({data['passenger_desc']}):\n"
    text += f'✈️ {origin_name} - {dest_name} — {price} ₽\n'
    
    if data.get("display_depart") and data["display_depart"] != "ближайшие даты":
        text += f'📅 Вылет: {data["display_depart"]}\n'
        if data.get("is_roundtrip") and data.get("display_return"):
            text += f'   ↩️ Обратно: {data["display_return"]}\n'
    else:
        try:
            dep_dt = datetime.fromisoformat(top_flight["departure_at"].replace("Z", "+00:00"))
            text += f'📅 Вылет: {dep_dt.day:02d}.{dep_dt.month:02d}.{dep_dt.year}\n'
        except:
            pass
    
    link = generate_booking_link(
        top_flight,
        top_flight["origin"],
        data["dest_iata"],
        data.get("original_depart", ""),
        "1",
        data.get("original_return", "")
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✈️ Забронировать прямой рейс ({price} ₽)", url=link)]
    ])
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

# === Главный обработчик текста ===
@router.message(F.text)
async def handle_any_message(message: Message):
    user_id = message.from_user.id
    is_first = await redis_client.is_first_time_user(user_id)
    if is_first:
        welcome = (
            "👋 Привет! Я — бот для поиска авиабилетов.\n"
            "🔍 <b>Как я работаю:</b>\n"
            "1. Напишите маршрут: <code>Москва - Сочи 10.03</code>\n"
            "2. Можно без даты: <code>Москва - Сочи</code> (найду самые дешёвые)\n"
            "3. Укажите пассажиров: <code>2 взр., 1 реб.</code>\n"
            "4. Или <code>везде - Сочи</code> — поиск из всех городов\n\n"
            "💡 Используйте обычный дефис <code>-</code> между городами"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Только туда", callback_data="type_oneway")],
            [InlineKeyboardButton(text="🔁 Туда-обратно", callback_data="type_roundtrip")],
            [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_offers")],
            [InlineKeyboardButton(text="💰 Дешёвые билеты", callback_data="cheap_flights")]
        ])
        await message.answer(welcome, reply_markup=kb, parse_mode="HTML")
    else:
        await handle_flight_request(message)