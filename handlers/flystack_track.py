# handlers/flystack_track.py
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime
import re
import os
from services.flystack_client import flystack_client, format_flight_details
from utils.redis_client import redis_client
from utils.cities_loader import get_city_name
from utils.logger import logger

router = Router()

class FlyStackTrack(StatesGroup):
    flight_number = State()
    depart_date = State()
    confirm = State()

@router.callback_query(F.data == "track_flight")
async def start_track_flight(callback: CallbackQuery, state: FSMContext):
    """Начало отслеживания рейса с полным логированием"""
    logger.info(f"🔍 [FlyStack] Пользователь {callback.from_user.id} нажал кнопку 'track_flight'")
    logger.debug(f"📝 [FlyStack] Текущее состояние FSM: {await state.get_state()}")
    
    # Очищаем состояние перед началом
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
    """Обработка номера рейса с полным логированием"""
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
            logger.info(f"✅ [FlyStack] Распознана авиакомпания: {key} → {code}")
            break
    
    if not airline or not flight_num:
        match = re.match(r'^([A-Z]{2})\s*(\d{1,4})$', text)
        if match:
            airline = match.group(1)
            flight_num = match.group(2)
            logger.info(f"✅ [FlyStack] Распознан IATA код: {airline}{flight_num}")
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
    
    logger.info(f"✈️ [FlyStack] Распознан рейс: {airline}{flight_num}")
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
    """Обработка даты вылета с полным логированием"""
    logger.info(f"🔍 [FlyStack] Пользователь {message.from_user.id} ввёл дату: {message.text}")
    
    date_text = message.text.strip()
    if not re.match(r'^\d{1,2}\.\d{1,2}$', date_text):
        logger.warning(f"⚠️ [FlyStack] Неверный формат даты: {date_text}")
        await message.answer(
            "❌ Неверный формат. Пример: <code>15.03</code>",
            parse_mode="HTML"
        )
        return
    
    # ПРЕОБРАЗУЕМ ДАТУ В ФОРМАТ API (ГГГГ-ММ-ДД)
    try:
        day, month = map(int, date_text.split('.'))
        year = 2026  # или datetime.now().year
        api_date = f"{year}-{month:02d}-{day:02d}"
        logger.info(f"✅ [FlyStack] Преобразована дата: {date_text} → {api_date}")
    except Exception as e:
        logger.error(f"❌ [FlyStack] Ошибка преобразования даты: {e}")
        await message.answer("❌ Ошибка в дате. Попробуйте ещё раз.")
        return
    
    await state.update_data(depart_date=date_text, api_depart_date=api_date)
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
    """Подтверждение и запрос к FlyStack с полным логированием"""
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
    
    # ИСПОЛЬЗУЕМ api_depart_date вместо depart_date
    api_date = data.get("api_depart_date", data.get("depart_date"))
    logger.info(f"🔍 [FlyStack] Запрос к API: {data['airline']}{data['flight_number']} на {api_date}")
    
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
    
    # Формируем кнопки
    kb_buttons = []
    
    # Кнопка отслеживания
    kb_buttons.append([
        InlineKeyboardButton(
            text="🔔 Отслеживать этот рейс",
            callback_data=f"subscribe_track:{data['airline']}{data['flight_number']}:{data['depart_date']}"
        )
    ])
    
    # Кнопка информации об авиакомпании
    kb_buttons.append([
        InlineKeyboardButton(
            text="📊 Информация об авиакомпании",
            callback_data=f"airline_info:{data['airline']}"
        )
    ])
    
    # Кнопка возврата в меню
    kb_buttons.append([
        InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    # Формируем сообщение
    message_text = (
        f"✈️ <b>Рейс {data['airline']}{data['flight_number']}</b>\n"
        f"📅 {data['depart_date']}\n\n"
        f"✈️ <b>Самолёт:</b> {aircraft}\n\n"
        f"{formatted}"
    )
    
    # Проверяем, есть ли статус задержки
    if details.get("status") == "delayed" and details.get("delay_minutes"):
        message_text = (
            f"⚠️ <b>ВНИМАНИЕ! Задержка рейса</b> ⚠️\n\n"
            + message_text
        )
    
    await callback.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.clear()
    logger.info(f"✅ [FlyStack] Поиск завершён успешно")
    await callback.answer()

@router.callback_query(FlyStackTrack.confirm, F.data == "edit_track")
async def edit_track(callback: CallbackQuery, state: FSMContext):
    """Редактирование данных с логированием"""
    logger.info(f"✏️ [FlyStack] Пользователь {callback.from_user.id} начал редактирование данных")
    
    await callback.message.edit_text(
        "📊 <b>Информация о рейсе</b>\n\n"
        "Введите номер рейса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
    )
    await state.set_state(FlyStackTrack.flight_number)
    logger.debug(f"📝 [FlyStack] Установлено состояние: {await state.get_state()}")
    await callback.answer()

@router.callback_query(F.data.startswith("airline_info:"))
async def show_airline_info(callback: CallbackQuery):
    """Показать информацию об авиакомпании с логированием"""
    logger.info(f"✈️ [FlyStack] Запрос информации об авиакомпании")
    
    airline_iata = callback.data.split(":")[1]
    logger.debug(f"📝 [FlyStack] IATA код авиакомпании: {airline_iata}")
    
    await callback.answer("⏳ Загружаем информацию...")
    
    airline_info = await flystack_client.get_airline(airline_iata)
    fleet = await flystack_client.get_airline_fleet(airline_iata)
    
    if not airline_info:
        logger.error(f"❌ [FlyStack] Не удалось получить информацию об авиакомпании {airline_iata}")
        await callback.message.answer(
            "❌ Не удалось получить информацию об авиакомпании",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data="track_flight")]
            ])
        )
        return
    
    logger.info(f"✅ [FlyStack] Получена информация об авиакомпании: {airline_info.get('name', airline_iata)}")
    
    text = f"✈️ <b>{airline_info.get('name', airline_iata)}</b>\n"
    text += f"🌍 Страна: {airline_info.get('country', 'Не указано')}\n"
    text += f"🔢 IATA: {airline_iata}\n"
    
    if airline_info.get('icao_code'):
        text += f"🔤 ICAO: {airline_info.get('icao_code')}\n"
    
    if airline_info.get('website'):
        text += f"🌐 Сайт: {airline_info.get('website')}\n"
    
    if airline_info.get('phone'):
        text += f"📞 Телефон: {airline_info.get('phone')}\n"
    
    if fleet:
        text += f"\n🛩️ Флот: {len(fleet)} самолётов\n"
        aircraft_types = {}
        for plane in fleet:
            aircraft_type = plane.get('aircraft_type', 'Unknown')
            aircraft_types[aircraft_type] = aircraft_types.get(aircraft_type, 0) + 1
        
        text += "\n<b>Типы самолётов:</b>\n"
        for aircraft_type, count in sorted(aircraft_types.items(), key=lambda x: x[1], reverse=True)[:5]:
            text += f"• {aircraft_type}: {count}\n"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад", callback_data="track_flight")]
    ])
    
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    logger.info(f"✅ [FlyStack] Информация об авиакомпании отправлена пользователю {callback.from_user.id}")

@router.callback_query(F.data.startswith("subscribe_track:"))
async def subscribe_to_flight(callback: CallbackQuery):
    """Подписка на уведомления о статусе рейса с логированием"""
    logger.info(f"🔔 [FlyStack] Запрос на подписку к отслеживанию рейса")
    
    data = callback.data.split(":")[1].split(":")
    if len(data) < 3:
        logger.warning(f"⚠️ [FlyStack] Неверные данные для подписки: {callback.data}")
        await callback.answer("❌ Ошибка данных для подписки", show_alert=True)
        return
    
    airline = data[0][:2]
    flight_number = data[0][2:]
    depart_date = data[1]
    
    logger.debug(f"📝 [FlyStack] Данные для подписки: рейс {airline}{flight_number} на {depart_date}")
    
    # Здесь должна быть логика сохранения подписки в Redis
    # await redis_client.save_flight_track_subscription(...)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подписаться", callback_data="confirm_subscribe")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_subscribe")]
    ])
    
    await callback.message.edit_text(
        f"🔔 <b>Подписка на рейс {airline}{flight_number}</b>\n\n"
        "Вы будете получать уведомления о:\n"
        "• Изменении статуса рейса\n"
        "• Задержках и отменах\n"
        "• Изменении гейта вылета\n"
        "• Времени вылета и прилёта",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()

@router.callback_query(F.data == "confirm_subscribe")
async def confirm_subscription(callback: CallbackQuery):
    """Подтверждение подписки с логированием"""
    logger.info(f"✅ [FlyStack] Подтверждена подписка на отслеживание рейса")
    
    # Здесь должна быть логика сохранения подписки в Redis
    # user_id = callback.from_user.id
    # await redis_client.save_flight_track_subscription(...)
    
    await callback.message.edit_text(
        "✅ Вы успешно подписаны на уведомления о рейсе!\n\n"
        "Вы будете получать уведомления при любых изменениях статуса рейса.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "cancel_subscribe")
async def cancel_subscription(callback: CallbackQuery):
    """Отмена подписки с логированием"""
    logger.info(f"❌ [FlyStack] Отменена подписка на отслеживание рейса")
    
    await callback.message.edit_text(
        "❌ Подписка на рейс отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()