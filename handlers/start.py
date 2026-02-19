import re
import logging
from datetime import datetime
from typing import Tuple, Optional
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from utils.cities import CITY_TO_IATA, IATA_TO_CITY, GLOBAL_HUBS
from utils.logger import logger
from services.flight_search import search_flights, validate_date, normalize_date, format_avia_link_date
from services.everywhere_search import search_origin_everywhere, search_dest_everywhere
from utils.redis_client import redis_client
from utils.link_converter import convert_to_partner_link

router = Router()

# Клавиатура отмены
CANCEL_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
])

class FlightSearch(StatesGroup):
    route = State()
    depart_date = State()
    return_date = State()
    flight_type = State()
    confirm = State()

def validate_dates_order(depart_date: str, return_date: str) -> bool:
    """Проверяет, что дата возврата не раньше даты вылета"""
    try:
        logger.debug(f"[validate_dates_order] Проверка дат: вылет={depart_date}, возврат={return_date}")
        
        # Разбираем даты
        depart_day, depart_month = map(int, depart_date.split('.'))
        return_day, return_month = map(int, return_date.split('.'))
        
        # Определяем год (2026 или 2027)
        current_year = 2026
        current_month = 2  # Текущий месяц - февраль 2026
        current_day = 19   # Текущий день - 19 февраля 2026
        
        # Для даты вылета
        if depart_month < current_month or (depart_month == current_month and depart_day < current_day):
            depart_year = 2027
        else:
            depart_year = 2026
            
        # Для даты возврата
        if return_month < current_month or (return_month == current_month and return_day < current_day):
            return_year = 2027
        else:
            return_year = 2026
            
        # Если дата возврата в том же месяце, что и вылет, но год определился разный
        if depart_month == return_month and depart_year > return_year:
            return_year = depart_year
            
        depart_dt = datetime(depart_year, depart_month, depart_day)
        return_dt = datetime(return_year, return_month, return_day)
        
        logger.debug(f"[validate_dates_order] Вылет: {depart_dt}, Возврат: {return_dt}")
        return return_dt >= depart_dt
    except Exception as e:
        logger.error(f"[validate_dates_order] Ошибка при проверке дат: {e}")
        return False

def validate_route(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Валидация и нормализация маршрута"""
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
    
    # Нормализация специфических названий
    origin = origin.replace("санкт петербург", "санкт-петербург")
    origin = origin.replace("ростов на дону", "ростов-на-дону")
    dest = dest.replace("санкт петербург", "санкт-петербург")
    dest = dest.replace("ростов на дону", "ростов-на-дону")
    
    logger.debug(f"[validate_route] Обработанный маршрут: origin='{origin}', dest='{dest}'")
    return origin, dest

@router.message(F.text)
async def handle_flight_request(message: Message):
    """Обработка запроса на поиск рейсов в свободном формате"""
    text = message.text.strip().lower()
    
    # Проверка на "везде"
    if text == "везде":
        await message.answer(
            "✈️ Поиск самых дешёвых рейсов из всех городов России\n"
            "Укажите дату вылета в формате `ДД.ММ`",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await message.answer("Пример: 15.03", reply_markup=CANCEL_KB)
        return
    
    # Проверка формата: Город - Город ДД.ММ [ДД.ММ] [пассажиры]
    match = re.match(
        r"^([а-яёa-z\s]+?)\s*[-→>—\s]+\s*([а-яёa-z\s]+?)\s+(\d{1,2}.\d{1,2})(?:\s*[-–]\s*(\d{1,2}.\d{1,2}))?\s*(.*)?$",
        text, re.IGNORECASE
    )
    
    if not match:
        # Проверка формата: Город ДД.ММ [ДД.ММ] [пассажиры]
        match = re.match(
            r"^([а-яёa-z\s]+?)\s+(\d{1,2}.\d{1,2})(?:\s*[-–]\s*(\d{1,2}.\d{1,2}))?\s*(.*)?$",
            text, re.IGNORECASE
        )
        if not match:
            await message.answer(
                "Неверный формат. Примеры:\n"
                "`Москва - Сочи 10.03`\n"
                "`Москва 10.03`\n"
                "`Сочи 10.03-15.03`",
                parse_mode="HTML",
                reply_markup=CANCEL_KB
            )
            return
    
    try:
        if len(match.groups()) >= 4:
            origin_city, dest_city, depart_date, return_date, passengers_part = match.groups()
            is_origin_everywhere = origin_city.strip().lower() == "везде"
            is_dest_everywhere = dest_city.strip().lower() == "везде"
        else:
            origin_city = "везде"
            dest_city, depart_date, return_date, passengers_part = match.groups()
            is_origin_everywhere = True
            is_dest_everywhere = False
        
        logger.debug(f"[handle_flight_request] Параметры: origin={origin_city}, dest={dest_city}, "
                    f"depart={depart_date}, return={return_date}, passengers={passengers_part}")
        
        # Проверка на одинаковые города
        if not is_origin_everywhere and not is_dest_everywhere:
            if origin_city.strip().lower() == dest_city.strip().lower():
                logger.warning(f"[handle_flight_request] ОТКЛОНЕНО: одинаковые города '{origin_city}'")
                await message.answer(
                    "❌ Город отправления и прибытия не могут совпадать!\n"
                    f"Вы ввели: {origin_city.strip()} - {dest_city.strip()}\n"
                    "Пожалуйста, укажите разные города.",
                    parse_mode="HTML",
                    reply_markup=CANCEL_KB
                )
                return
        
        # Проверка формата дат
        if not validate_date(depart_date):
            await message.answer(
                "❌ Неверный формат даты вылета.\n"
                "Введите в формате `ДД.ММ` (например: 15.03)",
                parse_mode="HTML",
                reply_markup=CANCEL_KB
            )
            return
        
        if return_date and not validate_date(return_date):
            await message.answer(
                "❌ Неверный формат даты возврата.\n"
                "Введите в формате `ДД.ММ` (например: 15.03)",
                parse_mode="HTML",
                reply_markup=CANCEL_KB
            )
            return
        
        # Проверка порядка дат
        if return_date and not validate_dates_order(depart_date, return_date):
            logger.warning(f"[handle_flight_request] ОТКЛОНЕНО: возврат раньше вылета "
                          f"(вылет={depart_date}, возврат={return_date})")
            await message.answer(
                "❌ Дата возврата не может быть раньше даты вылета!\n"
                f"Вылет: {depart_date}, Возврат: {return_date}",
                parse_mode="HTML",
                reply_markup=CANCEL_KB
            )
            return
        
        # Парсинг пассажиров
        passengers_code = "1"  # По умолчанию 1 взрослый
        if passengers_part:
            # Убираем всё кроме цифр и оставляем максимум 3 цифры
            passengers_code = re.sub(r'\D', '', passengers_part)[:3]
            # Если после очистки пусто или начинается с 0 — используем "1"
            if not passengers_code or passengers_code[0] == '0':
                passengers_code = "1"
        
        logger.debug(f"[handle_flight_request] passengers_code: '{passengers_code}' (тип: {type(passengers_code)})")
        
        # Поиск рейсов
        if is_origin_everywhere or is_dest_everywhere:
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
        
        # Обычный поиск
        origin_iata = CITY_TO_IATA.get(origin_city.strip())
        if not origin_iata:
            await message.answer(f"Не знаю город отправления: {origin_city.strip()}", reply_markup=CANCEL_KB)
            return
        
        dest_iata = CITY_TO_IATA.get(dest_city.strip())
        if not dest_iata:
            await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return
        
        all_flights = await search_flights(
            origins=[origin_iata],
            destinations=[dest_iata],
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code
        )
        
        if not all_flights:
            origin_name = IATA_TO_CITY.get(origin_iata, origin_city.strip().capitalize())
            dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
            
            d1 = format_avia_link_date(depart_date)
            d2 = format_avia_link_date(return_date) if return_date else ""
            route = f"{origin_iata}{d1}{dest_iata}{d2}{passengers_code}"
            clean_link = f"https://www.aviasales.ru/search/{route}"
            partner_link = await convert_to_partner_link(clean_link)
            
            await message.answer(
                f"😔 Рейсов по маршруту {origin_name} → {dest_name} на эти даты не найдено.\n\n"
                "Попробуйте:\n"
                "• Изменить даты\n"
                "• Поискать на другие дни\n"
                "• Посмотреть варианты с пересадками\n\n"
                f"[Посмотреть на Aviasales]({partner_link})",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            return
        
        # Формирование и отправка результатов
        response = []
        for i, flight in enumerate(all_flights[:3], 1):
            price = flight['price']
            transfers = flight.get('transfers', 0)
            direct = "прямой" if transfers == 0 else f"с {transfers} пересадкой(ами)"
            response.append(
                f"{i}. {price} ₽ • {direct}\n"
                f"   {flight['airline']} • {flight['departure']} → {flight['arrival']}"
            )
        
        if return_date:
            dates_text = f"{depart_date} → {return_date}"
        else:
            dates_text = depart_date
        
        origin_name = IATA_TO_CITY.get(origin_iata, origin_city.strip().capitalize())
        dest_name = IATA_TO_CITY.get(dest_iata, dest_city.strip().capitalize())
        
        response_text = (
            f"✈️ {origin_name} → {dest_name} | {dates_text}\n\n" +
            "\n".join(response) +
            f"\n\n[Посмотреть все варианты]({partner_link})"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Уведомить о снижении цены", callback_data="watch_price")],
            [InlineKeyboardButton(text="🔄 Изменить поиск", callback_data="edit_search")]
        ])
        
        await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)
    
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {e}")
        await message.answer("Произошла ошибка при обработке запроса. Попробуйте позже.")

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
    """Обработка поиска с 'везде'"""
    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return False
    
    if is_origin_everywhere:
        dest_iata = CITY_TO_IATA.get(dest_city.strip())
        if not dest_iata:
            await message.answer(f"Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return False
        
        all_flights = await search_origin_everywhere(
            dest_iata=dest_iata,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code
        )
        search_type = "origin_everywhere"
    else:  # is_dest_everywhere
        origin_iata = CITY_TO_IATA.get(origin_city.strip())
        if not origin_iata:
            await message.answer(f"Не знаю город отправления: {origin_city.strip()}", reply_markup=CANCEL_KB)
            return False
        
        all_flights = await search_dest_everywhere(
            origin_iata=origin_iata,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code
        )
        search_type = "dest_everywhere"
    
    if not all_flights:
        await message.answer("😔 К сожалению, рейсов не найдено. Попробуйте изменить даты.")
        return False
    
    # Формирование и отправка результатов
    response = []
    for i, flight in enumerate(all_flights[:3], 1):
        price = flight['price']
        origin = flight['origin']
        dest = flight['destination']
        origin_name = IATA_TO_CITY.get(origin, origin)
        dest_name = IATA_TO_CITY.get(dest, dest)
        response.append(
            f"{i}. {price} ₽ • {origin_name} → {dest_name}\n"
            f"   {flight['airline']} • {flight['departure']} → {flight['arrival']}"
        )
    
    if return_date:
        dates_text = f"{depart_date} → {return_date}"
    else:
        dates_text = depart_date
    
    if is_origin_everywhere:
        header = f"✈️ Из всех городов → {dest_city.strip().capitalize()} | {dates_text}"
    else:
        header = f"✈️ {origin_city.strip().capitalize()} → Все популярные города | {dates_text}"
    
    response_text = (
        f"{header}\n\n" +
        "\n".join(response) +
        "\n\n🔔 Установите уведомление о снижении цены!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Уведомить о снижении цены", callback_data="watch_price")],
        [InlineKeyboardButton(text="🔄 Изменить поиск", callback_data="edit_search")]
    ])
    
    await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)
    return True

@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    """Обработка ввода маршрута"""
    origin, dest = validate_route(message.text)
    
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n"
            "Попробуйте ещё раз: `Москва - Сочи`",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    logger.debug(f"[process_route] Введён маршрут: origin='{origin}', dest='{dest}'")
    
    # Проверка на одинаковые города (кроме "везде")
    if origin == dest and origin != "везде":
        logger.warning(f"[process_route] ОТКЛОНЕНО: одинаковые города '{origin}'")
        await message.answer(
            "❌ Город отправления и прибытия не могут совпадать!\n"
            f"Вы ввели: {origin} - {dest}\n"
            "Пожалуйста, укажите разные города.",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    # Проверка на "везде → везде"
    if origin == "везде" and dest == "везде":
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return
    
    # Определение IATA кодов
    if origin != "везде":
        orig_iata = CITY_TO_IATA.get(origin)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город отправления: {origin}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        origin_name = IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = None
        origin_name = "Везде"
    
    if dest != "везде":
        dest_iata = CITY_TO_IATA.get(dest)
        if not dest_iata:
            await message.answer(f"❌ Не знаю город прибытия: {dest}\nПопробуйте ещё раз.", reply_markup=CANCEL_KB)
            return
        dest_name = IATA_TO_CITY.get(dest_iata, dest.capitalize())
    else:
        dest_iata = None
        dest_name = "Везде"
    
    # Дополнительная проверка на одинаковые IATA коды
    if orig_iata and dest_iata and orig_iata == dest_iata:
        logger.warning(f"[process_route] ОТКЛОНЕНО: одинаковые IATA коды '{orig_iata}'")
        await message.answer(
            "❌ Город отправления и прибытия не могут совпадать!\n"
            f"Код аэропорта: {orig_iata}\n"
            "Пожалуйста, укажите разные города.",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    logger.info(f"[process_route] Маршрут принят: {origin_name} → {dest_name}")
    
    # Сохранение данных
    await state.update_data(
        origin=origin,
        origin_iata=orig_iata,
        dest=dest,
        dest_iata=dest_iata,
        origin_name=origin_name,
        dest_name=dest_name
    )
    
    # Подсказка
    if dest == "везде":
        hint = f"✈️ Буду искать рейсы из {origin_name} во все популярные города мира (покажу топ-3)"
    elif origin == "везде":
        hint = f"✈️ Буду искать рейсы из всех городов России в {dest_name}"
    else:
        hint = f"✈️ Маршрут: {origin_name} → {dest_name}"
    
    await message.answer(
        hint + "\n\n" +
        "📅 Введите дату вылета в формате `ДД.ММ`",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.depart_date)

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    """Обработка ввода даты вылета"""
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате `ДД.ММ` (например: 15.03)",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    logger.debug(f"[process_depart_date] Введена дата вылета: {message.text}")
    
    await state.update_data(depart_date=message.text)
    
    # Спрашиваем дату возврата только если не "везде → город"
    data = await state.get_data()
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    
    if not is_origin_everywhere and not is_dest_everywhere:
        await message.answer(
            "📅 Введите дату возврата в формате `ДД.ММ` или `-`, если в одну сторону",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.return_date)
    else:
        await ask_flight_type(message, state)

async def ask_flight_type(message: Message, state: FSMContext):
    """Спрашиваем тип рейса (прямой/с пересадками)"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Прямые рейсы", callback_data="flight_type_direct")],
        [InlineKeyboardButton(text="🔄 С пересадками", callback_data="flight_type_transfer")],
        [InlineKeyboardButton(text="ℹ️ Все рейсы", callback_data="flight_type_all")]
    ])
    
    await message.answer(
        "Какие рейсы показывать?",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.flight_type)

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    """Обработка ввода даты возврата"""
    if message.text.strip() == "-":
        await state.update_data(return_date=None)
        await ask_flight_type(message, state)
        return
    
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате `ДД.ММ` (например: 15.03)",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    logger.debug(f"[process_return_date] Введена дата возврата: {message.text}")
    
    # Проверка порядка дат
    data = await state.get_data()
    depart_date = data.get("depart_date")
    
    if depart_date and not validate_dates_order(depart_date, message.text):
        logger.warning(f"[process_return_date] ОТКЛОНЕНО: возврат раньше вылета "
                      f"(вылет={depart_date}, возврат={message.text})")
        await message.answer(
            "❌ Дата возврата не может быть раньше даты вылета!\n"
            f"Вылет: {depart_date}, Возврат: {message.text}\n"
            "Пожалуйста, введите корректную дату.",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        return
    
    await state.update_data(return_date=message.text)
    logger.info(f"[process_return_date] Дата возврата сохранена: {message.text}")
    
    await ask_flight_type(message, state)

@router.callback_query(FlightSearch.flight_type, F.data.startswith("flight_type_"))
async def process_flight_type(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора типа рейса"""
    flight_type = callback.data.split("_")[2]  # direct, transfer, all
    
    logger.debug(f"[process_flight_type] Выбран тип рейса: {flight_type}")
    
    await state.update_data(flight_type=flight_type)
    
    # Показываем подтверждение
    await confirm_search(callback.message, state)
    await callback.answer()

async def confirm_search(message: Message, state: FSMContext):
    """Показываем подтверждение поиска"""
    data = await state.get_data()
    
    # Формируем текст подтверждения
    origin_name = data["origin_name"]
    dest_name = data["dest_name"]
    depart_date = data["depart_date"]
    return_date = data.get("return_date")
    flight_type = data.get("flight_type", "all")
    
    if return_date:
        dates_text = f"{depart_date} → {return_date}"
    else:
        dates_text = depart_date
    
    flight_type_text = {
        "direct": "прямые рейсы",
        "transfer": "рейсы с пересадками",
        "all": "все рейсы"
    }.get(flight_type, "все рейсы")
    
    text = (
        f"🔍 Проверка данных поиска:\n\n"
        f"📍 Маршрут: {origin_name} → {dest_name}\n"
        f"📅 Даты: {dates_text}\n"
        f"✈️ Тип рейсов: {flight_type_text}\n\n"
        "Подтвердите поиск или измените данные:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [
            InlineKeyboardButton(text="📍 Маршрут", callback_data="edit_route"),
            InlineKeyboardButton(text="📅 Даты", callback_data="edit_dates")
        ],
        [
            InlineKeyboardButton(text="✈️ Тип рейса", callback_data="edit_flight_type")
        ]
    ])
    
    await message.edit_text(text, reply_markup=kb) if hasattr(message, 'edit_text') else await message.answer(text, reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def do_search(callback: CallbackQuery, state: FSMContext):
    """Выполнение поиска рейсов"""
    data = await state.get_data()
    
    logger.info(f"[do_search] Запуск поиска с параметрами: {data}")
    
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere = data["dest"] == "везде"
    flight_type = data.get("flight_type", "all")
    direct_only = (flight_type == "direct")
    transfers_only = (flight_type == "transfer")
    
    # Пассажиры (по умолчанию 1 взрослый)
    passengers_code = "1"
    
    if is_origin_everywhere and not is_dest_everywhere:
        all_flights = await search_origin_everywhere(
            dest_iata=data["dest_iata"],
            depart_date=data["depart_date"],
            return_date=data.get("return_date"),
            passengers_code=passengers_code
        )
        search_type = "origin_everywhere"
    elif not is_origin_everywhere and is_dest_everywhere:
        all_flights = await search_dest_everywhere(
            origin_iata=data["origin_iata"],
            depart_date=data["depart_date"],
            return_date=data.get("return_date"),
            passengers_code=passengers_code
        )
        search_type = "dest_everywhere"
    else:
        origins = [data["origin_iata"]]
        destinations = [data["dest_iata"]]
        
        all_flights = await search_flights(
            origins=origins,
            destinations=destinations,
            depart_date=data["depart_date"],
            return_date=data.get("return_date"),
            passengers_code=passengers_code
        )
        search_type = "regular"
    
    # Фильтрация по типу рейса
    if direct_only:
        all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
    elif transfers_only:
        all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
    
    if not all_flights:
        # Проверяем, был ли запрос на прямые рейсы
        if direct_only:
            # Предлагаем поиск с пересадками
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Показать с пересадками", 
                                    callback_data=f"retry_with_transfers_{search_type}")],
                [InlineKeyboardButton(text="🔄 Изменить поиск", callback_data="edit_search")]
            ])
            await callback.message.edit_text(
                "😔 Прямых рейсов на эти даты не найдено.\n"
                "Хотите посмотреть варианты с пересадками? Они часто дешевле!",
                reply_markup=kb
            )
            await callback.answer()
            return
        
        # Обычное сообщение об отсутствии рейсов
        origin_iata = data["origin_iata"]
        dest_iata = data["dest_iata"]
        d1 = format_avia_link_date(data["depart_date"])
        d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
        route = f"{origin_iata}{d1}{dest_iata}{d2}{passengers_code}"
        clean_link = f"https://www.aviasales.ru/search/{route}"
        partner_link = await convert_to_partner_link(clean_link)
        
        await callback.message.edit_text(
            f"😔 Рейсов по маршруту не найдено.\n\n"
            "Попробуйте:\n"
            "• Изменить даты\n"
            "• Поискать на другие дни\n"
            "• Посмотреть варианты с пересадками\n\n"
            f"[Посмотреть на Aviasales]({partner_link})",
            parse_mode="Markdown"
        )
        await callback.answer()
        return
    
    # Формирование и отправка результатов
    response = []
    for i, flight in enumerate(all_flights[:3], 1):
        price = flight['price']
        transfers = flight.get('transfers', 0)
        direct = "прямой" if transfers == 0 else f"с {transfers} пересадкой(ами)"
        response.append(
            f"{i}. {price} ₽ • {direct}\n"
            f"   {flight['airline']} • {flight['departure']} → {flight['arrival']}"
        )
    
    if data.get("return_date"):
        dates_text = f"{data['depart_date']} → {data['return_date']}"
    else:
        dates_text = data['depart_date']
    
    response_text = (
        f"✈️ {data['origin_name']} → {data['dest_name']} | {dates_text}\n\n" +
        "\n".join(response) +
        f"\n\n[Посмотреть все варианты](https://www.aviasales.ru/...)"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Уведомить о снижении цены", callback_data="watch_price")],
        [InlineKeyboardButton(text="🔄 Изменить поиск", callback_data="edit_search")]
    ])
    
    await callback.message.edit_text(response_text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()
    await state.clear()

@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    """Редактирование шага поиска"""
    step = callback.data.split("_")[1]
    
    if step == "route":
        await callback.message.edit_text(
            "📍 Введите маршрут: `Город - Город`",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.route)
    elif step == "dates":
        await callback.message.edit_text(
            "📅 Введите дату вылета: `ДД.ММ`",
            parse_mode="HTML",
            reply_markup=CANCEL_KB
        )
        await state.set_state(FlightSearch.depart_date)
    elif step == "flight_type":
        await ask_flight_type(callback.message, state)
    
    await callback.answer()

@router.callback_query(FlightSearch.confirm, F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    """Повторный поиск с пересадками с использованием сохраненных параметров"""
    # Получаем данные из состояния (сохраненные при первом поиске)
    data = await state.get_data()
    
    # Обновляем тип рейса на "all" (чтобы включить рейсы с пересадками)
    await state.update_data(flight_type="all")
    
    # Вызываем подтверждение поиска снова с обновленным состоянием
    # Это запустит новый поиск с теми же параметрами, но без фильтрации по прямым рейсам
    await confirm_search(callback, state)
    await callback.answer()
# ===== Отслеживание цен =====
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

# ===== Трансферы =====
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

# ===== ГЛОБАЛЬНЫЙ ОБРАБОТЧИК =====
@router.message(F.text)
async def handle_any_message(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state:
        await message.answer(
            "Пожалуйста, завершите текущий поиск или отмените его через кнопку ↩️ В начало",
            reply_markup=CANCEL_KB
        )
        return
    if message.text.startswith("/"):
        return
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

# ===== ОБРАБОТЧИК ПОВТОРНОГО ПОИСКА С ПЕРЕСАДКАМИ =====
@router.callback_query(F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    """Повторный поиск с пересадками с использованием сохраненных параметров"""
    # Получаем данные из состояния (сохраненные при первом поиске)
    data = await state.get_data()
    
    if not data:
        # Если состояние пустое (редкий случай), показываем сообщение об ошибке
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")]
        ])
        await callback.message.edit_text(
            "😔 Данные поиска устарели. Пожалуйста, выполните новый поиск.",
            reply_markup=kb
        )
        await callback.answer()
        return
    
    # Обновляем тип рейса на "all" (чтобы искать все рейсы, включая с пересадками)
    await state.update_data(flight_type="all")
    
    # Вызываем подтверждение поиска снова с обновленным состоянием
    # Это запустит новый поиск с теми же параметрами, но без фильтрации по прямым рейсам
    await confirm_search(callback, state)
    
    await callback.answer()