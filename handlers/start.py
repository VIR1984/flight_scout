# handlers/start.py
import re
from aiogram import Router, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from services.flight_search import search_flights, generate_booking_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY

router = Router()

def parse_passengers(s: str) -> str:
    if not s:
        return "1"
    if s.isdigit():
        return s
    adults = children = infants = 0
    for part in s.split(","):
        part = part.strip().lower()
        n = int(re.search(r"\d+", part).group()) if re.search(r"\d+", part) else 1
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
    return str(adults) + (str(children) if children else "") + (str(infants) if infants else "")

def build_passenger_desc(code: str):
    try:
        ad = int(code[0])
        ch = int(code[1]) if len(code) > 1 else 0
        inf = int(code[2]) if len(code) > 2 else 0
        parts = []
        if ad: parts.append(f"{ad} взр.")
        if ch: parts.append(f"{ch} реб.")
        if inf: parts.append(f"{inf} мл.")
        return parts or ["1 взр."]
    except:
        return ["1 взр."]

@router.message(F.text == "/start")
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✈️ Только туда", callback_data="type_oneway")],
        [InlineKeyboardButton(text="🔁 Туда-обратно", callback_data="type_roundtrip")]
    ])
    await message.answer("Выберите тип поиска:", reply_markup=kb)

@router.callback_query(F.data.startswith("type_"))
async def handle_search_type(callback: CallbackQuery):
    await callback.answer()
    is_round = callback.data == "type_roundtrip"
    example = "Сингапур → Гонконг 06.03 – 07.03" if is_round else "Сингапур → Гонконг 06.03"
    hint = "\n\nДля обратного рейса укажите две даты через «–» (тире или дефис)." if is_round else ""
    await callback.message.answer(
        f"Отправьте запрос в формате:\n<b>{example}</b>{hint}\n\n"
        "Можно указать пассажиров в конце: <code>2</code> или <code>21</code>",
        parse_mode="HTML"
    )

@router.message()
async def handle_flight_request(message: Message):
    text = message.text.strip().lower()

    # Попытка распознать round-trip: "город → город дд.мм – дд.мм"
    round_match = re.search(
        r"([а-яёa-z\s]+?)(?:\s*[-→>]\s*)([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})\s*[-–]\s*(\d{1,2}\.\d{1,2})\s*(.*)?",
        text, re.IGNORECASE
    )

    if round_match:
        origin_city, dest_city, depart_date, return_date, passengers_part = round_match.groups()
        is_roundtrip = True
    else:
        oneway_match = re.search(
            r"([а-яёa-z\s]+?)(?:\s*[-→>]\s*)([а-яёa-z\s]+?)\s+(\d{1,2}\.\d{1,2})\s*(.*)?",
            text, re.IGNORECASE
        )
        if not oneway_match:
            await message.answer("Неверный формат. Нажмите /start и выберите тип поиска.")
            return
        origin_city, dest_city, depart_date, passengers_part = oneway_match.groups()
        return_date = None
        is_roundtrip = False

    dest_iata = CITY_TO_IATA.get(dest_city)
    if not dest_iata:
        await message.answer(f"Не знаю город: {dest_city}")
        return

    passengers_code = parse_passengers((passengers_part or "").strip())
    passenger_desc = ", ".join(build_passenger_desc(passengers_code))

    if origin_city == "везде":
        origins = GLOBAL_HUBS
    else:
        orig_iata = CITY_TO_IATA.get(origin_city)
        if not orig_iata:
            await message.answer(f"Не знаю город: {origin_city}")
            return
        origins = [orig_iata]

    await message.answer("Ищу билеты...")

    all_flights = []
    for orig in origins:
        flights = await search_flights(orig, dest_iata, depart_date, return_date)
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)

    if not all_flights:
        await message.answer("Билеты не найдены 😢")
        return

    all_flights.sort(key=lambda f: f.get("value") or f.get("price") or 999999)
    top_flights = all_flights[:3]

    response = f"✅ Найдено ({passenger_desc}):\n"
    for i, f in enumerate(top_flights, 1):
        price = f.get("value") or f.get("price") or "?"
        departure = f.get("departure_at", "")[:10] if f.get("departure_at") else "?"
        origin_name = IATA_TO_CITY.get(f["origin"], f["origin"])
        dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
        response += f'{i}. ✈️ {origin_name} → {dest_name} — от {price} ₽ — {departure}\n'
        if is_roundtrip and f.get("return_at"):
            response += f'   ↩️ Обратно: {f["return_at"][:10]}\n'

    await message.answer(response)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    for f in top_flights:
        price = f.get("value") or f.get("price") or "?"
        origin_name = IATA_TO_CITY.get(f["origin"], f["origin"])
        dest_name = IATA_TO_CITY.get(dest_iata, dest_iata)
        link = generate_booking_link(f, f["origin"], dest_iata, depart_date, passengers_code, return_date)
        btn_text = f"✈️ от {price} ₽ — {origin_name}→{dest_name}"
        keyboard.inline_keyboard.append([InlineKeyboardButton(text=btn_text, url=link)])

    await message.answer("Выберите предложение:", reply_markup=keyboard)