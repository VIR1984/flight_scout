# handlers/start.py
import json
import asyncio
import os
import re
from uuid import uuid4
from typing import Dict, Any
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from services.flight_search import search_flights, generate_booking_link, normalize_date
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.redis_client import redis_client
from aiogram.filters import Command

router = Router()

# Храним контекст трансфера для каждого пользователя
transfer_context: Dict[int, Dict[str, Any]] = {}

@router.message(Command("start"))
async def cmd_start(message: Message):
    welcome = (
        "👋 Привет! Я — ваш личный помощник по поиску авиабилетов!\n"
        "✈️ <b>Как со мной работать:</b>\n"
        "📍 Просто напишите маршрут в формате:\n"
        "   <code>Город - Город ДД.ММ</code>\n"
        "📌 Примеры:\n"
        "• <code>Москва - Сочи 10.03</code>\n"
        "• <code>Москва - Сочи 10.03 - 15.03</code> (туда-обратно)\n"
        "• <code>Москва - Бангкок 20.03 2 взр., 1 реб.</code>\n"
        "• <code>Везде - Стамбул 10.03</code> — найду самый дешёвый вылет из любого города!\n"
        "🕒 Я сразу покажу актуальные цены и помогу перейти к бронированию.\n"
        "Удачи в путешествиях! 🌍✈️"
    )
    await message.answer(welcome, parse_mode="HTML")

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
        await message.answer("Неверный формат. Пример:\n<code>Орск - Пермь 10.03</code>", parse_mode="HTML")
        return

    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    is_roundtrip = bool(return_date)
    dest_iata = CITY_TO_IATA.get(dest_city.strip())
    if not dest_iata:
        await message.answer(f"Не знаю город прилёта: {dest_city.strip()}")
        return

    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = build_passenger_desc(passengers_code)
    origin_clean = origin_city.strip()

    # Определение пункта вылета
    if origin_clean == "везде":
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
    else:
        orig_iata = CITY_TO_IATA.get(origin_clean)
        if not orig_iata:
            await message.answer(f"Не знаю город вылета: {origin_clean}")
            return
        origins = [orig_iata]
        origin_name = IATA_TO_CITY.get(orig_iata, origin_clean.capitalize())

    dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None

    await message.answer("Ищу билеты (включая с пересадками)...")

    # Запросы к API с задержкой
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

    # Расчет минимальной цены
    min_price = min([f.get("value") or f.get("price") or 999999 for f in all_flights])
    total_flights = len(all_flights)

    # Формируем информативное сообщение
    text = (
        f"✅ <b>Билеты найдены!</b>\n"
        f"📍 <b>Маршрут:</b> {origin_name} → {dest_name}\n"
        f"📅 <b>Дата вылета:</b> {display_depart}\n"
    )
    if is_roundtrip and display_return:
        text += f"📅 <b>Дата возврата:</b> {display_return}\n"
    text += (
        f"👥 <b>Пассажиры:</b> {passenger_desc}\n\n"
        f"💰 <b>Самая низкая цена:</b> {min_price} ₽\n"
        f"📊 <b>Всего вариантов:</b> {total_flights}\n\n"
        f"Выберите, как хотите посмотреть билеты:"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✈️ Самый дешёвый ({min_price} ₽)",
                callback_data=f"show_top_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"📋 Все варианты ({total_flights})",
                callback_data=f"show_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="📉 Следить за ценой",
                callback_data=f"watch_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="↩️ В главное меню",
                callback_data="main_menu"
            )
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=kb)

# === Обработчики кнопок результатов ===
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

    # Проверяем, поддерживается ли трансфер для этого аэропорта
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
            [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_{cache_id}_{price}")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
        ])
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✈️ Забронировать ({price} ₽)", url=link)],
            [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_{cache_id}_{price}")],
            [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
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
        [InlineKeyboardButton(text="👀 Следить за ценой", callback_data=f"watch_all_{cache_id}")],
        [InlineKeyboardButton(text="✈️ Все предложения на Aviasales", url=link)],
        [InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")]
    ])

    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    await callback.answer()

# === Отслеживание цен ===
@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery):
    parts = callback.data.split("_")
    if parts[1] == "all":  # watch_all_{cache_id}
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        min_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        price = min_flight.get("value") or min_flight.get("price")
        origin = min_flight["origin"]
        dest = data["dest_iata"]
        depart_date = data["original_depart"]
        return_date = data["original_return"]
    else:  # watch_{cache_id}_{price}
        cache_id = parts[1]
        price = int(parts[2])
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        origin = top_flight["origin"]
        dest = data["dest_iata"]
        depart_date = data["original_depart"]
        return_date = data["original_return"]

    origin_name = IATA_TO_CITY.get(origin, origin)
    dest_name = IATA_TO_CITY.get(dest, dest)

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
    dest = data["dest_iata"]

    await redis_client.save_price_watch(
        user_id=callback.from_user.id,
        origin=origin,
        dest=dest,
        depart_date=data["original_depart"],
        return_date=data["original_return"],
        current_price=price,
        passengers="1",
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

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery):
    welcome = (
        "👋 Привет! Я найду вам дешёвые авиабилеты.\n\n"
        
        "📍 <b>Как пользоваться:</b>\n"
        "<code>Город - Город ДД.ММ</code>\n\n"
        
        "📌 <b>Примеры запросов:</b>\n"
        "• Москва - Сочи 10.03\n"
        "• Москва - Сочи 10.03 - 15.03\n"
        "• Москва - Бангкок 20.03 2 взр.\n"
        "• Везде - Стамбул 10.03\n\n"
        
        "💡 <b>Важноb>\n"
        "• Даты в формате ДД.ММ\n"
        "• Для обратного билета укажите 2 даты\n"
        "• Можно писать 'Везде' вместо города вылета\n\n"
        
        "⚡ Просто напишите запрос — и я покажу цены за секунды!"
    )
    await callback.message.edit_text(welcome, parse_mode="HTML")
    await callback.answer()

# === Трансферы ===
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
    await callback.message.edit_text("Хорошо! Если передумаете — просто выполните новый поиск билетов. ✈️", reply_markup=kb)
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
        f"🚖 <b>Варианты трансфера {depart_date}</b>\n"
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
            InlineKeyboardButton(text=f"🚖 Вариант {i}: {price} ₽", url=transfer_link)
        ])

    buttons.append([
        InlineKeyboardButton(text="↩️ В главное меню", callback_data="main_menu")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(message_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.message(F.text)
async def handle_any_message(message: Message):
    if message.text.startswith("/"):
        return
    await handle_flight_request(message)