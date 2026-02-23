# handlers/flystack_track.py
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, date
import re
import os
from services.flystack_client import flystack_client, format_flight_details
from utils.redis_client import redis_client
from utils.cities import IATA_TO_CITY
from utils.logger import logger

router = Router()

def convert_date_to_api_format(date_str: str) -> str:
    """Конвертирует дату из формата ДД.ММ в YYYY-MM-DD для FlyStack API.
    Год подставляется текущий или следующий, если дата уже прошла.
    """
    try:
        day, month = map(int, date_str.split("."))
        today = date.today()
        year = today.year
        candidate = date(year, month, day)
        if candidate < today:
            year += 1
        return date(year, month, day).strftime("%Y-%m-%d")
    except Exception:
        return date_str

class FlyStackTrack(StatesGroup):
    flight_number = State()
    depart_date = State()
    confirm = State()
    
    
def build_history(data: dict) -> str:
    """Строит историю выборов в правильном порядке"""
    history = ""
    
    # 1. Маршрут
    if data.get("origin_name") and data.get("dest_name"):
        history += f"📍 Маршрут: {data['origin_name']} → {data['dest_name']}\n"
    
    # 2. Дата вылета
    if data.get("depart_date"):
        history += f"📅 Вылет: {data['depart_date']}\n"
    
    # 3. Дата возврата (если есть)
    if data.get("need_return", False) and data.get("return_date"):
        history += f"↩️ Возврат: {data['return_date']}\n"
    
    # 4. Тип рейса
    if data.get("flight_type"):
        flight_type_text = {
            "direct": "✈️ Прямые рейсы",
            "transfer": "🔄 С пересадками",
            "all": "ℹ️ Все рейсы"
        }.get(data["flight_type"], "Неизвестный тип")
        history += f"{flight_type_text}\n"
    
    # 5. Пассажиры
    if data.get("adults", 0) > 0:
        passenger_desc = f"👥 {data['adults']} взр"
        if data.get("children", 0) > 0:
            passenger_desc += f", {data['children']} дет"
        if data.get("infants", 0) > 0:
            passenger_desc += f", {data['infants']} мл"
        history += f"{passenger_desc}\n"
    
    return history

@router.callback_query(F.data == "track_flight")
async def start_track_flight(callback: CallbackQuery, state: FSMContext):
    """Начало отслеживания рейса"""
    # ← ЛОГИРОВАНИЕ
    logger.info(f"🔍 [FlyStack] Пользователь {callback.from_user.id} нажал кнопку 'track_flight'")
    logger.debug(f"📝 [FlyStack] Текущее состояние FSM: {await state.get_state()}")
    
    # ← Очищаем состояние перед началом
    await state.clear()
    logger.debug(f"📝 [FlyStack] Состояние очищено: {await state.get_state()}")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    
    try:
        await callback.message.edit_text(
            "📊 <b>Информация о рейсе</b>\n\n"
            "Введите номер рейса (например, <code>SU381</code> или <code>Аэрофлот 381</code>):",
            parse_mode="HTML",
            reply_markup=kb
        )
        logger.info(f"✅ [FlyStack] Сообщение отправлено пользователю {callback.from_user.id}")
    except Exception as e:
        logger.error(f"❌ [FlyStack] Ошибка при edit_text: {e}")
        await callback.message.answer(
            "📊 <b>Информация о рейсе</b>\n\n"
            "Введите номер рейса:",
            parse_mode="HTML",
            reply_markup=kb
        )
    
    await state.set_state(FlyStackTrack.flight_number)
    logger.debug(f"📝 [FlyStack] Установлено состояние: {await state.get_state()}")
    await callback.answer()

@router.message(FlyStackTrack.flight_number)
async def process_flight_number(message: Message, state: FSMContext):
    """Обработка номера рейса"""
    logger.info(f"🔍 [FlyStack] Пользователь {message.from_user.id} ввёл номер рейса: {message.text}")
    
    text = message.text.strip().upper()
    airline_map = {
        "АЭРОФЛОТ": "SU", "AEROFLOT": "SU",
        "S7": "S7", "С7": "S7",
        "ПОБЕДА": "DP", "POBEDA": "DP",
        "УРАЛЬСКИЕ": "U6", "URAL": "U6",
        "РОССИЯ": "FV", "ROSSIYA": "FV"
    }

    airline = None
    flight_num = None

    for key, code in airline_map.items():
        if key in text:
            airline = code
            match = re.search(r'(\d{1,4})', text)
            if match:
                flight_num = match.group(1)
            break

    if not airline or not flight_num:
        # Поддерживаем форматы: SU1136, SU 1136, SU-1136
        match = re.match(r'^([A-Z]{2})\s*[-]?\s*(\d{1,4})$', text)
        if match:
            airline = match.group(1)
            flight_num = match.group(2)
        else:
            logger.warning(f"⚠️ [FlyStack] Неверный формат номера рейса: {text}")
            await message.answer(
                "❌ Неверный формат.\n\n"
                "Примеры:\n"
                "• <code>SU381</code>\n"
                "• <code>Аэрофлот 381</code>\n"
                "• <code>S7 123</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
                ])
            )
            return

    logger.info(f"✅ [FlyStack] Распознан рейс: {airline}{flight_num}")
    await state.update_data(airline=airline, flight_number=flight_num)

    await message.answer(
        f"✅ Рейс: <b>{airline}{flight_num}</b>\n\n"
        "📅 Введите дату вылета в формате <code>ДД.ММ</code>:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
    )
    await state.set_state(FlyStackTrack.depart_date)
    logger.debug(f"📝 [FlyStack] Установлено состояние: {await state.get_state()}")

@router.message(FlyStackTrack.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    """Обработка даты вылета"""
    logger.info(f"🔍 [FlyStack] Пользователь {message.from_user.id} ввёл дату: {message.text}")
    
    date_text = message.text.strip()
    if not re.match(r'^\d{1,2}\.\d{1,2}$', date_text):
        logger.warning(f"⚠️ [FlyStack] Неверный формат даты: {date_text}")
        await message.answer(
            "❌ Неверный формат. Пример: <code>15.03</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(depart_date=date_text)
    data = await state.get_data()
    airline = data["airline"]
    flight_num = data["flight_number"]
    
    logger.info(f"✅ [FlyStack] Данные сохранены: {airline}{flight_num} на {date_text}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, продолжить", callback_data="confirm_track")],
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit_track")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])

    await message.answer(
        f"📋 <b>Подтвердите данные:</b>\n"
        f"✈️ Рейс: {airline}{flight_num}\n"
        f"📅 Дата: {date_text}\n\n"
        f"Проверить детали рейса?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlyStackTrack.confirm)
    logger.debug(f"📝 [FlyStack] Установлено состояние: {await state.get_state()}")

@router.callback_query(FlyStackTrack.confirm, F.data == "confirm_track")
async def confirm_track(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и запрос к FlyStack"""
    logger.info(f"🔍 [FlyStack] Пользователь {callback.from_user.id} подтвердил поиск")
    
    data = await state.get_data()
    user_id = callback.from_user.id
    
    logger.debug(f"📝 [FlyStack] Данные из состояния: {data}")
    
    # Проверяем лимит использования
    current_month = datetime.now().strftime("%Y-%m")
    usage = await redis_client.get_flystack_usage(user_id, current_month)
    free_limit = int(os.getenv("FLYSTACK_FREE_LIMIT", "3"))
    
    logger.info(f"📊 [FlyStack] Использование: {usage}/{free_limit} за {current_month}")

    if usage >= free_limit:
        logger.warning(f"⚠️ [FlyStack] Лимит исчерпан для пользователя {user_id}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text(
            f"❌ <b>Лимит бесплатных запросов исчерпан</b>\n\n"
            f"Вы использовали {usage} из {free_limit} запросов в этом месяце.\n"
            f"💡 Лимит сбросится 1-го числа следующего месяца.",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text("⏳ Загружаем информацию о рейсе...")
    logger.info(f"🔍 [FlyStack] Запрос к API: {data['airline']}{data['flight_number']} на {data['depart_date']}")

    # Конвертируем дату ДД.ММ → YYYY-MM-DD для API
    api_date = convert_date_to_api_format(data["depart_date"])
    logger.info(f"📅 [FlyStack] Дата для API: {api_date}")

    # Запрашиваем детали у FlyStack
    details = await flystack_client.get_flight_details(
        airline=data["airline"],
        flight_number=data["flight_number"],
        departure_date=api_date
    )
    
    logger.debug(f"📝 [FlyStack] Ответ от API: {details}")

    if not details:
        logger.error(f"❌ [FlyStack] Не удалось получить данные о рейсе")
        await callback.message.edit_text(
            "❌ Не удалось получить информацию о рейсе.\n"
            "Проверьте номер рейса и дату, или попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        return

    if details.get("error") == "rate_limit":
        logger.warning(f"⚠️ [FlyStack] Превышен лимит API")
        await callback.message.edit_text(
            "⚠️ Сервис временно перегружен. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        return

    # Форматируем и показываем ответ
    formatted = format_flight_details(details)
    aircraft = details.get("aircraft_type", "Не указано")
    
    logger.info(f"✅ [FlyStack] Данные получены: самолёт {aircraft}")

    # Увеличиваем счётчик использования
    await redis_client.increment_flystack_usage(user_id, current_month, free_limit)
    logger.info(f"📊 [FlyStack] Счётчик увеличен: {usage + 1}/{free_limit}")

    # Кнопки
    kb_buttons = [
        [InlineKeyboardButton(text="🔔 Отслеживать этот рейс", callback_data=f"subscribe_track:{data['airline']}{data['flight_number']}:{data['depart_date']}")],
        [InlineKeyboardButton(text="📊 Информация об авиакомпании", callback_data=f"airline_info:{data['airline']}")],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await callback.message.edit_text(
        f"✈️ <b>Рейс {data['airline']}{data['flight_number']}</b>\n"
        f"📅 {data['depart_date']}\n\n"
        f"✈️ <b>Самолёт:</b> {aircraft}\n\n"
        f"{formatted}",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.clear()
    logger.info(f"✅ [FlyStack] Поиск завершён успешно")
    await callback.answer()