# handlers/flight_wizard.py
"""
Пошаговый мастер поиска авиабилетов через кнопки
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timedelta
from typing import Optional
import re
from services.flight_search import search_flights, generate_booking_link, normalize_date
from utils.cities import CITY_TO_IATA, IATA_TO_CITY
from utils.redis_client import redis_client
from uuid import uuid4

router = Router()

class FlightSearch(StatesGroup):
    origin_city = State()      # Город отправления
    dest_city = State()        # Город прибытия
    depart_date = State()      # Дата вылета
    is_roundtrip = State()     # Нужен ли обратный билет
    return_date = State()      # Дата возврата (если нужен)
    adults = State()           # Количество взрослых
    children = State()         # Количество детей
    infants = State()          # Количество младенцев

# === Вспомогательные функции ===
def generate_date_buttons(prefix: str, start_date: Optional[datetime] = None) -> InlineKeyboardMarkup:
    """Генерирует кнопки с популярными датами"""
    if not start_date:
        start_date = datetime.now()
    
    buttons = []
    labels = ["Завтра", "Через 3 дня", "Через неделю", "Через 2 недели", "Через месяц"]
    deltas = [1, 3, 7, 14, 30]
    
    row = []
    for label, delta in zip(labels, deltas):
        date = start_date + timedelta(days=delta)
        date_str = f"{date.day}.{date.month}"
        row.append(InlineKeyboardButton(
            text=label,
            callback_data=f"{prefix}_{date_str}"
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    buttons.append([InlineKeyboardButton(text="✏️ Ввести дату вручную", callback_data=f"{prefix}_manual")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def generate_passenger_buttons(prefix: str, current: int = 1, max_count: int = 9) -> InlineKeyboardMarkup:
    """Генерирует кнопки выбора количества пассажиров"""
    buttons = []
    row = []
    for i in range(1, max_count + 1):
        row.append(InlineKeyboardButton(
            text=str(i),
            callback_data=f"{prefix}_{i}"
        ))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# === Обработчики шагов ===
@router.callback_query(F.data == "start_search")
async def start_search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FlightSearch.origin_city)
    await callback.message.answer(
        "📍 <b>Шаг 1 из 7</b>\n\n"
        "✈️ Из какого города летим?\n"
        "Напишите название города (например: Москва, Пекин, Стамбул)",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(FlightSearch.origin_city)
async def process_origin_city(message: Message, state: FSMContext):
    city = message.text.strip().lower()
    iata = CITY_TO_IATA.get(city)
    
    if not iata:
        await message.answer(
            f"❌ Не знаю город «{city}».\n"
            "Попробуйте написать по-другому или выберите из популярных:\n"
            "Москва, Пекин, Стамбул, Дубай, Бангкок, Сочи, Пхукет"
        )
        return
    
    await state.update_data(origin_city=city, origin_iata=iata)
    await state.set_state(FlightSearch.dest_city)
    
    await message.answer(
        "📍 <b>Шаг 2 из 7</b>\n\n"
        f"🛫 Вылет из: <b>{IATA_TO_CITY.get(iata, city).title()}</b>\n"
        "🛬 В какой город летим?",
        parse_mode="HTML"
    )

@router.message(FlightSearch.dest_city)
async def process_dest_city(message: Message, state: FSMContext):
    city = message.text.strip().lower()
    iata = CITY_TO_IATA.get(city)
    
    if not iata:
        await message.answer(
            f"❌ Не знаю город «{city}».\n"
            "Попробуйте написать по-другому или выберите из популярных:\n"
            "Сочи, Пхукет, Дубай, Бангкок, Стамбул, Пекин, Москва"
        )
        return
    
    data = await state.get_data()
    if data.get("origin_iata") == iata:
        await message.answer("❌ Город отправления и прибытия не могут совпадать. Выберите другой город:")
        return
    
    await state.update_data(dest_city=city, dest_iata=iata)
    await state.set_state(FlightSearch.depart_date)
    
    # Предлагаем популярные даты
    kb = generate_date_buttons("depart")
    await message.answer(
        "📍 <b>Шаг 3 из 7</b>\n\n"
        f"🛫 {IATA_TO_CITY.get(data['origin_iata'], data['origin_city']).title()} → "
        f"{IATA_TO_CITY.get(iata, city).title()}\n\n"
        "📅 Когда летим? Выберите дату вылета:",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("depart_"))
async def process_depart_date(callback: CallbackQuery, state: FSMContext):
    data = callback.data.split("_", 1)[1]
    
    if data == "manual":
        await callback.message.answer("✏️ Введите дату вылета в формате ДД.ММ (например: 15.03)")
        await callback.answer()
        return
    
    # Валидация даты
    if not re.match(r"^\d{1,2}\.\d{1,2}$", data):
        await callback.answer("❌ Неверный формат даты", show_alert=True)
        return
    
    await state.update_data(depart_date=data)
    await state.set_state(FlightSearch.is_roundtrip)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="roundtrip_yes")],
        [InlineKeyboardButton(text="❌ Нет, только туда", callback_data="roundtrip_no")]
    ])
    
    await callback.message.edit_text(
        "📍 <b>Шаг 4 из 7</b>\n\n"
        f"🛫 Вылет: {data}\n"
        "↩️ Нужен ли обратный билет?",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(FlightSearch.depart_date)
async def process_depart_date_manual(message: Message, state: FSMContext):
    date_str = message.text.strip()
    
    if not re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
        await message.answer("❌ Неверный формат. Пример: 15.03\nВведите дату снова:")
        return
    
    await state.update_data(depart_date=date_str)
    await state.set_state(FlightSearch.is_roundtrip)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="roundtrip_yes")],
        [InlineKeyboardButton(text="❌ Нет, только туда", callback_data="roundtrip_no")]
    ])
    
    await message.answer(
        "📍 <b>Шаг 4 из 7</b>\n\n"
        f"🛫 Вылет: {date_str}\n"
        "↩️ Нужен ли обратный билет?",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data == "roundtrip_yes")
async def process_roundtrip_yes(callback: CallbackQuery, state: FSMContext):
    await state.update_data(is_roundtrip=True)
    await state.set_state(FlightSearch.return_date)
    
    # Предлагаем даты возврата (минимум +3 дня от вылета)
    data = await state.get_data()
    depart_day, depart_month = map(int, data["depart_date"].split("."))
    start_date = datetime(datetime.now().year, depart_month, depart_day) + timedelta(days=3)
    
    kb = generate_date_buttons("return", start_date=start_date)
    
    await callback.message.edit_text(
        "📍 <b>Шаг 5 из 7</b>\n\n"
        f"🛫 Вылет: {data['depart_date']}\n"
        "📅 Когда возвращаемся? (минимум через 3 дня)",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "roundtrip_no")
async def process_roundtrip_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(is_roundtrip=False, return_date=None)
    await state.set_state(FlightSearch.adults)
    
    kb = generate_passenger_buttons("adults")
    await callback.message.edit_text(
        "📍 <b>Шаг 5 из 7</b>\n\n"
        "👤 Сколько взрослых летит? (от 18 лет)",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("return_"))
async def process_return_date(callback: CallbackQuery, state: FSMContext):
    data = callback.data.split("_", 1)[1]
    
    if data == "manual":
        await callback.message.answer("✏️ Введите дату возврата в формате ДД.ММ (например: 20.03)")
        await callback.answer()
        return
    
    if not re.match(r"^\d{1,2}\.\d{1,2}$", data):
        await callback.answer("❌ Неверный формат даты", show_alert=True)
        return
    
    # Проверка: дата возврата должна быть позже вылета
    state_data = await state.get_data()
    depart_day, depart_month = map(int, state_data["depart_date"].split("."))
    return_day, return_month = map(int, data.split("."))
    
    if (return_month < depart_month) or (return_month == depart_month and return_day <= depart_day):
        await callback.answer("❌ Дата возврата должна быть позже даты вылета", show_alert=True)
        return
    
    await state.update_data(return_date=data)
    await state.set_state(FlightSearch.adults)
    
    kb = generate_passenger_buttons("adults")
    await callback.message.edit_text(
        "📍 <b>Шаг 6 из 7</b>\n\n"
        f"🛫 Вылет: {state_data['depart_date']} → ↩️ Возврат: {data}\n"
        "👤 Сколько взрослых летит? (от 18 лет)",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(FlightSearch.return_date)
async def process_return_date_manual(message: Message, state: FSMContext):
    date_str = message.text.strip()
    
    if not re.match(r"^\d{1,2}\.\d{1,2}$", date_str):
        await message.answer("❌ Неверный формат. Пример: 20.03\nВведите дату снова:")
        return
    
    # Проверка даты
    state_data = await state.get_data()
    depart_day, depart_month = map(int, state_data["depart_date"].split("."))
    return_day, return_month = map(int, date_str.split("."))
    
    if (return_month < depart_month) or (return_month == depart_month and return_day <= depart_day):
        await message.answer("❌ Дата возврата должна быть позже даты вылета. Введите снова:")
        return
    
    await state.update_data(return_date=date_str)
    await state.set_state(FlightSearch.adults)
    
    kb = generate_passenger_buttons("adults")
    await message.answer(
        "📍 <b>Шаг 6 из 7</b>\n\n"
        f"🛫 Вылет: {state_data['depart_date']} → ↩️ Возврат: {date_str}\n"
        "👤 Сколько взрослых летит? (от 18 лет)",
        reply_markup=kb,
        parse_mode="HTML"
    )

@router.callback_query(F.data.startswith("adults_"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    count = int(callback.data.split("_")[1])
    await state.update_data(adults=count)
    
    # Если выбрано 9 взрослых — пропускаем детей/младенцев
    if count == 9:
        await state.update_data(children=0, infants=0)
        await finalize_search(callback.message, state)
    else:
        await state.set_state(FlightSearch.children)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👶 Нет детей", callback_data="children_0")],
            [InlineKeyboardButton(text="1 ребёнок", callback_data="children_1")],
            [InlineKeyboardButton(text="2 ребёнка", callback_data="children_2")],
            [InlineKeyboardButton(text="3 ребёнка", callback_data="children_3")],
        ])
        await callback.message.edit_text(
            "📍 <b>Шаг 7 из 7</b>\n\n"
            f"👤 Взрослых: {count}\n"
            "👶 Есть ли дети (2-12 лет)?",
            reply_markup=kb,
            parse_mode="HTML"
        )
    await callback.answer()

@router.callback_query(F.data.startswith("children_"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    count = int(callback.data.split("_")[1])
    await state.update_data(children=count)
    
    state_data = await state.get_data()
    total = state_data["adults"] + count
    
    if total >= 9:
        await state.update_data(infants=0)
        await finalize_search(callback.message, state)
    else:
        await state.set_state(FlightSearch.infants)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍼 Нет младенцев", callback_data="infants_0")],
            [InlineKeyboardButton(text="1 младенец", callback_data="infants_1")],
            [InlineKeyboardButton(text="2 младенца", callback_data="infants_2")],
        ])
        await callback.message.edit_text(
            "🍼 Есть ли младенцы (до 2 лет)?\n"
            f"Всего пассажиров сейчас: {total}",
            reply_markup=kb,
            parse_mode="HTML"
        )
    await callback.answer()

@router.callback_query(F.data.startswith("infants_"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    count = int(callback.data.split("_")[1])
    await state.update_data(infants=count)
    await finalize_search(callback.message, state)
    await callback.answer()

async def finalize_search(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    # Формируем данные для поиска
    origin_iata = data["origin_iata"]
    dest_iata = data["dest_iata"]
    depart_date = data["depart_date"]
    return_date = data.get("return_date")
    is_roundtrip = data.get("is_roundtrip", False)
    
    # Формируем код пассажиров (например: "21" = 2 взр + 1 реб)
    adults = data.get("adults", 1)
    children = data.get("children", 0)
    infants = data.get("infants", 0)
    passengers_code = str(adults) + (str(children) if children else "") + (str(infants) if infants else "")
    
    # Отображаем сводку
    summary = (
        f"🔍 <b>Поиск билетов</b>\n\n"
        f"🛫 {IATA_TO_CITY.get(origin_iata, origin_iata)} → "
        f"{IATA_TO_CITY.get(dest_iata, dest_iata)}\n"
        f"📅 Вылет: {depart_date}\n"
    )
    if is_roundtrip and return_date:
        summary += f"↩️ Возврат: {return_date}\n"
    summary += f"👤 Пассажиры: {adults} взр."
    if children: summary += f", {children} реб."
    if infants: summary += f", {infants} мл."
    
    await message.answer(summary, parse_mode="HTML")
    await message.answer("Ищу билеты (включая с пересадками)...")
    
    # Выполняем поиск (как в оригинальном коде)
    all_flights = []
    flights = await search_flights(
        origin_iata,
        dest_iata,
        normalize_date(depart_date),
        normalize_date(return_date) if return_date else None
    )
    for f in flights:
        f["origin"] = origin_iata
    all_flights.extend(flights)
    
    if not all_flights:
        origin_name = IATA_TO_CITY.get(origin_iata, origin_iata)
        dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
        await message.answer(
            f"Билеты не найдены 😢\n"
            f"Попробуйте другие даты или поискать на Aviasales напрямую:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔍 Посмотреть на Aviasales",
                    url=f"https://www.aviasales.ru/search/{origin_iata}{depart_date.replace('.','')}{dest_iata}1"
                )]
            ])
        )
        return
    
    # Сохраняем результаты в кэш (как в оригинальном коде)
    cache_id = str(uuid4())
    display_depart = f"{depart_date}.2026"  # Упрощённо для примера
    display_return = f"{return_date}.2026" if return_date else None
    
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": is_roundtrip,
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": depart_date,
        "original_return": return_date,
        "passenger_desc": f"{adults} взр." + (f", {children} реб." if children else "") + (f", {infants} мл." if infants else "")
    })
    
    # Показываем результаты
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Самое дешёвое", callback_data=f"show_top_{cache_id}")],
        [InlineKeyboardButton(text="📋 Все предложения", callback_data=f"show_all_{cache_id}")]
    ])
    await message.answer("Отлично! Билеты найдены:", reply_markup=kb)

# === Кнопка для запуска мастера из /start ===
def get_start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Начать поиск билетов", callback_data="start_search")]
    ])