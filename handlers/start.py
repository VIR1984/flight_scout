import os
import uuid
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from aiogram import Router, F, Bot
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from services.flight_search import (
    search_flights, search_grouped_prices, generate_booking_link,
    normalize_date, format_avia_link_date, add_marker_to_url
)
from services.redis_client import redis_client
from utils.logger import logger
from config import IATA_TO_CITY, CITY_TO_IATA

router = Router()

# === FSM States ===
class FlightSearch(StatesGroup):
    origin = State()
    dest = State()
    depart_date = State()
    return_date = State()
    passengers = State()
    confirm = State()

# === Constants ===
SUPPORTED_AIRPORTS = {
    "MOW": "Москва", "LED": "Санкт-Петербург", "AER": "Сочи", "KZN": "Казань",
    "OVB": "Новосибирск", "ROV": "Ростов-на-Дону", "KUF": "Самара", "UFA": "Уфа",
    "CEK": "Челябинск", "TJM": "Тюмень", "KJA": "Красноярск", "OMS": "Омск",
    "BAX": "Барнаул", "KRR": "Краснодар", "GRV": "Грозный", "MCX": "Махачкала",
    "VOG": "Волгоград", "IST": "Стамбул", "DXB": "Дубай", "BKK": "Бангкок",
    "HKT": "Пхукет", "CNX": "Чиангмай", "DAD": "Дананг", "SGN": "Хошимин",
    "CXR": "Нячанг", "REP": "Сием-Реап", "PNH": "Пномпень", "DPS": "Бали",
    "MLE": "Мальдивы", "KIX": "Осака", "CTS": "Саппоро", "AUH": "Абу-Даби",
    "DOH": "Доха", "AYT": "Анталия", "ADB": "Измир", "BJV": "Бодрум",
    "DLM": "Даламан", "PMI": "Майорка", "IBZ": "Ибица", "AGP": "Малага",
    "RHO": "Родос", "HER": "Ираклион", "CFU": "Корфу", "JMK": "Санторини"
}

SUPPORTED_TRANSFER_AIRPORTS = [
    "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
    "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
    "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK"
]

# === Helper Functions ===
def format_user_date(date_str: str) -> str:
    """Преобразует ДД.ММ в 'ДД марта' для отображения пользователю"""
    try:
        day, month = map(int, date_str.split('.'))
        months = [
            "января", "февраля", "марта", "апреля", "мая", "июня",
            "июля", "августа", "сентября", "октября", "ноября", "декабря"
        ]
        return f"{day} {months[month - 1]}"
    except:
        return date_str

def parse_passengers(code: str) -> Tuple[int, int, int, str]:
    """
    Парсит код пассажиров: "211" → (2 взр, 1 реб, 1 мл)
    Возвращает: (взрослые, дети, младенцы, описание)
    """
    adults = 1
    children = 0
    infants = 0
    
    if not code or not code.isdigit():
        return 1, 0, 0, "1 взрослый"
    
    digits = list(code)
    adults = int(digits[0]) if len(digits) > 0 else 1
    children = int(digits[1]) if len(digits) > 1 else 0
    infants = int(digits[2]) if len(digits) > 2 else 0
    
    parts = [f"{adults} взр." if adults > 1 else "1 взр."]
    if children:
        parts.append(f"{children} реб." if children > 1 else "1 реб.")
    if infants:
        parts.append(f"{infants} мл." if infants > 1 else "1 мл.")
    
    desc = " + ".join(parts)
    return adults, children, infants, desc

# === Search "Everywhere" Functions ===
async def search_origin_everywhere(
    destination: str,
    dest_iata: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    passenger_desc: str,
    state: FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск из всех городов в указанный пункт назначения"""
    origins = [k for k, v in SUPPORTED_AIRPORTS.items() if k != dest_iata and k not in ["MOW", "LED"]]
    all_flights = []
    
    for orig in origins:
        result = await search_grouped_prices(
            orig,
            dest_iata,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None,
            passengers=passengers_code
        )
        
        if result and result.get("data"):
            for route in result["data"]:
                route["origin"] = orig
                route["destination"] = dest_iata
                all_flights.append(route)
        
        await asyncio.sleep(0.3)
    
    return all_flights, "origin_everywhere"

async def search_destination_everywhere(
    origin: str,
    origin_iata: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    passenger_desc: str,
    state: FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск из указанного пункта отправления во все города"""
    destinations = [k for k, v in SUPPORTED_AIRPORTS.items() if k != origin_iata and k not in ["MOW", "LED"]]
    all_flights = []
    
    for dest in destinations:
        result = await search_grouped_prices(
            origin_iata,
            dest,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None,
            passengers=passengers_code
        )
        
        if result and result.get("data"):
            for route in result["data"]:
                route["origin"] = origin_iata
                route["destination"] = dest
                all_flights.append(route)
        
        await asyncio.sleep(0.3)
    
    return all_flights, "destination_everywhere"

async def process_everywhere_search(
    callback: CallbackQuery,
    data: Dict,
    all_flights: List[Dict],
    search_type: str
) -> bool:
    """Обработка результатов поиска 'везде'"""
    if not all_flights:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "😔 К сожалению, билеты не найдены по вашему запросу.",
            reply_markup=kb
        )
        return False
    
    # Сортируем по цене
    all_flights.sort(key=lambda f: f.get("value") or f.get("price") or 999999999)
    top_flight = all_flights[0]
    
    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata = top_flight["origin"]
    dest_iata = top_flight["destination"]
    origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
    
    display_depart = format_user_date(data["depart_date"])
    
    text = (
        f"✅ <b>Самый дешёвый вариант на {display_depart} ({data['passenger_desc']}):</b>\n"
        f"🛫 <b>Рейс: {origin_name}</b> → <b>{dest_name}</b>\n"
        f"📅 Дата вылета: {display_depart}\n"
        f"\n💰 <b>Цена от:</b> {price} ₽\n"
        f"⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"
    )
    
    booking_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data["depart_date"],
        passengers_code=data.get("passenger_code", "1"),
        return_date=data["return_date"] if data.get("need_return") else None
    )
    
    if not booking_link.startswith(('http://', 'https://')):
        booking_link = f"https://www.aviasales.ru{booking_link}"
    
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✈️ Перейти к бронированию ({price} ₽)", url=booking_link)],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    return True

# === Handlers ===
@router.callback_query(F.data == "flight_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Из Москвы", callback_data="origin_MOW")],
        [InlineKeyboardButton(text="✈️ Из СПб", callback_data="origin_LED")],
        [InlineKeyboardButton(text="✈️ Из любого города", callback_data="origin_everywhere")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "📍 Откуда летим?",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.origin)
    await callback.answer()

@router.callback_query(FlightSearch.origin, F.data.startswith("origin_"))
async def set_origin(callback: CallbackQuery, state: FSMContext):
    origin_code = callback.data.split("_")[1]
    
    if origin_code == "everywhere":
        await state.update_data(origin="везде", origin_iata="MOW", origin_name="Любой город")
    else:
        origin_name = SUPPORTED_AIRPORTS.get(origin_code, origin_code)
        await state.update_data(origin=origin_code, origin_iata=origin_code, origin_name=origin_name)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇹🇭 Таиланд", callback_data="dest_TH")],
        [InlineKeyboardButton(text="🇹🇷 Турция", callback_data="dest_TR")],
        [InlineKeyboardButton(text="🇻🇳 Вьетнам", callback_data="dest_VN")],
        [InlineKeyboardButton(text="🇰🇭 Камбоджа", callback_data="dest_KH")],
        [InlineKeyboardButton(text="🇮🇩 Индонезия", callback_data="dest_ID")],
        [InlineKeyboardButton(text="🇯🇵 Япония", callback_data="dest_JP")],
        [InlineKeyboardButton(text="🇦🇪 ОАЭ", callback_data="dest_AE")],
        [InlineKeyboardButton(text="🇶🇦 Катар", callback_data="dest_QA")],
        [InlineKeyboardButton(text="✈️ В любой город", callback_data="dest_everywhere")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="flight_search")]
    ])
    await callback.message.edit_text(
        "📍 Куда летим?",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.dest)
    await callback.answer()

@router.callback_query(FlightSearch.dest, F.data.startswith("dest_"))
async def set_dest(callback: CallbackQuery, state: FSMContext):
    dest_code = callback.data.split("_")[1]
    
    dest_mapping = {
        "TH": ("BKK", "Бангкок"),
        "TR": ("IST", "Стамбул"),
        "VN": ("SGN", "Хошимин"),
        "KH": ("REP", "Сием-Реап"),
        "ID": ("DPS", "Бали"),
        "JP": ("KIX", "Осака"),
        "AE": ("DXB", "Дубай"),
        "QA": ("DOH", "Доха")
    }
    
    if dest_code == "everywhere":
        await state.update_data(dest="везде", dest_iata="BKK", dest_name="Любой город")
    elif dest_code in dest_mapping:
        iata, name = dest_mapping[dest_code]
        await state.update_data(dest=dest_code, dest_iata=iata, dest_name=name)
    else:
        await state.update_data(dest=dest_code, dest_iata=dest_code, dest_name=SUPPORTED_AIRPORTS.get(dest_code, dest_code))
    
    today = datetime.now()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data=f"depart_{today.strftime('%d.%m')}")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data=f"depart_{(today + timedelta(days=1)).strftime('%d.%m')}")],
        [InlineKeyboardButton(text="📅 Через 2 дня", callback_data=f"depart_{(today + timedelta(days=2)).strftime('%d.%m')}")],
        [InlineKeyboardButton(text="📅 Выбрать дату", callback_data="depart_custom")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="flight_search")]
    ])
    await callback.message.edit_text(
        "📅 Когда вылетаем? (формат ДД.ММ)",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.depart_date)
    await callback.answer()

@router.callback_query(FlightSearch.depart_date, F.data.startswith("depart_"))
async def set_depart_date(callback: CallbackQuery, state: FSMContext):
    if callback.data == "depart_custom":
        await callback.message.edit_text("✏️ Введите дату вылета в формате ДД.ММ (например, 15.03):")
        return
    
    date_str = callback.data.split("_")[1]
    await state.update_data(depart_date=date_str)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Без обратного", callback_data="return_none")],
        [InlineKeyboardButton(text="↩️ Через 3 дня", callback_data="return_3")],
        [InlineKeyboardButton(text="↩️ Через 7 дней", callback_data="return_7")],
        [InlineKeyboardButton(text="↩️ Через 14 дней", callback_data="return_14")],
        [InlineKeyboardButton(text="↩️ Выбрать дату", callback_data="return_custom")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="flight_search")]
    ])
    await callback.message.edit_text(
        "↩️ Нужен ли обратный билет?",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.return_date)
    await callback.answer()

@router.message(FlightSearch.depart_date)
async def handle_custom_depart_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text or len(text) != 5 or text[2] != '.' or not text.replace('.', '').isdigit():
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ (например, 15.03):")
        return
    
    day, month = map(int, text.split('.'))
    if day < 1 or day > 31 or month < 1 or month > 12:
        await message.answer("❌ Неверная дата. Введите корректную дату ДД.ММ:")
        return
    
    await state.update_data(depart_date=text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Без обратного", callback_data="return_none")],
        [InlineKeyboardButton(text="↩️ Через 3 дня", callback_data="return_3")],
        [InlineKeyboardButton(text="↩️ Через 7 дней", callback_data="return_7")],
        [InlineKeyboardButton(text="↩️ Через 14 дней", callback_data="return_14")],
        [InlineKeyboardButton(text="↩️ Выбрать дату", callback_data="return_custom")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="flight_search")]
    ])
    await message.answer(
        "↩️ Нужен ли обратный билет?",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.return_date)

@router.callback_query(FlightSearch.return_date, F.data.startswith("return_"))
async def set_return_date(callback: CallbackQuery, state: FSMContext):
    data = callback.data
    
    if data == "return_none":
        await state.update_data(need_return=False, return_date=None)
        await ask_passengers(callback, state)
        return
    
    if data == "return_custom":
        await callback.message.edit_text("✏️ Введите дату возврата в формате ДД.ММ (например, 22.03):")
        await state.set_state(FlightSearch.return_date)
        return
    
    days = int(data.split("_")[1])
    depart_date = (await state.get_data())["depart_date"]
    depart_dt = datetime.strptime(f"{depart_date}.2026", "%d.%m.%Y")
    return_dt = depart_dt + timedelta(days=days)
    return_date = return_dt.strftime("%d.%m")
    
    await state.update_data(need_return=True, return_date=return_date)
    await ask_passengers(callback, state)
    await callback.answer()

@router.message(FlightSearch.return_date)
async def handle_custom_return_date(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text or len(text) != 5 or text[2] != '.' or not text.replace('.', '').isdigit():
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ (например, 22.03):")
        return
    
    day, month = map(int, text.split('.'))
    if day < 1 or day > 31 or month < 1 or month > 12:
        await message.answer("❌ Неверная дата. Введите корректную дату ДД.ММ:")
        return
    
    await state.update_data(need_return=True, return_date=text)
    await ask_passengers(message, state)

async def ask_passengers(event: CallbackQuery | Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 1 взрослый", callback_data="pass_1")],
        [InlineKeyboardButton(text="👤👤 2 взрослых", callback_data="pass_2")],
        [InlineKeyboardButton(text="👤👤👶 2 взр. + 1 реб.", callback_data="pass_21")],
        [InlineKeyboardButton(text="👤👤👶🍼 2 взр. + 1 реб. + 1 мл.", callback_data="pass_211")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="pass_custom")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="flight_search")]
    ])
    
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(
            "👥 Сколько пассажиров?",
            reply_markup=kb
        )
        await event.answer()
    else:
        await event.answer(
            "👥 Сколько пассажиров?",
            reply_markup=kb
        )
    
    await state.set_state(FlightSearch.passengers)

@router.callback_query(FlightSearch.passengers, F.data.startswith("pass_"))
async def set_passengers(callback: CallbackQuery, state: FSMContext):
    if callback.data == "pass_custom":
        await callback.message.edit_text(
            "✏️ Введите код пассажиров:\n"
            "• 1 — 1 взрослый\n"
            "• 2 — 2 взрослых\n"
            "• 21 — 2 взр. + 1 ребёнок\n"
            "• 211 — 2 взр. + 1 реб. + 1 младенец\n\n"
            "Пример: 21"
        )
        return
    
    code = callback.data.split("_")[1]
    adults, children, infants, desc = parse_passengers(code)
    
    await state.update_data(
        passenger_code=code,
        passenger_desc=desc,
        adults=adults,
        children=children,
        infants=infants
    )
    
    data = await state.get_data()
    depart_display = format_user_date(data["depart_date"])
    return_display = format_user_date(data["return_date"]) if data.get("return_date") else None
    
    text = (
        "🔍 <b>Проверьте параметры поиска:</b>\n\n"
        f"🛫 Откуда: {data['origin_name']}\n"
        f"🛬 Куда: {data['dest_name']}\n"
        f"📅 Вылет: {depart_display}\n"
    )
    
    if return_display:
        text += f"↩️ Обратно: {return_display}\n"
    
    text += f"👥 Пассажиры: {desc}\n\n"
    text += "✅ Все верно?"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, искать!", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="change_passengers")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)
    await callback.answer()

@router.message(FlightSearch.passengers)
async def handle_custom_passengers(message: Message, state: FSMContext):
    code = message.text.strip()
    
    if not code.isdigit() or len(code) > 3:
        await message.answer(
            "❌ Неверный формат. Введите код из цифр (макс. 3):\n"
            "• 1 — 1 взрослый\n"
            "• 2 — 2 взрослых\n"
            "• 21 — 2 взр. + 1 ребёнок\n"
            "• 211 — 2 взр. + 1 реб. + 1 младенец\n\n"
            "Пример: 21"
        )
        return
    
    adults, children, infants, desc = parse_passengers(code)
    
    await state.update_data(
        passenger_code=code,
        passenger_desc=desc,
        adults=adults,
        children=children,
        infants=infants
    )
    
    data = await state.get_data()
    depart_display = format_user_date(data["depart_date"])
    return_display = format_user_date(data["return_date"]) if data.get("return_date") else None
    
    text = (
        "🔍 <b>Проверьте параметры поиска:</b>\n\n"
        f"🛫 Откуда: {data['origin_name']}\n"
        f"🛬 Куда: {data['dest_name']}\n"
        f"📅 Вылет: {depart_display}\n"
    )
    
    if return_display:
        text += f"↩️ Обратно: {return_display}\n"
    
    text += f"👥 Пассажиры: {desc}\n\n"
    text += "✅ Все верно?"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, искать!", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="change_passengers")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

@router.callback_query(FlightSearch.confirm, F.data == "change_passengers")
async def change_passengers(callback: CallbackQuery, state: FSMContext):
    await ask_passengers(callback, state)

# === ОСНОВНОЙ МЕТОД С ЗАМЕНОЙ НА search_grouped_prices ===
@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_text("⏳ Ищу билеты (включая с пересадками)...")
    
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    
    # === ЛОГИКА "ВЕЗДЕ" ===
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
    # ======================
    
    # === СТАНДАРТНЫЙ ПОИСК С ГРУППИРОВАННЫМИ ЦЕНАМИ ===
    origins = [data["origin_iata"]]
    destinations = [data["dest_iata"]]
    all_flights = []
    
    for orig in origins:
        for dest in destinations:
            if orig == dest:
                continue
            
            # ЗАМЕНА: используем search_grouped_prices вместо search_flights
            result = await search_grouped_prices(
                orig,
                dest,
                normalize_date(data["depart_date"]),
                normalize_date(data["return_date"]) if data.get("return_date") else None,
                passengers=data.get("passenger_code", "1")
            )
            
            if result and result.get("data"):
                for route in result["data"]:
                    route["origin"] = orig
                    route["destination"] = dest
                    all_flights.append(route)
            
            await asyncio.sleep(0.5)
    # ================================================
    
    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(data["depart_date"])
        d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
        route = f"{origin_iata}{d1}{destinations[0]}{d2}{data.get('passenger_code', '1')}"
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link = add_marker_to_url(link, marker, sub_id)
        
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
    
    # НАЙТИ САМЫЙ ДЕШЁВЫЙ РЕЙС НА ТОЧНО УКАЗАННЫЕ ДАТЫ
    def find_cheapest_flight_on_exact_date(
        flights: List[Dict],
        requested_depart_date: str,
        requested_return_date: Optional[str] = None
    ) -> Dict:
        exact_flights = []
        req_depart = normalize_date(requested_depart_date)
        req_return = normalize_date(requested_return_date) if requested_return_date else None
        
        for flight in flights:
            flight_depart = flight.get("departure_at", "")[:10]
            flight_return = flight.get("return_at", "")[:10] if flight.get("return_at") else None
            
            if flight_depart == req_depart:
                if req_return:
                    if flight_return and flight_return == req_return:
                        exact_flights.append(flight)
                else:
                    exact_flights.append(flight)
        
        if not exact_flights:
            return min(flights, key=lambda f: f.get("value") or f.get("price") or 999999999)
        
        return min(exact_flights, key=lambda f: f.get("value") or f.get("price") or 999999999)
    
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
    
    text += "\n⚠️ <i>Цена актуальна на момент поиска. Точная стоимость при бронировании может отличаться.</i>"
    
    # ГЕНЕРИРУЕМ ССЫЛКУ С ПРАВИЛЬНЫМ КОДОМ ПАССАЖИРОВ
    booking_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data["depart_date"],
        passengers_code=data.get("passenger_code", "1"),
        return_date=data["return_date"] if data.get("need_return") else None
    )
    
    if not booking_link.startswith(('http://', 'https://')):
        booking_link = f"https://www.aviasales.ru{booking_link}"
    
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    if marker:
        booking_link = add_marker_to_url(booking_link, marker, sub_id)
    
    kb_buttons = [
        [InlineKeyboardButton(text=f"✈️ Перейти к бронированию ({price} ₽)", url=booking_link)],
        [InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ]
    
    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(1, [
            InlineKeyboardButton(
                text=f"🚖 Трансфер в {dest_name}",
                url=transfer_link
            )
        ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

# === Меню ===
@router.callback_query(F.data == "main_menu")
async def main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Поиск авиабилетов", callback_data="flight_search")],
        [InlineKeyboardButton(text="ℹ️ О боте", callback_data="about")]
    ])
    await callback.message.edit_text(
        "👋 Добро пожаловать в бота для поиска авиабилетов!\n\n"
        "Выберите действие:",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "✈️ Бот для поиска дешёвых авиабилетов через Aviasales.\n\n"
        "💡 Возможности:\n"
        "• Поиск билетов из Москвы/СПб или любого города\n"
        "• Поиск в популярные направления (Таиланд, Турция и др.)\n"
        "• Поддержка детей и младенцев\n"
        "• Отслеживание цен на рейсы\n"
        "• Прямые ссылки на бронирование с вашим маркером",
        reply_markup=kb
    )
    await callback.answer()