# handlers/start.py
import re
from aiogram import Router, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from services.flight_search import search_one_way, generate_booking_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY


router = Router()

def parse_passengers(s: str) -> str:
    if not s:
        return "1"  # по умолчанию 1 взрослый
    if s.isdigit():
        return s
    # Пример: "2 взр, 1 реб, 1 мл"
    adults, children, infants = 0, 0, 0
    for part in s.split(","):
        part = part.strip().lower()
        if "взр" in part or "взросл" in part:
            adults = int(re.search(r"\d+", part).group() or 1)
        elif "реб" in part or "дет" in part:
            children = int(re.search(r"\d+", part).group() or 1)
        elif "мл" in part or "млад" in part:
            infants = int(re.search(r"\d+", part).group() or 1)
    return str(adults) + (str(children) if children else "") + (str(infants) if infants else "")

@router.message()
async def handle_flight_request(message: types.Message):
    text = message.text.strip().lower()
    
    # Парсинг: "москва → дубай 15.03 [2 взр, 1 реб]" или "москва → дубай 15.03 2"
    match = re.search(
        r"([а-яёa-z\s]+?)(?:\s*[-→>]\s*)([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})\s*(.*)?",
        text,
        re.IGNORECASE
    )

    if not match:
        await message.answer("Напишите: <b>Москва → Бангкок 15.03 2</b>\n\n<i>Формат пассажиров: 1, или 21 (2 взрослых, 1 ребенок), или 121 (1 взрослый, 2 ребенка, 1 младенец)</i>", parse_mode="HTML")
        return

    origin_city, dest_city, date, passengers_part = match.groups()
    dest_iata = CITY_TO_IATA.get(dest_city)
    passengers_code = parse_passengers((passengers_part or "").strip())
    
    # Описание пассажиров для вывода
    desc_parts = []
    try:
        ad = int(passengers_code[0]) if passengers_code else 1
        ch = int(passengers_code[1]) if len(passengers_code) > 1 else 0
        inf = int(passengers_code[2]) if len(passengers_code) > 2 else 0
        if ad: desc_parts.append(f"{ad} взр.")
        if ch: desc_parts.append(f"{ch} реб.")
        if inf: desc_parts.append(f"{inf} мл.")
    except:
        desc_parts = ["1 взр."]
    passengers_desc = ", ".join(desc_parts)

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
    response = f"✅ Найдено ({passengers_desc}):\n"
    for i, f in enumerate(top_flights, 1):
        price = f.get("value") or f.get("price") or "?"
        departure = f.get("departure_at", "")[:10] if f.get("departure_at") else "?"
        # Получаем полные названия городов
        origin_city_name = IATA_TO_CITY.get(f["origin"], f["origin"])
        dest_city_name = IATA_TO_CITY.get(dest_iata, dest_iata)
        response += f'{i}. ✈️ {origin_city_name} → {dest_city_name} — от {price} ₽ — {departure}\n'
    
    await message.answer(response)

    # Кнопки для всех топ-рейсов
    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for i, f in enumerate(top_flights, 1):
        price = f.get("value") or f.get("price") or "?"
        origin_name = IATA_TO_CITY.get(f["origin"], f["origin"])
        dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
        link = generate_booking_link(f, f["origin"], dest_iata, date, passengers_code)
        btn_text = f"✈️ от {price} ₽ — {origin_city_name}→{dest_city_name}"
        keyboard.inline_keyboard.append([
            InlineKeyboardButton(text=btn_text, url=link)
        ])

    await message.answer("Выберите предложение:", reply_markup=keyboard)
