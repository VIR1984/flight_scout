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

class FlightStates:
    """Состояния для пошагового мастера (без использования FSM для простоты)"""
    AWAITING_ORIGIN = "awaiting_origin"
    AWAITING_DEST = "awaiting_dest"
    AWAITING_DATE = "awaiting_date"
    AWAITING_RETURN_DATE = "awaiting_return_date"

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome = (
        "👋 Привет! Я — ваш личный помощник по поиску авиабилетов!\n\n"
        "Выберите удобный способ поиска:\n"
        "• ✈️ <b>Пошаговый поиск</b> — отвечайте кнопками, без сложных форматов\n"
        "• ℹ️ <b>Продвинутый ввод</b> — быстрый текстовый запрос для опытных пользователей"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Начать пошаговый поиск", callback_data="start_wizard")],
        [InlineKeyboardButton(text="ℹ️ Как писать запросы (справка)", callback_data="show_help")]
    ])
    await message.answer(welcome, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "show_help")
async def show_help(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    help_text = (
        "ℹ️ <b>Как писать запросы в текстовом формате</b>\n\n"
        "📌 Базовый формат:\n"
        "<code>Город - Город ДД.ММ</code>\n\n"
        "✅ Примеры:\n"
        "• <code>Москва - Сочи 10.03</code>\n"
        "• <code>Москва - Сочи 10.03 - 15.03</code> (туда-обратно)\n"
        "• <code>Москва - Бангкок 20.03 2 взр., 1 реб.</code>\n"
        "• <code>Везде - Стамбул 10.03</code> (поиск из всех городов)\n\n"
        "💡 Советы:\n"
        "• Города можно писать на русском или английском\n"
        "• Дата всегда в формате <b>ДД.ММ</b> (день.месяц)\n"
        "• Пробелы и дефисы между городами не важны:\n"
        "  <code>Москва-Сочи</code>, <code>Москва → Сочи</code>, <code>Москва Сочи</code> — всё работает!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Начать пошаговый поиск", callback_data="start_wizard")],
        [InlineKeyboardButton(text="↩️ Вернуться в меню", callback_data="back_to_start")]
    ])
    await callback.message.edit_text(help_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery, state: FSMContext):
    await cmd_start(callback.message, state)
    await callback.answer()

@router.callback_query(F.data == "start_wizard")
async def start_wizard(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FlightStates.AWAITING_ORIGIN)
    await callback.message.answer(
        "📍 <b>Шаг 1: Город отправления</b>\n\n"
        "Напишите город, из которого летите:\n"
        "• Москва\n• Пекин\n• Стамбул\n• Дубай\n• Бангкок\n"
        "(или любой другой город)",
        parse_mode="HTML"
    )
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

async def handle_flight_request(message: Message, origin_city, dest_city, depart_date, return_date=None, passengers_part=""):
    is_roundtrip = bool(return_date)
    dest_iata = CITY_TO_IATA.get(dest_city.strip())
    if not dest_iata:
        await message.answer(f"❌ Не знаю город прилёта: {dest_city.strip()}\nПопробуйте написать по-другому или нажмите /start для пошагового поиска")
        return

    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = ", ".join(build_passenger_desc(passengers_code))

    origin_clean = origin_city.strip()
    if origin_clean == "везде":
        origins = GLOBAL_HUBS[:5]
    else:
        orig_iata = CITY_TO_IATA.get(origin_clean)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город вылета: {origin_clean}\nПопробуйте написать по-другому или нажмите /start для пошагового поиска")
            return
        origins = [orig_iata]

    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None

    await message.answer("Ищу билеты (включая с пересадками)...")

    all_flights = []
    for i, orig in enumerate(origins):
        if i > 0:
            await asyncio.sleep(1)
        
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
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales (с пересадками)", url=link)]
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
        "passenger_desc": passenger_desc
    })

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Самое дешёвое", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все предложения", callback_data=f"show_all_{cache_id}")]
    ])
    await message.answer("Отлично! Билеты найдены:", reply_markup=kb)

# === Обработчики состояний ===
@router.message(F.text, state=FlightStates.AWAITING_ORIGIN)
async def process_origin(message: Message, state: FSMContext):
    city = message.text.strip().lower()
    iata = CITY_TO_IATA.get(city)
    if not iata:
        await message.answer(f"❌ Не знаю город «{city}». Попробуйте: Москва, Пекин, Стамбул, Дубай, Бангкок")
        return
    
    await state.update_data(origin_city=city, origin_iata=iata)
    await state.set_state(FlightStates.AWAITING_DEST)
    await message.answer(
        f"📍 <b>Шаг 2: Город прибытия</b>\n\n"
        f"🛫 Вылет из: <b>{IATA_TO_CITY.get(iata, city).title()}</b>\n"
        "Напишите город, куда летите:",
        parse_mode="HTML"
    )

@router.message(F.text, state=FlightStates.AWAITING_DEST)
async def process_dest(message: Message, state: FSMContext):
    city = message.text.strip().lower()
    iata = CITY_TO_IATA.get(city)
    data = await state.get_data()
    if not iata:
        await message.answer(f"❌ Не знаю город «{city}». Попробуйте: Сочи, Пхукет, Дубай, Бангкок")
        return
    if data.get("origin_iata") == iata:
        await message.answer("❌ Город отправления и прибытия не могут совпадать. Выберите другой город:")
        return
    
    await state.update_data(dest_city=city, dest_iata=iata)
    await state.set_state(FlightStates.AWAITING_DATE)
    await message.answer(
        f"📍 <b>Шаг 3: Дата вылета</b>\n\n"
        f"🛫 {IATA_TO_CITY.get(data['origin_iata'], data['origin_city']).title()} → "
        f"{IATA_TO_CITY.get(iata, city).title()}\n"
        "Напишите дату вылета в формате <b>ДД.ММ</b> (например: 15.03)",
        parse_mode="HTML"
    )

@router.message(F.text, state=FlightStates.AWAITING_DATE)
async def process_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    if not re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
        await message.answer("❌ Неверный формат даты. Пример: <b>15.03</b>", parse_mode="HTML")
        return
    
    await state.update_data(depart_date=date_str)
    await state.set_state("awaiting_roundtrip")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен обратный", callback_data="roundtrip_yes_simple")],
        [InlineKeyboardButton(text="❌ Нет, только туда", callback_data="roundtrip_no_simple")]
    ])
    await message.answer(
        f"📍 <b>Шаг 4: Обратный билет?</b>\n\n"
        f"🛫 Вылет: {date_str}\n"
        "Нужен ли обратный билет?",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "roundtrip_yes_simple", state="awaiting_roundtrip")
async def roundtrip_yes_simple(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FlightStates.AWAITING_RETURN_DATE)
    await callback.message.edit_text(
        "📍 <b>Шаг 5: Дата возврата</b>\n\n"
        "Напишите дату возврата в формате <b>ДД.ММ</b> (например: 20.03)",
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "roundtrip_no_simple", state="awaiting_roundtrip")
async def roundtrip_no_simple(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await handle_flight_request(
        callback.message,
        data['origin_city'],
        data['dest_city'],
        data['depart_date']
    )
    await callback.answer()

@router.message(F.text, state=FlightStates.AWAITING_RETURN_DATE)
async def process_return_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    if not re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
        await message.answer("❌ Неверный формат даты. Пример: <b>20.03</b>", parse_mode="HTML")
        return
    
    data = await state.get_data()
    await state.clear()
    await handle_flight_request(
        message,
        data['origin_city'],
        data['dest_city'],
        data['depart_date'],
        date_str
    )

# === Обработчики кнопок результатов ===
@router.callback_query(F.data.startswith("show_top_"))
async def show_top_offer(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cache_id = callback.data.split("_")[-1]
    data = await redis_client.get_search_cache(cache_id)
    if not 
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

    SUPPORTED_TRANSFER_AIRPORTS = [
        "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
        "DPS", "MLE", "KIX", "CTS",
        "DXB", "AUH", "DOH",
        "AYT", "ADB", "BJV", "DLM",
        "PMI", "IBZ", "AGP",
        "RHO", "HER", "CFU", "JMK",
    ]

    show_transfer_button = data["dest_iata"] in SUPPORTED_TRANSFER_AIRPORTS

    if show_transfer_button:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        
        airport_names = {
            "BKK": "Бангкок", "HKT": "Пхукет", "CNX": "Чиангмай", "DPS": "Бали",
            "DXB": "Дубай", "AYT": "Анталия", "PMI": "Майорка", "RHO": "Родос",
            "MLE": "Мальдивы", "SGN": "Хошимин", "DAD": "Дананг", "CXR": "Нячанг",
            "USM": "Самуи", "REP": "Сиемреап", "PNH": "Пномпень", "KIX": "Осака",
            "CTS": "Саппоро", "AUH": "Абу-Даби", "DOH": "Доха", "ADB": "Измир",
            "BJV": "Бодрум", "DLM": "Даламан", "IBZ": "Ибица", "AGP": "Малага",
            "HER": "Ираклион", "CFU": "Корфу", "JMK": "Миконос"
        }
        airport_name = airport_names.get(data["dest_iata"], data["dest_iata"])
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✈️ Забронировать ({price} ₽)", url=link)],
            [InlineKeyboardButton(text=f"🚖 Трансфер до отеля в {airport_name}", url=transfer_link)],
            [InlineKeyboardButton(text="👀 Следить за ценой", 
                                 callback_data=f"watch_{cache_id}_{price}")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✈️ Забронировать ({price} ₽)", url=link)],
            [InlineKeyboardButton(text="👀 Следить за ценой", 
                                 callback_data=f"watch_{cache_id}_{price}")]
        ])

    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data.startswith("show_all_"))
async def show_all_offers(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    cache_id = callback.data.split("_")[-1]
    data = await redis_client.get_search_cache(cache_id)
    if not 
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
        f"• Цены указаны <i>за 1 взрослого</i> (без учета детей/младенцев)\n"
        f"🔗 <a href='{link}'>Перейти на Aviasales — просмотреть все доступные рейсы</a>\n"
        f"💡 Включая рейсы с пересадками!"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👀 Следить за ценой", 
                             callback_data=f"watch_all_{cache_id}")],
        [InlineKeyboardButton(text="✈️ Все предложения на Aviasales", url=link)]
    ])
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()

# === Отслеживание цен ===
@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split("_")
    
    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not 
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
            f"{'📅 Возврат: ' + data['display_return'] + chr(10) if data.get('display_return') else ''}"
            f"💰 Текущая цена: {price} ₽\n\n"
            f"📲 Пришлю уведомление, если цена упадёт! 📉"
        )
    
    else:
        cache_id = parts[1]
        price = int(parts[2])
        data = await redis_client.get_search_cache(cache_id)
        if not 
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
            f"{'📅 Возврат: ' + data['display_return'] + chr(10) if data.get('display_return') else ''}"
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

# === Обработчик текстовых сообщений (для продвинутого ввода) ===
@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    
    # Если пользователь в пошаговом мастере — не обрабатываем как текстовый запрос
    if current_state in [FlightStates.AWAITING_ORIGIN, FlightStates.AWAITING_DEST, FlightStates.AWAITING_DATE, FlightStates.AWAITING_RETURN_DATE]:
        return
    
    # Очищаем состояние перед обработкой текстового запроса
    await state.clear()
    
    # Обрабатываем как обычный текстовый запрос
    text = message.text.strip().lower()
    match = re.match(
        r"^([а-яёa-z\s]+?)\s*[-→>—\s]+\s*([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})(?:\s*[-–]\s*(\d{1,2}\.\d{1,2}))?\s*(.*)?$",
        text, re.IGNORECASE
    )
    if not match:
        await message.answer(
            "❌ Неверный формат запроса.\n"
            "Нажмите /start и выберите:\n"
            "• ✈️ Пошаговый поиск — для простого поиска кнопками\n"
            "• ℹ️ Справка — чтобы узнать правильный формат текстового запроса",
            parse_mode="HTML"
        )
        return

    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    await handle_flight_request(message, origin_city, dest_city, depart_date, return_date, passengers_part)