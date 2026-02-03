# handlers/start.py
import re
from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.flight_search import search_one_way, generate_booking_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS


router = Router()

@router.message()
async def handle_flight_request(message: types.Message):
    text = message.text.strip().lower()
    
    # Парсинг: "москва → дубай 15.03"
    match = re.search(
        r"([а-яёa-z\s]+?)(?:\s*[-→>]\s*)([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})",
        text,
        re.IGNORECASE
    )

    if not match:
        await message.answer("Напишите запрос как: <b>Москва → Дубай 15.03</b>", parse_mode="HTML")
        return

    origin_city, dest_city, date = match.groups()
    dest_iata = CITY_TO_IATA.get(dest_city)
    
    if origin_city == "везде":
        origins = GLOBAL_HUBS
    else:
        orig_iata = CITY_TO_IATA.get(origin_city)
        if not orig_iata:
            await message.answer(f"Не знаю город: {origin_city}")
            return
        origins = [orig_iata]

    if not dest_iata:
        await message.answer(f"Не знаю город: {dest_city}")
        return

    await message.answer("Ищу билеты...")

    all_flights = []
    for orig in origins:
        flights = await search_one_way(orig, dest_iata, date)
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)

    if not all_flights:
        await message.answer("Билеты не найдены 😢")
        return

    # Сортируем по цене
    def get_price(flight):
        return flight.get("value") or flight.get("price") or 999999

    all_flights.sort(key=get_price)
    top_flights = all_flights[:3]

    # Формируем текстовое сообщение без ссылок
    response = "✅ Найдено:\n\n"
    for i, f in enumerate(top_flights, 1):
        price = f.get("value") or f.get("price") or "?"
        airline = f.get("airline", "?")
        departure = f.get("departure_at", "")[:10] if f.get("departure_at") else "?"
        response += f'{i}. ✈️ {airline} — ${price} — {departure}\n'

    await message.answer(response)

    # Отправляем кнопку для первого (самого дешёвого) рейса
    first_flight = top_flights[0]
    link = generate_booking_link(first_flight, first_flight["origin"], dest_iata, date)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Забронировать на Aviasales", url=link)]
    ])
    await message.answer("Выберите предложение:", reply_markup=keyboard)