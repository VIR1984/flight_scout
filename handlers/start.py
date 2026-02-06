# handlers/start.py
import asyncio
import os
import re
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from aiogram.filters import Command

router = Router()

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome = (
        "👋 Привет! Я — ваш личный помощник по поиску авиабилетов!\n\n"
        "Выберите удобный способ:\n"
        "• ✈️ <b>Быстрый поиск</b> — напишите запрос в формате:\n"
        "  <code>Город - Город ДД.ММ</code>\n\n"
        "• ℹ️ <b>Справка по формату</b> — как правильно писать запросы"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ℹ️ Показать справку", callback_data="show_help")],
        [InlineKeyboardButton(text="✈️ Примеры запросов", callback_data="show_examples")]
    ])
    await message.answer(welcome, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "show_help")
async def show_help(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    help_text = (
        "ℹ️ <b>Как писать запросы</b>\n\n"
        "📌 Базовый формат:\n"
        "<code>Город - Город ДД.ММ</code>\n\n"
        "✅ Примеры:\n"
        "• <code>Москва - Сочи 10.03</code>\n"
        "• <code>Москва - Сочи 10.03 - 15.03</code> (туда-обратно)\n"
        "• <code>Москва - Бангкок 20.03 2 взр.</code>\n"
        "• <code>Везде - Стамбул 10.03</code> (поиск из всех городов)\n\n"
        "💡 Советы:\n"
        "• Города: на русском или английском (Москва / Moscow)\n"
        "• Дата: всегда <b>ДД.ММ</b> (15.03 = 15 марта)\n"
        "• Разделители: дефис, стрелка или пробел работают одинаково"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_start")]
    ])
    await callback.message.edit_text(help_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "show_examples")
async def show_examples(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    examples = (
        "✈️ <b>Готовые примеры для копирования:</b>\n\n"
        "<code>Москва - Сочи 10.03</code>\n"
        "<code>Пекин - Мальдивы 15.03 - 25.03</code>\n"
        "<code>Везде - Дубай 20.03</code>\n"
        "<code>Санкт-Петербург - Пхукет 05.04 2 взр.</code>\n\n"
        "Просто скопируйте любой пример и отправьте боту!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_start")]
    ])
    await callback.message.edit_text(examples, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await cmd_start(callback.message, state)
    await callback.answer()

# === Вспомогательные функции ===
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
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."

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
        await message.answer(
            "❌ Неверный формат запроса.\n\n"
            "Нажмите /start → «ℹ️ Показать справку» чтобы узнать правильный формат.",
            parse_mode="HTML"
        )
        return

    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    is_roundtrip = bool(return_date)

    dest_iata = CITY_TO_IATA.get(dest_city.strip())
    if not dest_iata:
        await message.answer(f"❌ Не знаю город прилёта: {dest_city.strip()}\nПопробуйте написать по-другому.")
        return

    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = build_passenger_desc(passengers_code)

    origin_clean = origin_city.strip()
    if origin_clean == "везде":
        origins = GLOBAL_HUBS[:5]  # Ограничение до 5 хабов
    else:
        orig_iata = CITY_TO_IATA.get(origin_clean)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город вылета: {origin_clean}\nПопробуйте написать по-другому.")
            return
        origins = [orig_iata]

    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None

    await message.answer("Ищу билеты (включая с пересадками)...")

    all_flights = []
    for i, orig in enumerate(origins):
        if i > 0:
            await asyncio.sleep(1)  # Задержка между запросами
        
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
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link += f"?marker={marker}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales", url=link)]
        ])
        await message.answer(
            "Билеты не найдены 😢\n"
            "Попробуйте другие даты или поискать напрямую:",
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

    min_price = min([f.get("value") or f.get("price") or 999999 for f in all_flights])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✈️ Самый дешёвый ({min_price} ₽)", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все варианты", callback_data=f"show_all_{cache_id}")],
        [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_all_{cache_id}")]
    ])
    await message.answer("✅ Билеты найдены! Выберите действие:", reply_markup=kb)

# === Обработчики кнопок (без изменений) ===
@router.callback_query(F.data.startswith("show_top_"))
async def show_top_offer(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
    text += f'✈️ {origin_name} → {dest_name} — {price} ₽ — {data["display_depart"]}\n'
    if data["is_roundtrip"] and data["display_return"]:
        text += f'↩️ Обратно: {data["display_return"]}\n'

    link = generate_booking_link(
        top_flight,
        top_flight["origin"],
        data["dest_iata"],
        data["original_depart"],
        "1",
        data["original_return"]
    )

    # Кнопка трансфера для туристических аэропортов
    SUPPORTED_TRANSFER_AIRPORTS = [
        "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
        "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
        "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK"
    ]
    
    kb_buttons = [
        [InlineKeyboardButton(text=f"✈️ Забронировать ({price} ₽)", url=link)],
        [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_{cache_id}_{price}")]
    ]
    
    if data["dest_iata"] in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        airport_names = {
            "BKK": "Бангкок", "HKT": "Пхукет", "DPS": "Бали", "MLE": "Мальдивы",
            "DXB": "Дубай", "AYT": "Анталия", "PMI": "Майорка"
        }
        airport_name = airport_names.get(data["dest_iata"], data["dest_iata"])
        kb_buttons.insert(1, [
            InlineKeyboardButton(text=f"🚖 Трансфер в {airport_name}", url=transfer_link)
        ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("show_all_"))
async def show_all_offers(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    base_sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    link = f"https://www.aviasales.ru/search/{route}"
    if marker.isdigit():
        sub_id = f"{base_sub_id}_{callback.from_user.id}"
        link += f"?marker={marker}&sub_id={sub_id}"

    text = (
        f"📋 Все предложения ({data['passenger_desc']}):\n"
        f"• Маршрут: <b>{origin_name} → {dest_name}</b>\n"
        f"• Стоимость от: <b>{min_price} ₽</b>\n"
        f"• Дата вылета: <b>{depart_date_disp}</b>\n"
    )
    if data["is_roundtrip"] and return_date_disp:
        text += f"• Дата возврата: <b>{return_date_disp}</b>\n"
    text += (
        f"• Цены указаны <i>за 1 взрослого</i>\n"
        f"🔗 <a href='{link}'>Посмотреть все рейсы на Aviasales</a>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_all_{cache_id}")],
        [InlineKeyboardButton(text="✈️ Все предложения", url=link)]
    ])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()

# === Отслеживание цен (без изменений) ===
@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split("_")
    
    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        
        min_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        price = min_flight.get("value") or min_flight.get("price")
        
        await redis_client.save_price_watch(
            user_id=callback.from_user.id,
            origin=min_flight["origin"],
            dest=data["dest_iata"],
            depart_date=data["original_depart"],
            return_date=data["original_return"],
            current_price=price,
            passengers="1"
        )
        
        origin_name = IATA_TO_CITY.get(min_flight["origin"], min_flight["origin"])
        dest_name = IATA_TO_CITY.get(data["dest_iata"], data["dest_iata"])
        
        await callback.message.answer(
            f"✅ <b>Отлично! Я буду следить за ценами</b>\n\n"
            f"📍 Маршрут: {origin_name} → {dest_name}\n"
            f"📅 Вылет: {data['display_depart']}\n"
            f"💰 Текущая цена: {price} ₽\n\n"
            f"📲 Пришлю уведомление, если цена упадёт! 📉"
        )
    
    else:
        cache_id = parts[1]
        price = int(parts[2])
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        
        top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        
        await redis_client.save_price_watch(
            user_id=callback.from_user.id,
            origin=top_flight["origin"],
            dest=data["dest_iata"],
            depart_date=data["original_depart"],
            return_date=data["original_return"],
            current_price=price,
            passengers="1"
        )
        
        origin_name = IATA_TO_CITY.get(top_flight["origin"], top_flight["origin"])
        dest_name = IATA_TO_CITY.get(data["dest_iata"], data["dest_iata"])
        
        await callback.message.answer(
            f"✅ <b>Я слежу за ценами!</b>\n\n"
            f"📍 Маршрут: {origin_name} → {dest_name}\n"
            f"📅 Вылет: {data['display_depart']}\n"
            f"💰 Текущая цена: {price} ₽\n\n"
            f"📲 Пришлю уведомление, если цена упадёт 📉"
        )
    
    await callback.answer()

@router.callback_query(F.data.startswith("unwatch_"))
async def handle_unwatch(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    watch_key = callback.data.split("_", 1)[1]
    
    if str(callback.from_user.id) in watch_key:
        await redis_client.remove_watch(callback.from_user.id, watch_key)
        await callback.message.edit_text("✅ Больше не слежу за этим маршрутом")
    else:
        await callback.answer("Это не ваше отслеживание", show_alert=True)
    
    await callback.answer()

@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    # Если пользователь не в состоянии (не в мастере) — обрабатываем как текстовый запрос
    if current_state is None:
        await handle_flight_request(message)
    # Иначе игнорируем (ожидаем ответ на вопрос мастера)