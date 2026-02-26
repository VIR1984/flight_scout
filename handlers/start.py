import json
import asyncio
import os
import re
from uuid import uuid4
from typing import Dict, Any

# Семафор: не более 10 параллельных поисков одновременно (защита от перегрузки API)
_SEARCH_SEMAPHORE = asyncio.Semaphore(10)
from aiogram import Router, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from services.flight_search import search_flights, search_flights_realtime, generate_booking_link, normalize_date, format_avia_link_date, find_cheapest_flight_on_exact_date, update_passengers_in_link, format_passenger_desc
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities_loader import get_iata, get_city_name, CITY_TO_IATA, IATA_TO_CITY, _normalize_name
from utils.cities import GLOBAL_HUBS 
from utils.redis_client import redis_client
from handlers.everywhere_search import (
    search_origin_everywhere,
    search_destination_everywhere,
    process_everywhere_search,
    handle_everywhere_search_manual,
    format_user_date,
    build_passenger_desc
)
from utils.logger import logger
from utils.link_converter import convert_to_partner_link

router = Router()
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
])

class FlightSearch(StatesGroup):
    route = State()
    depart_date = State()
    need_return = State()
    return_date = State()
    flight_type = State()
    adults = State()
    has_children = State()
    children = State()
    infants = State()
    confirm = State()

def validate_route(text: str) -> tuple:
    text = text.strip().lower()
    if re.search(r'\s+[-→—>]+\s+', text):
        parts = re.split(r'\s+[-→—>]+\s+', text, maxsplit=1)
    elif any(sym in text for sym in ['→', '—', '>']):
        parts = re.split(r'[→—>]+', text, maxsplit=1)
    else:
        parts = text.split(maxsplit=1)
    
    if len(parts) < 2:
        return None, None
    
    origin = parts[0].strip()
    dest = parts[1].strip()
    origin = origin.replace("санкт петербург", "санкт-петербург")
    dest = dest.replace("ростов на дону", "ростов-на-дону")
    return origin, dest

def validate_date(date_str: str) -> bool:
    try:
        day, month = map(int, date_str.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except:
        return False

def build_choices_summary(data: dict) -> str:
    """Строит пошаговую сводку выбора пользователя в правильном порядке."""
    lines = []
    n = 1
    lines.append(f"{n}. Маршрут: {data.get('origin_name', '')} → {data.get('dest_name', '')}")
    n += 1
    depart_date = data.get('depart_date', '')
    depart_display = format_user_date(depart_date) if depart_date else ''
    lines.append(f"{n}. Дата вылета: {depart_display}")
    n += 1

    need_return = data.get("need_return")
    if need_return is not None:
        if need_return and data.get("return_date"):
            return_date = data["return_date"]
            return_display = format_user_date(return_date) if return_date else return_date
            lines.append(f"{n}. Обратный билет: {return_display}")
        elif need_return:
            lines.append(f"{n}. Обратный билет: да")
        else:
            lines.append(f"{n}. Обратный билет: нет")
        n += 1

    if "flight_type" in data:
        ft_map = {"direct": "прямые рейсы", "transfer": "рейсы с пересадками", "all": "все варианты"}
        lines.append(f"{n}. Тип рейса: {ft_map.get(data['flight_type'], 'все варианты')}")
        n += 1

    if "passenger_desc" in data or "adults" in data:
        pd = data.get("passenger_desc")
        if not pd:
            a, c, i = data.get("adults", 1), data.get("children", 0), data.get("infants", 0)
            pd = f"{a} взр." + (f", {c} дет." if c else "") + (f", {i} мл." if i else "")
        else:
            for abbr, repl in [(" взр", " взр."), (" дет", " дет."), (" мл", " мл.")]:
                if abbr in pd and f"{abbr}." not in pd:
                    pd = pd.replace(abbr, repl)
        lines.append(f"{n}. Пассажиры: {pd}")

    return "\n".join(lines)


def build_passenger_code(adults: int, children: int = 0, infants: int = 0) -> str:
    print(f"[DEBUG build_passenger_code] Вход: adults={adults}, children={children}, infants={infants}")
    adults = max(1, adults)
    total = adults + children + infants

    if total > 9:
        print(f"[DEBUG build_passenger_code] Всего пассажиров > 9 ({total}), корректирую...")
        remaining = 9 - adults
        if children + infants > remaining:
            old_children, old_infants = children, infants
            children = min(children, remaining)
            infants = max(0, remaining - children)
            print(f"[DEBUG build_passenger_code] Коррекция детей/младенцев: {old_children}/{old_infants} -> {children}/{infants}")
        if infants > adults:
            old_infants = infants
            infants = adults
            print(f"[DEBUG build_passenger_code] Коррекция младенцев: {old_infants} -> {infants} (не больше взрослых)")

    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)

    print(f"[DEBUG build_passenger_code] Выход: '{code}'")
    return code

@router.message(Command("start"))
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        # [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],  # ← НОВАЯ
        [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
    ])
    await message.answer(
        "👋 Привет! Я найду вам дешёвые авиабилеты.\n",
        reply_markup=kb
    )

@router.callback_query(F.data == "main_menu")
async def handle_main_menu(callback: CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        # [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
        [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
    ])
    try:
        await callback.message.edit_text(
            "👋 Привет! Я найду вам дешёвые авиабилеты.\n",
            reply_markup=kb
        )
    except Exception:
        await callback.message.answer(
            "👋 Привет! Я найду вам дешёвые авиабилеты.\n",
            reply_markup=kb
        )
    await callback.answer()
    
# hot_deals_menu обрабатывается в handlers/hot_deals.py

@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Начнём поиск билетов 👌\n\n"
        "<b>Напишите маршрут в формате: Город отправления - Город прибытия</b>\n\n"
        "<i>Пример: Москва - Сочи</i>\n\n"
        "Если ещё не решили, откуда или куда полетите, напишите слово «Везде».",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.route)
    _schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()

@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    origin, dest = validate_route(message.text)
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n"
            "<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return

    if origin != "везде":
        # ← ИСПОЛЬЗУЕМ get_iata() + fallback на старый словарь
        orig_iata = get_iata(origin) or CITY_TO_IATA.get(_normalize_name(origin))
        if not orig_iata:
            await message.answer(f"❌ Не знаю город отправления: {origin}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        # ← ИСПОЛЬЗУЕМ get_city_name() + fallback
        origin_name = get_city_name(orig_iata) or IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = None
        origin_name = "Везде"

    if dest != "везде":
        # ← ИСПОЛЬЗУЕМ get_iata() + fallback на старый словарь
        dest_iata = get_iata(dest) or CITY_TO_IATA.get(_normalize_name(dest))
        if not dest_iata:
            await message.answer(f"❌ Не знаю город прибытия: {dest}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        # ← ИСПОЛЬЗУЕМ get_city_name() + fallback
        dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest.capitalize())
    else:
        dest_iata = None
        dest_name = "Везде"

    if origin == "везде" and dest == "везде":
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return

    # ← ПРОВЕРКА: Одинаковые города
    if orig_iata and dest_iata and orig_iata == dest_iata:
        logger.info(f"[DEBUG VALIDATION] Ошибка: одинаковые города {orig_iata} == {dest_iata}")
        print(f"[DEBUG VALIDATION] Ошибка: одинаковые города {orig_iata} == {dest_iata}")
        await message.answer(
            "❌ Город вылета и прибытия не могут совпадать.\n"
            "Пожалуйста, выберите разные города.",
            reply_markup=CANCEL_KB
        )
        return

    await state.update_data(
        origin=origin,
        origin_iata=orig_iata,
        dest=dest,
        dest_iata=dest_iata,
        origin_name=origin_name,
        dest_name=dest_name
    )

    data = await state.get_data()
    _cancel_inactivity(message.chat.id)
    if data.get("_edit_mode"):
        # Точечное редактирование — возвращаем к summary без перехода по датам
        await state.update_data(_edit_mode=False)
        await show_summary(message, state)
        return

    await message.answer(
        "Введите дату вылета в формате <code>ДД.ММ</code>\n"
        "<i>Пример: 10.03</i>",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.depart_date)
    _schedule_inactivity(message.chat.id, message.from_user.id)

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "<i>Пример: 10.03</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    _cancel_inactivity(message.chat.id)
    await state.update_data(depart_date=message.text)
    data = await state.get_data()
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"

    if data.get("_edit_mode"):
        # В режиме редактирования: если есть обратный рейс — обновляем только дату вылета,
        # потом спрашиваем новую дату обратного (если нужно), иначе → summary
        if is_origin_everywhere or is_dest_everywhere:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
            return
        if data.get("need_return"):
            await message.answer(
                "✏️ Введите новую дату обратного рейса в формате <code>ДД.ММ</code>\n<i>Пример: 20.03</i>",
                parse_mode="HTML",
                reply_markup=CANCEL_KB
            )
            await state.set_state(FlightSearch.return_date)
        else:
            await state.update_data(_edit_mode=False)
            await show_summary(message, state)
        return

    if is_origin_everywhere or is_dest_everywhere:
        await state.update_data(need_return=False, return_date=None)
        await ask_flight_type(message, state)
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да, нужен"), KeyboardButton(text="Нет, спасибо")],
            [KeyboardButton(text="В начало")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Нужен ли обратный билет?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.need_return)


@router.message(FlightSearch.need_return, F.text.in_(["Да, нужен", "Нет, спасибо"]))
async def process_need_return(message: Message, state: FSMContext):
    need_return = message.text == "Да, нужен"
    await state.update_data(need_return=need_return)
    if need_return:
        await message.answer(
            "Введите дату возврата в формате <code>ДД.ММ</code>\n"
            "<i>Пример: 15.03</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
            ])
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await state.update_data(return_date=None)
        await ask_flight_type(message, state)


@router.message(FlightSearch.need_return, F.text == "В начало")
async def need_return_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "<i>Пример: 15.03</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return

    # ← ПРОВЕРКА: Дата возврата не раньше вылета
    data = await state.get_data()
    depart_date = data.get('depart_date')
    return_date = message.text
    norm_depart = normalize_date(depart_date)
    norm_return = normalize_date(return_date)
    
    logger.info(f"[DEBUG VALIDATION] Сравнение дат: Вылет {norm_depart} vs Возврат {norm_return}")
    print(f"[DEBUG VALIDATION] Сравнение дат: Вылет {norm_depart} vs Возврат {norm_return}")

    if norm_return <= norm_depart:
        await message.answer(
            "❌ Дата возврата не может быть раньше или равна дате вылета.\n"
            "Укажите правильную дату возврата.",
            reply_markup=CANCEL_KB
        )
        return

    _cancel_inactivity(message.chat.id)
    await state.update_data(return_date=message.text)

    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(message, state)
        return

    await ask_flight_type(message, state)

def _flight_type_text_to_code(text: str) -> str:
    if text == "Прямые":
        return "direct"
    if text == "С пересадкой":
        return "transfer"
    if text == "Все варианты":
        return "all"
    return "all"


async def ask_flight_type(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Прямые"), KeyboardButton(text="С пересадкой")],
            [KeyboardButton(text="Все варианты")],
            [KeyboardButton(text="В начало")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Какие рейсы показывать?", parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.flight_type)


@router.message(FlightSearch.flight_type, F.text.in_(["Прямые", "С пересадкой", "Все варианты"]))
async def process_flight_type(message: Message, state: FSMContext):
    _cancel_inactivity(message.chat.id)
    flight_type = _flight_type_text_to_code(message.text)
    await state.update_data(flight_type=flight_type)

    data = await state.get_data()
    if data.get("_edit_mode"):
        await state.update_data(_edit_mode=False)
        await show_summary(message, state)
        return

    await ask_adults(message, state)


@router.message(FlightSearch.flight_type, F.text == "В начало")
async def flight_type_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)

async def ask_adults(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="1"),
                KeyboardButton(text="2"),
                KeyboardButton(text="3"),
                KeyboardButton(text="4"),
            ],
            [
                KeyboardButton(text="5"),
                KeyboardButton(text="6"),
                KeyboardButton(text="7"),
                KeyboardButton(text="8"),
            ],
            [KeyboardButton(text="9"), KeyboardButton(text="В начало")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Сколько взрослых пассажиров (от 12 лет)?", parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.adults)


@router.message(FlightSearch.adults, F.text.regexp(r"^[1-9]$"))
async def process_adults(message: Message, state: FSMContext):
    _cancel_inactivity(message.chat.id)
    adults = int(message.text)
    await state.update_data(adults=adults)
    if adults == 9:
        await state.update_data(children=0, infants=0, passenger_desc="9 взр.", passenger_code="9")
        await show_summary(message, state)
    else:
        await ask_has_children(message, state)


@router.message(FlightSearch.adults, F.text == "В начало")
async def adults_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)


async def ask_has_children(message: Message, state: FSMContext):
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Да"), KeyboardButton(text="Нет")],
            [KeyboardButton(text="В начало")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("С вами летят дети?", parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.has_children)


@router.message(FlightSearch.has_children, F.text.in_(["Да", "Нет"]))
async def process_has_children(message: Message, state: FSMContext):
    has_children = message.text == "Да"
    if has_children:
        await ask_children(message, state)
    else:
        data = await state.get_data()
        adults = data["adults"]
        await state.update_data(children=0, infants=0)
        pd = f"{adults} взр."
        await state.update_data(passenger_desc=pd, passenger_code=str(adults))
        # _edit_mode сбросится внутри show_summary
        await show_summary(message, state)


@router.message(FlightSearch.has_children, F.text == "В начало")
async def has_children_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)


async def ask_children(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    max_children = 9 - adults
    row = [KeyboardButton(text=str(i)) for i in range(0, max_children + 1)]
    kb = ReplyKeyboardMarkup(
        keyboard=[row, [KeyboardButton(text="В начало")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Сколько детей (от 2 до 11 лет)?\n"
        "Если у вас младенцы, укажете дальше.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.children)


@router.message(FlightSearch.children, F.text.regexp(r"^\d+$"))
async def process_children(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    max_children = 9 - adults
    children = int(message.text)
    if children < 0 or children > max_children:
        return
    await state.update_data(children=children)
    remaining = 9 - adults - children
    if remaining == 0:
        await state.update_data(infants=0)
        pd = f"{adults} взр." + (f", {children} дет." if children else "")
        await state.update_data(passenger_desc=pd, passenger_code=build_passenger_code(adults, children, 0))
        await show_summary(message, state)
    else:
        await ask_infants(message, state)


@router.message(FlightSearch.children, F.text == "В начало")
async def children_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)


async def ask_infants(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    children = data.get("children", 0)
    remaining = 9 - adults - children
    max_infants = min(adults, remaining)
    row = [KeyboardButton(text=str(i)) for i in range(0, max_infants + 1)]
    kb = ReplyKeyboardMarkup(
        keyboard=[row, [KeyboardButton(text="В начало")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Сколько младенцев? (младше 2 лет без места)",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.infants)


@router.message(FlightSearch.infants, F.text.regexp(r"^\d+$"))
async def process_infants(message: Message, state: FSMContext):
    data = await state.get_data()
    adults = data["adults"]
    children = data.get("children", 0)
    remaining = 9 - adults - children
    max_infants = min(adults, remaining)
    infants = int(message.text)
    if infants < 0 or infants > max_infants:
        return
    await state.update_data(infants=infants)
    await show_summary(message, state)


@router.message(FlightSearch.infants, F.text == "В начало")
async def infants_to_menu(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        [InlineKeyboardButton(text="📊 Информация о рейсе", callback_data="track_flight")],
    ])
    await message.answer("👋 Привет! Я найду вам дешёвые авиабилеты.\n", reply_markup=ReplyKeyboardRemove())
    await message.answer("Выберите действие:", reply_markup=kb)

async def show_summary(message, state: FSMContext):
    # Сбрасываем флаг точечного редактирования
    await state.update_data(_edit_mode=False)
    data = await state.get_data()
    adults = data["adults"]
    children = data.get("children", 0)
    infants = data.get("infants", 0)

    passenger_code = str(adults)
    if children > 0:
        passenger_code += str(children)
    if infants > 0:
        passenger_code += str(infants)

    passenger_desc = f"{adults} взр."
    if children > 0:
        passenger_desc += f", {children} дет."
    if infants > 0:
        passenger_desc += f", {infants} мл."

    await state.update_data(
        passenger_code=passenger_code,
        passenger_desc=passenger_desc
    )
    data = await state.get_data()

    summary_steps = build_choices_summary(data)
    summary = (
        "Проверьте даты и данные:\n\n"
        f"{summary_steps}"
    )
    
    # Клавиатура подтверждения с кнопками редактирования
    data = await state.get_data()
    is_everywhere = data.get("origin") == "везде" or data.get("dest") == "везде"

    edit_buttons = [
        [InlineKeyboardButton(text="✏️ Маршрут", callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты", callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса", callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры", callback_data="edit_passengers")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        *edit_buttons,
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])

    await message.answer(summary, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    await message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)
    _schedule_inactivity(message.chat.id if hasattr(message, "chat") else message.from_user.id,
                          message.from_user.id)

async def _do_edit_action(callback: CallbackQuery, state: FSMContext, action: str):
    """
    Точечное изменение одного параметра.
    После изменения show_summary вернёт пользователя к экрану подтверждения,
    без прохождения всей цепочки заново.
    """
    if action == "route":
        # Маршрут — вводится текстом, переходим в FlightSearch.route
        # Но помечаем флаг, чтобы после ввода вернуться к summary, а не идти к датам
        await state.update_data(_edit_mode=True)
        await callback.message.edit_text(
            "✏️ Введите новый маршрут:\n<b>Город вылета - Город прибытия</b>\n\n<i>Пример: Москва - Сочи</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.route)

    elif action == "dates":
        # Только дата вылета — после ввода сразу к summary (через флаг _edit_mode)
        await state.update_data(_edit_mode=True)
        data = await state.get_data()
        is_roundtrip = data.get("need_return", False)
        hint = ""
        if is_roundtrip:
            hint = "\n(затем введёте дату обратного рейса)"
        await callback.message.edit_text(
            f"✏️ Введите новую дату вылета в формате <code>ДД.ММ</code>{hint}\n<i>Пример: 15.03</i>",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.depart_date)

    elif action == "flight_type":
        # Тип рейса — кнопки, после выбора сразу к summary
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_flight_type(callback.message, state)

    elif action == "passengers":
        # Пассажиры — кнопки, после выбора сразу к summary
        await state.update_data(_edit_mode=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await ask_adults(callback.message, state)


@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    action = callback.data[len("edit_"):]
    await _do_edit_action(callback, state, action)
    await callback.answer()

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    _cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    logger.info(f"[confirm_search] user={callback.from_user.id} маршрут={data.get('origin_iata')}→{data.get('dest_iata')}")
    await callback.message.edit_text("⏳ Ищу билеты...")
    # Ограничиваем число параллельных поисков, чтобы не перегружать API
    async with _SEARCH_SEMAPHORE:
        await _do_confirm_search(callback, state, data)


async def _do_confirm_search(callback: CallbackQuery, state: FSMContext, data: dict):
    """Основная логика поиска. Вызывается внутри семафора."""
    
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    flight_type = data.get("flight_type", "all")
    direct_only = (flight_type == "direct")
    transfers_only = (flight_type == "transfer")

    if is_origin_everywhere and not is_dest_everywhere:
        all_flights = await search_origin_everywhere(
            dest_iata=data["dest_iata"],
            depart_date=data["depart_date"],
            flight_type=data.get("flight_type", "all")
        )
        search_type = "origin_everywhere"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return
    elif not is_origin_everywhere and is_dest_everywhere:
        all_flights = await search_destination_everywhere(
            origin_iata=data["origin_iata"],
            depart_date=data["depart_date"],
            flight_type=data.get("flight_type", "all")
        )
        search_type = "destination_everywhere"
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, search_type)
        if success:
            await state.clear()
            return

    origins = [data["origin_iata"]]
    destinations = [data["dest_iata"]]
    all_flights = []

    # Парсим пассажиров из кода (например "211" → adults=2, children=1, infants=1)
    pax_code = data.get("passenger_code", "1")
    try:
        rt_adults   = int(pax_code[0]) if pax_code else 1
        rt_children = int(pax_code[1]) if len(pax_code) > 1 else 0
        rt_infants  = int(pax_code[2]) if len(pax_code) > 2 else 0
    except (ValueError, IndexError):
        rt_adults, rt_children, rt_infants = 1, 0, 0

    # Прогресс-сообщение — обновляется только если поиск идёт долго
    # cached API ~2-5 сек, real-time ~30-45 сек
    progress_msg = await callback.message.edit_text(
        "⏳ <b>Ищу билеты...</b>",
        parse_mode="HTML"
    )

    async def update_progress():
        await asyncio.sleep(10)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Запрашиваю актуальные цены...</b>\n"
                "<i>Получаю данные от авиакомпаний</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await asyncio.sleep(20)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Почти готово...</b>\n"
                "<i>Сравниваю предложения</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    progress_task = asyncio.create_task(update_progress())

    try:
        for orig in origins:
            for dest in destinations:
                if orig == dest:
                    continue
                flights = await search_flights_realtime(
                    origin=orig,
                    destination=dest,
                    depart_date=normalize_date(data["depart_date"]),
                    return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
                    adults=rt_adults,
                    children=rt_children,
                    infants=rt_infants,
                )
                if direct_only:
                    flights = [f for f in flights if f.get("transfers", 999) == 0]
                elif transfers_only:
                    flights = [f for f in flights if f.get("transfers", 0) > 0]

                for f in flights:
                    f["origin"] = orig
                    f["destination"] = dest
                all_flights.extend(flights)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    # Логируем источник для отладки
    sources = set(f.get("_source", "unknown") for f in all_flights)
    logger.info(f"🔍 [Search] {len(all_flights)} рейсов, источник: {sources}")

    # ── Нет прямых → предлагаем с пересадками ──────────────────
    if direct_only and not all_flights:
        # Делаем второй запрос — все рейсы (чтобы убедиться что маршрут вообще есть)
        all_flights_any = []
        for orig in origins:
            for dest in destinations:
                if orig == dest:
                    continue
                f2 = await search_flights_realtime(
                    origin=orig, destination=dest,
                    depart_date=normalize_date(data["depart_date"]),
                    return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
                    adults=rt_adults, children=rt_children, infants=rt_infants,
                )
                all_flights_any.extend(f2)

        if all_flights_any:
            # Есть рейсы с пересадками — предлагаем переключиться
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Показать рейсы с пересадками",
                    callback_data="retry_with_transfers"
                )],
                [InlineKeyboardButton(text="✏️ Изменить параметры", callback_data="back_to_summary")],
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
            ])
            await callback.message.edit_text(
                "😔 <b>Прямых рейсов на эти даты не найдено.</b>\n\nЕсть варианты с пересадками — они часто дешевле!",
                parse_mode="HTML",
                reply_markup=kb
            )
        else:
            # Вообще нет рейсов на маршруте
            passengers_code = data.get("passenger_code", "1")
            origin_iata = origins[0]
            d1 = format_avia_link_date(data["depart_date"])
            d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
            route = f"{origin_iata}{d1}{destinations[0]}{d2}{passengers_code}"
            partner_link = await convert_to_partner_link(f"https://www.aviasales.ru/search/{route}")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Поискать на Aviasales", url=partner_link)],
                [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="back_to_summary")],
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
            ])
            await callback.message.edit_text(
                "😔 <b>Рейсов по этому маршруту не найдено.</b>\n\n"
                "Попробуйте изменить даты или маршрут.",
                parse_mode="HTML",
                reply_markup=kb
            )
        return

    # ── Вообще нет рейсов ───────────────────────────────────────
    if not all_flights:
        passengers_code = data.get("passenger_code", "1")
        origin_iata = origins[0]
        d1 = format_avia_link_date(data["depart_date"])
        d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
        route = f"{origin_iata}{d1}{destinations[0]}{d2}{passengers_code}"
        partner_link = await convert_to_partner_link(f"https://www.aviasales.ru/search/{route}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Поискать на Aviasales", url=partner_link)],
            [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="back_to_summary")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            "😔 <b>Билеты не найдены.</b>\n\nПопробуйте изменить даты или маршрут.",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.clear()
        return

    cache_id = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    display_return = format_user_date(data["return_date"]) if data.get("return_date") else None
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        # маршрут
        "origin":       data.get("origin", ""),
        "origin_iata":  data.get("origin_iata", ""),
        "origin_name":  data.get("origin_name", ""),
        "dest":         data.get("dest", ""),
        "dest_iata":    data["dest_iata"],
        "dest_name":    data.get("dest_name", ""),
        # даты
        "depart_date":    data["depart_date"],
        "return_date":    data.get("return_date"),
        "need_return":    data.get("need_return", False),
        "display_depart": display_depart,
        "display_return": display_return,
        "original_depart": data["depart_date"],
        "original_return":  data.get("return_date"),
        # пассажиры
        "passenger_desc":  data["passenger_desc"],
        "passengers_code": data["passenger_code"],
        "passenger_code":  data["passenger_code"],
        "adults":    data.get("adults", 1),
        "children":  data.get("children", 0),
        "infants":   data.get("infants", 0),
        # прочее
        "origin_everywhere": False,
        "dest_everywhere":   False,
        "flight_type": flight_type,
    })

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
            from datetime import datetime
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

    text = "✅ <b>Самый дешёвый вариант</b>\n"
    price_per_passenger = int(float(price)) if price != "?" else 0
    passengers_code = data.get("passenger_code", "1")
    try:
        num_adults = int(passengers_code[0]) if passengers_code and passengers_code[0].isdigit() else 1
    except (IndexError, ValueError):
        num_adults = 1

    estimated_total_price = price_per_passenger * num_adults if price != "?" else "?"

    if price != "?":
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
    else:
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
    
    if (data.get("children", 0) > 0 or data.get("infants", 0) > 0):
        text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"

    text += f"\n\n🛫 <b>Рейс:</b> {origin_name} → {dest_name}"
    text += f"\n📍 {origin_airport} ({origin_iata}) → {dest_airport} ({dest_iata})"
    text += f"\n📅 <b>Туда:</b> {display_depart}"

    if data.get("need_return", False) and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"

    text += f"\n⏱️ <b>Продолжительность:</b> {duration}"
    text += f"\n{transfer_text}"

    airline = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_name_map = {
            "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа", "U6": "Уральские авиалинии",
            "FV": "Россия", "UT": "ЮТэйр", "N4": "Нордстар", "IK": "Победа"
        }
        airline_display = airline_name_map.get(airline, airline)
        flight_display = f"{airline_display} {flight_number}" if flight_number else airline_display
        text += f"\n✈️ <b>Авиакомпания и номер рейса:</b> {flight_display}"

    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    passengers_code = data.get("passenger_code", "1")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=data["depart_date"],
            passengers_code=passengers_code,
            return_date=data["return_date"] if data.get("need_return") else None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    fallback_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data["depart_date"],
        passengers_code=passengers_code,
        return_date=data["return_date"] if data.get("need_return") else None
    )
    if not fallback_link.startswith(('http://', 'https://')):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    booking_link = await convert_to_partner_link(booking_link)
    fallback_link = await convert_to_partner_link(fallback_link)

    kb_buttons = []
    if booking_link:
        kb_buttons.append([
            InlineKeyboardButton(text=f"✈️ Посмотреть детали за {price} ₽", url=booking_link)
        ])
    kb_buttons.append([
        InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="✏️ Изменить данные", callback_data=f"edit_from_results_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")
    ])

    SUPPORTED_TRANSFER_AIRPORTS = [
        "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
        "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
        "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK"
    ]
    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-2, [
            InlineKeyboardButton(
                text=f"🚖 Трансфер в {dest_name}",
                url=transfer_link
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

    # После результатов поиска — через 10 мин напоминаем о горячих предложениях
    asyncio.create_task(
        _remind_hot_deals_after_search(callback.message.chat.id, callback.from_user.id)
    )


async def _get_bot():
    """Получить экземпляр бота из синглтона."""
    from utils.bot_instance import bot as _bot
    return _bot


async def _remind_hot_deals_after_search(chat_id: int, user_id: int):
    """Через 10 минут после завершения поиска — предлагаем подписку на вау-цены."""
    await asyncio.sleep(10 * 60)
    try:
        _bot = await _get_bot()
        if not _bot:
            return
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Подписаться на вау-цены", callback_data="hot_deals_menu")],
            [InlineKeyboardButton(text="✈️ Новый поиск", callback_data="start_search")],
        ])
        await _bot.send_message(
            chat_id,
            "✈️ Кстати, укажи интересные тебе направления — "
            "и я сообщу о вау-ценах туда, как только они появятся!",
            reply_markup=kb
        )
        logger.info(f"[Reminder/after-search] user_id={user_id}")
    except Exception as e:
        logger.debug(f"[Reminder/after-search] ошибка: {e}")


# ── Трекер активности пользователя в FSM ─────────────────────────────────────
# Ключ: chat_id → asyncio.Task напоминания при бездействии
_inactivity_tasks: dict[int, asyncio.Task] = {}


def _cancel_inactivity(chat_id: int):
    """Отменить текущую задачу напоминания (пользователь активен)."""
    task = _inactivity_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def _schedule_inactivity(chat_id: int, user_id: int):
    """
    Перезапустить таймер бездействия.
    Вызывается при каждом шаге FSM.
    Напоминание 1 через 3 мин, напоминание 2 (вау-цены) через 10 мин.
    """
    _cancel_inactivity(chat_id)
    task = asyncio.create_task(_inactivity_reminder(chat_id, user_id))
    _inactivity_tasks[chat_id] = task


async def _inactivity_reminder(chat_id: int, user_id: int):
    """Двухэтапное напоминание при бездействии во время заполнения формы поиска."""
    try:
        # ── Этап 1: через 3 минуты — предлагаем продолжить ─────────
        await asyncio.sleep(3 * 60)
        _bot = await _get_bot()
        if not _bot:
            return
        kb1 = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить поиск", callback_data="start_search")],
            [InlineKeyboardButton(text="🔥 Горячие предложения", callback_data="hot_deals_menu")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
        await _bot.send_message(
            chat_id,
            "👋 Ты ещё здесь? Давай продолжим — я помогу найти дешёвые билеты!",
            reply_markup=kb1
        )
        logger.info(f"[Inactivity/1] Напоминание #1 отправлено user_id={user_id}")

        # ── Этап 2: ещё через 7 минут (итого 10) — вау-цены ────────
        await asyncio.sleep(7 * 60)
        _bot = await _get_bot()
        if not _bot:
            return
        kb2 = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔥 Подписаться на вау-цены", callback_data="hot_deals_menu")],
            [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
        ])
        await _bot.send_message(
            chat_id,
            "✈️ Укажи интересные направления — и я сообщу о вау-ценах туда, "
            "как только они появятся!",
            reply_markup=kb2
        )
        logger.info(f"[Inactivity/2] Напоминание #2 отправлено user_id={user_id}")

    except asyncio.CancelledError:
        pass  # Пользователь снова активен — всё нормально
    except Exception as e:
        logger.debug(f"[Inactivity] ошибка: {e}")
    finally:
        _inactivity_tasks.pop(chat_id, None)


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

async def handle_flight_request(message: Message):
    text = message.text.strip().lower()
    match = re.match(
        r"^([а-яёa-z\s]+?)\s*[-→>—\s]+\s*([а-яёa-z\s]+?)\s+(\d{1,2}.\d{1,2})(?:\s*[-–]\s*(\d{1,2}.\d{1,2}))?\s*(.*)?$",
        text, re.IGNORECASE
    )
    if not match:
        await message.answer(
            "Неверный формат. Пример:\n`Орск - Пермь 10.03`",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
    is_roundtrip = bool(return_date)
    is_origin_everywhere = origin_city.strip() == "везде"
    is_dest_everywhere = dest_city.strip() == "везде"

    flight_type = "all"
    if passengers_part:
        text_lower = passengers_part.lower()
        if "прям" in text_lower or "direct" in text_lower:
            flight_type = "direct"
        elif "пересад" in text_lower or "transfer" in text_lower or "с пересад" in text_lower:
            flight_type = "transfer"

    direct_only = (flight_type == "direct")
    transfers_only = (flight_type == "transfer")

    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return

    # ← ПРОВЕРКА: Одинаковые города (ручной ввод)
    # ← ИСПОЛЬЗУЕМ get_iata() + fallback
    orig_iata_check = get_iata(origin_city.strip()) or CITY_TO_IATA.get(_normalize_name(origin_city.strip()))
    dest_iata_check = get_iata(dest_city.strip()) or CITY_TO_IATA.get(_normalize_name(dest_city.strip()))
    
    if orig_iata_check and dest_iata_check and orig_iata_check == dest_iata_check:
        logger.info(f"[DEBUG VALIDATION] Ручной ввод: одинаковые города {orig_iata_check}")
        print(f"[DEBUG VALIDATION] Ручной ввод: одинаковые города {orig_iata_check}")
        await message.answer(
            "❌ Город вылета и прибытия не могут совпадать.\n"
            "Пожалуйста, выберите разные города.",
            reply_markup=CANCEL_KB
        )
        return

    # ← ПРОВЕРКА: Даты (ручной ввод)
    if return_date:
        norm_depart_manual = normalize_date(depart_date)
        norm_return_manual = normalize_date(return_date)
        logger.info(f"[DEBUG VALIDATION] Ручной ввод: Вылет {norm_depart_manual} vs Возврат {norm_return_manual}")
        print(f"[DEBUG VALIDATION] Ручной ввод: Вылет {norm_depart_manual} vs Возврат {norm_return_manual}")
        if norm_return_manual <= norm_depart_manual:
            await message.answer(
                "❌ Дата возврата не может быть раньше или равна дате вылета.\n"
                "Укажите правильную дату возврата.",
                reply_markup=CANCEL_KB
            )
            return

    if is_origin_everywhere or is_dest_everywhere:
        passengers_code = parse_passengers((passengers_part or "").strip())
        success = await handle_everywhere_search_manual(
            message=message,
            origin_city=origin_city,
            dest_city=dest_city,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code,
            is_origin_everywhere=is_origin_everywhere,
            is_dest_everywhere=is_dest_everywhere
        )
        if success:
            return

    # ← ИСПОЛЬЗУЕМ get_iata() + fallback для dest
    dest_iata = get_iata(dest_city.strip()) or CITY_TO_IATA.get(_normalize_name(dest_city.strip()))
    if not dest_iata:
        await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
        return

    origin_clean = origin_city.strip()
    # ← ИСПОЛЬЗУЕМ get_iata() + fallback для origin
    orig_iata = get_iata(origin_clean) or CITY_TO_IATA.get(_normalize_name(origin_clean))
    if not orig_iata:
        await message.answer(f"Не знаю город вылета: {origin_clean}", reply_markup=CANCEL_KB)
        return

    origins = [orig_iata]
    # ← ИСПОЛЬЗУЕМ get_city_name() + fallback для названий
    origin_name = get_city_name(orig_iata) or IATA_TO_CITY.get(orig_iata, origin_clean.capitalize())
    dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
    
    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = build_passenger_desc(passengers_code)
    display_depart = format_user_date(depart_date)
    display_return = format_user_date(return_date) if return_date else None

    await message.answer("Ищу билеты...")
    all_flights = []

    for orig in origins:
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None,
            direct=direct_only
        )
        if direct_only:
            flights = [f for f in flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            flights = [f for f in flights if f.get("transfers", 0) > 0]
        
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
        await asyncio.sleep(0.5)

    if direct_only and not all_flights:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔄 Показать рейсы с пересадками",
                    callback_data="show_transfers_fallback"
                )
            ],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ])
        await message.answer(
            "😔 Прямых рейсов на эти даты не найдено.\n"
            "Хотите посмотреть варианты с пересадками? Они часто дешевле!",
            reply_markup=kb
        )
        return

    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(depart_date)
        d2 = format_avia_link_date(return_date) if return_date else ""
        clean_link = f"https://www.aviasales.ru/search/{origin_iata}{d1}{dest_iata}{d2}{passengers_code}"
        partner_link = await convert_to_partner_link(clean_link)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales", url=partner_link)],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ])
        await message.answer(
            "😔 Билеты не найдены.\nНа Aviasales могут быть рейсы с пересадками — попробуйте:",
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
        "passenger_desc": passenger_desc,
        "passengers_code": passengers_code,
        "origin_everywhere": False,
        "dest_everywhere": False,
        "flight_type": flight_type
    })

    top_flight = find_cheapest_flight_on_exact_date(
        all_flights,
        depart_date,
        return_date
    )
    price = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata = top_flight["origin"]
    dest_iata = dest_iata
    # ← ИСПОЛЬЗУЕМ get_city_name() + fallback
    origin_name = get_city_name(origin_iata) or IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name = get_city_name(dest_iata) or IATA_TO_CITY.get(dest_iata, dest_iata)
    

    def format_datetime(dt_str):
        if not dt_str:
            return "??:??"
        try:
            from datetime import datetime
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

    header = f"✅ <b>Самый дешёвый вариант на {display_depart} ({passenger_desc}):</b>"
    route_line = f"🛫 <b>Рейс: {origin_name}</b> → <b>{dest_name}</b>"
    text = (
        f"{header}\n"
        f"{route_line}\n"
        f"📍({origin_iata}) → ({dest_iata})\n"
        f"📅 Туда: {display_depart}\n"
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

    price_per_passenger = int(float(price)) if price != "?" else 0
    passengers_code = data.get("passenger_code", "1") if 'data' in locals() else "1"
    try:
        num_adults = int(passengers_code[0]) if passengers_code and passengers_code[0].isdigit() else 1
    except (IndexError, ValueError):
        num_adults = 1

    estimated_total_price = price_per_passenger * num_adults if price != "?" else "?"

    if price != "?":
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_passenger} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерная стоимость для {num_adults} взрослых:</b> ~{estimated_total_price} ₽"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"
    else:
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Цена за {num_adults} взрослых:</b> ~{estimated_total_price} ₽ (если доступно)"
            text += f"\n<i>(стоимость для детей и младенцев может рассчитываться по-другому)</i>"

    text += f"\n📅 <b>Туда:</b> {display_depart}"

    if is_roundtrip and display_return:
        text += f"\n↩️ Обратно: {display_return}"

    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_date,
            passengers_code=passengers_code,
            return_date=return_date if is_roundtrip else None
        )
        if not booking_link.startswith(('http://', 'https://')):
            booking_link = f"https://www.aviasales.ru{booking_link}"
        
    fallback_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=depart_date,
        passengers_code=passengers_code,
        return_date=return_date if is_roundtrip else None
    )
    if not fallback_link.startswith(('http://', 'https://')):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    booking_link = await convert_to_partner_link(booking_link)
    fallback_link = await convert_to_partner_link(fallback_link)

    kb_buttons = []
    if booking_link:
        kb_buttons.append([
            InlineKeyboardButton(text=f"✈️ Посмотреть детали за {price} ₽", url=booking_link)
        ])
    kb_buttons.append([
        InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="✏️ Изменить данные", callback_data=f"edit_from_results_{cache_id}")
    ])
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")
    ])

    SUPPORTED_TRANSFER_AIRPORTS = [
        "BKK", "HKT", "CNX", "USM", "DAD", "SGN", "CXR", "REP", "PNH",
        "DPS", "MLE", "KIX", "CTS", "DXB", "AUH", "DOH", "AYT", "ADB",
        "BJV", "DLM", "PMI", "IBZ", "AGP", "RHO", "HER", "CFU", "JMK"
    ]
    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-2, [
            InlineKeyboardButton(
                text=f"🚖 Трансфер в {dest_name}",
                url=transfer_link
            )
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery):
    parts = callback.data.split("_")
    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        is_origin_everywhere = data.get("origin_everywhere", False)
        is_dest_everywhere = data.get("dest_everywhere", False)
        if is_dest_everywhere:
            origin = data["flights"][0]["origin"]
            dest = None
        elif is_origin_everywhere:
            origin = None
            dest = data.get("dest_iata") or data["flights"][0].get("destination")
        else:
            origin = data["flights"][0]["origin"]
            dest = data.get("dest_iata") or data["flights"][0].get("destination")
        
        min_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        price = min_flight.get("value") or min_flight.get("price")
        depart_date = data["original_depart"]
        return_date = data["original_return"]
    else:
        cache_id = parts[1]
        price = int(parts[2])
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        
        top_flight = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        origin = top_flight["origin"]
        dest = data.get("dest_iata") or top_flight.get("destination")
        depart_date = data["original_depart"]
        return_date = data["original_return"]

    origin_name = IATA_TO_CITY.get(origin, origin) if origin else "Везде"
    dest_name = IATA_TO_CITY.get(dest, dest) if dest else "Везде"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=" Любое изменение цены", callback_data=f"set_threshold:0:{cache_id}:{price}")],
        [InlineKeyboardButton(text=" Изменение на сотни ₽", callback_data=f"set_threshold:100:{cache_id}:{price}")],
        [InlineKeyboardButton(text=" Изменение на тысячи ₽", callback_data=f"set_threshold:1000:{cache_id}:{price}")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])
    await callback.message.answer(
        f"🔔 <b>Выберите условия уведомлений</b>\n",
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
    dest = data.get("dest_iata") or top_flight.get("destination")
    is_origin_everywhere = data.get("origin_everywhere", False)
    is_dest_everywhere = data.get("dest_everywhere", False)

    watch_key = await redis_client.save_price_watch(
        user_id=callback.from_user.id,
        origin=origin if not is_origin_everywhere else None,
        dest=dest if not is_dest_everywhere else None,
        depart_date=data["original_depart"],
        return_date=data["original_return"],
        current_price=price,
        passengers=data.get("passenger_code", "1"),
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
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])
    response_text = (
        f"✅ <b>Отлично! Я буду следить за ценами</b>\n"
        f"📲 Пришлю уведомление, если цена изменится!\n"
        f"📍 Маршрут: {origin_name} → {dest_name}\n"
        f"📅 Вылет: {data['display_depart']}\n"
    )
    if data.get('display_return'):
        response_text += f"📅 Возврат: {data['display_return']}\n"
    response_text += (
        f"💰 Текущая цена: {price} ₽\n"
        f" Уведомлять при: {condition_text}\n"
    )
    await callback.message.edit_text(response_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

transfer_context: Dict[int, Dict[str, Any]] = {}

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
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
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
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "Хорошо! Если передумаете — просто выполните новый поиск билетов. ✈️",
        reply_markup=kb
    )
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
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
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
        f"🚀 <b>Варианты трансфера {depart_date}</b>\n"
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
            InlineKeyboardButton(text=f"🚀 Вариант {i}: {price} ₽", url=transfer_link)
        ])
    buttons.append([
        InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(message_text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    text = message.text or ""

    # Пропускаем команды
    if text.startswith("/"):
        return

    # Пропускаем чужие FSM
    if current_state and current_state.startswith("FlyStackTrack"):
        return
    if current_state and current_state.startswith("HotDealsSub"):
        return

    # Внутри FlightSearch — предупреждаем (кроме "В начало")
    if current_state and current_state.startswith("FlightSearch"):
        logger.warning(f"⚠️ [Start] Пользователь в состоянии {current_state}: {text[:30]}")
        await message.answer(
            "Пожалуйста, завершите текущий поиск или отмените его через кнопку ↩️ В начало",
            reply_markup=CANCEL_KB
        )
        return

    # Вне FSM — пробуем тихий ручной поиск
    await handle_flight_request(message)

@router.callback_query(F.data.startswith("unwatch_"))
async def handle_unwatch(callback: CallbackQuery):
    key = callback.data.split("unwatch_")[1]
    user_id = callback.from_user.id
    if f":{user_id}:" not in key:
        await callback.answer("❌ Это не ваше отслеживание!", show_alert=True)
        return
    await redis_client.remove_watch(user_id, key)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "✅ Отслеживание цены остановлено.\n"
        "Больше не буду присылать уведомления по этому маршруту.",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data == "retry_with_transfers")
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    """Повторный поиск со всеми типами рейсов (убираем фильтр 'direct')"""
    data = await state.get_data()
    if not data:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")]
        ])
        await callback.message.edit_text(
            "😔 Данные поиска устарели. Выполните новый поиск.",
            reply_markup=kb
        )
        await callback.answer()
        return
    await state.update_data(flight_type="all")
    await confirm_search(callback, state)
    await callback.answer()


@router.callback_query(F.data == "back_to_summary")
async def back_to_summary(callback: CallbackQuery, state: FSMContext):
    """Возврат к экрану подтверждения для изменения параметров"""
    data = await state.get_data()
    if not data or "depart_date" not in data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Новый поиск", callback_data="start_search")]
            ])
        )
        await callback.answer()
        return
    # Восстанавливаем экран summary
    summary_steps = build_choices_summary(data)
    summary = "Проверьте даты и данные:\n\n" + summary_steps
    edit_buttons = [
        [InlineKeyboardButton(text="✏️ Маршрут", callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты", callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса", callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры", callback_data="edit_passengers")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        *edit_buttons,
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
    ])
    await callback.message.edit_text(summary, parse_mode="HTML")
    await callback.message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_from_results_"))
async def edit_from_results(callback: CallbackQuery, state: FSMContext):
    """Кнопка 'Изменить данные' со страницы результатов.
    Полностью восстанавливает FSM из кэша, затем показывает summary с кнопками редактирования."""
    cache_id = callback.data.replace("edit_from_results_", "")
    cached = await redis_client.get_search_cache(cache_id)
    if not cached:
        await callback.answer("Данные устарели, начните новый поиск", show_alert=True)
        return

    # Восстанавливаем полное состояние FSM из кэша
    cached.pop("flights", None)  # рейсы не нужны в FSM
    fsm_data = {
        "origin":         cached.get("origin", ""),
        "origin_iata":    cached.get("origin_iata", ""),
        "origin_name":    cached.get("origin_name", ""),
        "dest":           cached.get("dest", ""),
        "dest_iata":      cached.get("dest_iata", ""),
        "dest_name":      cached.get("dest_name", ""),
        "depart_date":    cached.get("depart_date") or cached.get("original_depart", ""),
        "return_date":    cached.get("return_date") or cached.get("original_return"),
        "need_return":    cached.get("need_return", False),
        "flight_type":    cached.get("flight_type", "all"),
        "adults":         cached.get("adults", 1),
        "children":       cached.get("children", 0),
        "infants":        cached.get("infants", 0),
        "passenger_code": cached.get("passenger_code") or cached.get("passengers_code", "1"),
        "passenger_desc": cached.get("passenger_desc", "1 взр."),
        "_edit_mode":     False,
    }
    await state.update_data(**fsm_data)
    await state.set_state(FlightSearch.confirm)

    # Показываем summary с кнопками редактирования (через back_to_summary логику)
    summary_steps = build_choices_summary(fsm_data)
    summary = "Проверьте даты и данные:\n\n" + summary_steps

    edit_buttons = [
        [InlineKeyboardButton(text="✏️ Маршрут",    callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",        callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",   callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",   callback_data="edit_passengers")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        *edit_buttons,
        [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
    ])
    await callback.message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# result_edit_ коллбэки удалены — edit_from_results теперь восстанавливает
# полный FSM в FlightSearch.confirm и использует те же edit_ коллбэки


# Обратная совместимость со старым форматом callback_data
@router.callback_query(F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers_legacy(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")]
            ])
        )
        await callback.answer()
        return
    await state.update_data(flight_type="all")
    await confirm_search(callback, state)
    await callback.answer()