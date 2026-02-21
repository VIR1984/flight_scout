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
from utils.cities import IATA_TO_CITY
from utils.logger import logger

router = Router()

class FlyStackTrack(StatesGroup):
    flight_number = State()
    depart_date = State()
    confirm = State()

@router.callback_query(F.data == "track_flight")
async def start_track_flight(callback: CallbackQuery, state: FSMContext):
    """Начало отслеживания рейса"""
    await state.clear() 
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
    ])
    
    await callback.message.edit_text(
        "📊 <b>Информация о рейсе</b>\n\n"
        "Введите номер рейса (например, <code>SU381</code> или <code>Аэрофлот 381</code>):",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlyStackTrack.flight_number)
    await callback.answer()

@router.message(FlyStackTrack.flight_number)
async def process_flight_number(message: Message, state: FSMContext):
    """Обработка номера рейса"""
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
    """Обработка даты вылета"""
    date_text = message.text.strip()
    
    if not re.match(r'^\d{1,2}\.\d{1,2}$', date_text):
        await message.answer(
            "❌ Неверный формат. Пример: <code>15.03</code>",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(depart_date=date_text)
    
    data = await state.get_data()
    airline = data["airline"]
    flight_num = data["flight_number"]
    
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

@router.callback_query(FlyStackTrack.confirm, F.data == "confirm_track")
async def confirm_track(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и запрос к FlyStack"""
    data = await state.get_data()
    user_id = callback.from_user.id
    
    # Проверяем лимит использования
    current_month = datetime.now().strftime("%Y-%m")
    usage = await redis_client.get_flystack_usage(user_id, current_month)
    free_limit = int(os.getenv("FLYSTACK_FREE_LIMIT", "3"))
    
    if usage >= free_limit:
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
    
    # Запрашиваем детали у FlyStack
    details = await flystack_client.get_flight_details(
        airline=data["airline"],
        flight_number=data["flight_number"],
        departure_date=data["depart_date"]
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
        return
    
    if details.get("error") == "rate_limit":
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
    
    # Увеличиваем счётчик использования
    await redis_client.increment_flystack_usage(user_id, current_month, free_limit)
    
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
    await callback.answer()

@router.callback_query(FlyStackTrack.confirm, F.data == "edit_track")
async def edit_track(callback: CallbackQuery, state: FSMContext):
    """Редактирование данных"""
    await callback.message.edit_text(
        "📊 <b>Информация о рейсе</b>\n\n"
        "Введите номер рейса:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
    )
    await state.set_state(FlyStackTrack.flight_number)
    await callback.answer()

@router.callback_query(F.data.startswith("airline_info:"))
async def show_airline_info(callback: CallbackQuery):
    """Показать информацию об авиакомпании"""
    airline_iata = callback.data.split(":")[1]
    
    await callback.answer("⏳ Загружаем информацию...")
    
    airline_info = await flystack_client.get_airline(airline_iata)
    fleet = await flystack_client.get_airline_fleet(airline_iata)
    
    if not airline_info:
        await callback.message.answer(
            "❌ Не удалось получить информацию об авиакомпании",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data="track_flight")]
            ])
        )
        return
    
    text = f"✈️ <b>{airline_info.get('name', airline_iata)}</b>\n"
    text += f"🌍 Страна: {airline_info.get('country', 'Не указано')}\n"
    text += f"🔢 IATA: {airline_iata}\n"
    
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