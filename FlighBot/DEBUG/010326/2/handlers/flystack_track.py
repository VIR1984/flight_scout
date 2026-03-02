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
    """Начало отслеживания рейса — ручной ввод номера."""
    await state.clear()

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
    except Exception:
        await callback.message.answer(
            "📊 <b>Информация о рейсе</b>\n\nВведите номер рейса:",
            parse_mode="HTML",
            reply_markup=kb
        )

    await state.set_state(FlyStackTrack.flight_number)
    await callback.answer()


@router.callback_query(F.data.startswith("track_flight_direct:"))
async def track_flight_direct(callback: CallbackQuery, state: FSMContext):
    """
    Вызов из результатов поиска — airline, flight_number и дата уже известны,
    пропускаем ввод и сразу показываем информацию о рейсе.
    Формат callback_data: track_flight_direct:{airline}:{flight_number}:{depart_date}
    """
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка данных рейса", show_alert=True)
        return

    airline     = parts[1]
    flight_num  = parts[2]
    depart_date = parts[3]  # формат ДД.ММ

    await state.clear()
    await _fetch_and_show_flight(
        callback=callback,
        state=state,
        airline=airline,
        flight_number=flight_num,
        depart_date_display=depart_date,
    )

@router.message(FlyStackTrack.flight_number)
async def process_flight_number(message: Message, state: FSMContext):
    """Обработка номера рейса."""
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
        match = re.match(r'^([A-Z]{2})\s*(\d{1,4})$', text)
        if match:
            airline = match.group(1)
            flight_num = match.group(2)
        else:
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


@router.message(FlyStackTrack.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    """Обработка даты вылета."""
    date_text = message.text.strip()
    if not re.match(r'^\d{1,2}\.\d{1,2}$', date_text):
        await message.answer(
            "❌ Неверный формат. Пример: <code>15.03</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(depart_date=date_text)
    data = await state.get_data()
    airline    = data["airline"]
    flight_num = data["flight_number"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, продолжить", callback_data="confirm_track")],
        [InlineKeyboardButton(text="✏️ Изменить данные", callback_data="edit_track")],
        [InlineKeyboardButton(text="↩️ В меню",          callback_data="main_menu")]
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


@router.callback_query(FlyStackTrack.confirm, F.data == "confirm_track")
async def confirm_track(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и запрос к FlyStack."""
    data = await state.get_data()
    await _fetch_and_show_flight(
        callback=callback,
        state=state,
        airline=data["airline"],
        flight_number=data["flight_number"],
        depart_date_display=data["depart_date"],
    )


async def _fetch_and_show_flight(
    callback: CallbackQuery,
    state: FSMContext,
    airline: str,
    flight_number: str,
    depart_date_display: str,  # ДД.ММ
):
    """
    Общая логика: проверяет лимит, запрашивает FlyStack API,
    форматирует и отправляет результат.
    Используется и из confirm_track, и из track_flight_direct.
    """
    user_id = callback.from_user.id
    current_month = datetime.now().strftime("%Y-%m")
    usage = await redis_client.get_flystack_usage(user_id, current_month)
    free_limit = int(os.getenv("FLYSTACK_FREE_LIMIT", "3"))

    if usage >= free_limit:
        await callback.message.edit_text(
            f"❌ <b>Лимит бесплатных запросов исчерпан</b>\n\n"
            f"Использовано {usage} из {free_limit} в этом месяце.\n"
            f"💡 Лимит сбрасывается 1-го числа следующего месяца.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text("⏳ Загружаем информацию о рейсе...")

    # Конвертируем ДД.ММ → ГГГГ-ММ-ДД с автоопределением года
    try:
        day, month = map(int, depart_date_display.split('.'))
        now = datetime.now()
        year = now.year if month >= now.month else now.year + 1
        api_date = f"{year}-{month:02d}-{day:02d}"
    except Exception:
        await callback.message.edit_text(
            "❌ Ошибка в дате. Попробуйте ввести ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        await callback.answer()
        return

    details = await flystack_client.get_flight_details(
        airline=airline,
        flight_number=flight_number,
        departure_date=api_date
    )

    if not details:
        await callback.message.edit_text(
            "❌ Не удалось получить информацию о рейсе.\n"
            "Проверьте номер рейса и дату, или попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        await callback.answer()
        return

    if details.get("error") == "rate_limit":
        await callback.message.edit_text(
            "⚠️ Сервис временно перегружен. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
            ])
        )
        await state.clear()
        await callback.answer()
        return

    # Увеличиваем счётчик использования
    await redis_client.increment_flystack_usage(user_id, current_month, free_limit)

    formatted = format_flight_details(details)
    aircraft   = details.get("aircraft_type", "Не указано")

    message_text = (
        f"✈️ <b>Рейс {airline}{flight_number}</b>\n"
        f"📅 {depart_date_display}\n\n"
        f"✈️ <b>Самолёт:</b> {aircraft}\n\n"
        f"{formatted}"
    )

    if details.get("status") == "delayed" and details.get("delay_minutes"):
        message_text = "⚠️ <b>ВНИМАНИЕ! Задержка рейса</b> ⚠️\n\n" + message_text

    # Остаток бесплатных запросов
    remaining = free_limit - (usage + 1)
    if remaining >= 0:
        message_text += f"\n\n<i>💡 Осталось бесплатных запросов в этом месяце: {remaining}</i>"

    kb_buttons = [
        # subscribe_track: используем : как разделитель airline и flight_number раздельно
        [InlineKeyboardButton(
            text="🔔 Отслеживать этот рейс",
            callback_data=f"subscribe_track:{airline}:{flight_number}:{depart_date_display}"
        )],
        [InlineKeyboardButton(
            text="📊 Информация об авиакомпании",
            callback_data=f"airline_info:{airline}"
        )],
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")],
    ]

    await callback.message.edit_text(
        message_text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )
    await state.clear()
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
    """
    Подписка на уведомления о статусе рейса.
    callback_data формат: subscribe_track:{airline}:{flight_number}:{depart_date}
    Пример:               subscribe_track:SU:381:15.03
    """
    parts = callback.data.split(":")
    # parts[0] = "subscribe_track", [1] = airline, [2] = flight_number, [3] = date
    if len(parts) < 4:
        await callback.answer("❌ Ошибка данных для подписки", show_alert=True)
        return

    airline     = parts[1]
    flight_num  = parts[2]
    depart_date = parts[3]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подписаться",
            callback_data=f"confirm_subscribe:{airline}:{flight_num}:{depart_date}"
        )],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_subscribe")]
    ])

    await callback.message.edit_text(
        f"🔔 <b>Подписка на рейс {airline}{flight_num}</b>\n\n"
        "Вы будете получать уведомления о:\n"
        "• Изменении статуса рейса\n"
        "• Задержках и отменах\n"
        "• Изменении гейта вылета\n"
        "• Времени вылета и прилёта",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_subscribe:"))
async def confirm_subscription(callback: CallbackQuery):
    """
    Подтверждение подписки — сохраняем в Redis.
    callback_data: confirm_subscribe:{airline}:{flight_number}:{depart_date}
    """
    parts = callback.data.split(":")
    if len(parts) < 4:
        await callback.answer("❌ Ошибка данных", show_alert=True)
        return

    airline     = parts[1]
    flight_num  = parts[2]
    depart_date = parts[3]
    user_id     = callback.from_user.id

    await redis_client.save_flight_track_subscription(
        user_id=user_id,
        airline=airline,
        flight_number=flight_num,
        depart_date=depart_date,
    )

    await callback.message.edit_text(
        f"✅ <b>Подписка оформлена!</b>\n\n"
        f"Рейс: {airline}{flight_num}  ·  {depart_date}\n\n"
        "Вы получите уведомление при изменении статуса рейса.",
        parse_mode="HTML",
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