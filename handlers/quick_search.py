# handlers/quick_search.py
"""
Ручной (тихий) поиск авиабилетов по тексту свободного формата.

Форматы:
  Москва Сочи 10.03
  Москва - Сочи 10.03 15.03
  Санкт-Петербург Бангкок 20.03 2 взр прямые
  Везде Стамбул 10.03
  MOW AER 15.04

Не регистрирует роутер — handle_flight_request вызывается из start.py.
Вынесен для удобства отладки и тестирования.
"""

import re
import asyncio
import logging
from uuid import uuid4

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from services.flight_search import (
    search_flights,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    find_cheapest_flight_on_exact_date,
    update_passengers_in_link,
    format_passenger_desc,
)
from utils.cities_loader import get_iata, get_city_name, CITY_TO_IATA, IATA_TO_CITY, _normalize_name
from utils.redis_client import redis_client
from utils.link_converter import convert_to_partner_link
from handlers.everywhere_search import (
    handle_everywhere_search_manual,
    format_user_date,
    build_passenger_desc,
)
from handlers.flight_constants import (
    CANCEL_KB,
    AIRPORT_NAMES,
    SUPPORTED_TRANSFER_AIRPORTS,
    MULTI_AIRPORT_CITIES,
)

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r'^\d{1,2}\.\d{1,2}$')
_IATA_RE = re.compile(r'^[A-Za-z]{3}$')

_AIRLINE_NAMES = {
    "SU": "Аэрофлот", "S7": "S7 Airlines", "DP": "Победа",
    "U6": "Уральские авиалинии", "FV": "Россия", "UT": "ЮТэйр",
    "N4": "Нордстар", "IK": "Победа",
}


# ── Вспомогательные ──────────────────────────────────────────────────────────

def _resolve_city(city_str: str) -> tuple[str | None, str | None]:
    """Принимает название или IATA. Возвращает (iata, display_name) или (None, None)."""
    c = city_str.strip()
    if _IATA_RE.match(c):
        iata = c.upper()
        name = get_city_name(iata) or IATA_TO_CITY.get(iata, iata)
        return iata, name
    iata = get_iata(c) or CITY_TO_IATA.get(_normalize_name(c))
    if iata:
        name = get_city_name(iata) or IATA_TO_CITY.get(iata, c.capitalize())
        return iata, name
    return None, None


def parse_passengers(s: str) -> str:
    """
    Парсит строку пассажиров в код для API.

    Поддерживаемые форматы:
      ''           → '1'        (1 взрослый по умолчанию)
      '3'          → '3'        (3 взрослых)
      '211'        → '211'      (2 взр, 1 реб, 1 млад) — компактный код
      '21'         → '21'       (2 взр, 1 реб)
      '2 взр, 1 реб, 1 млад'   → '211'
      '2 взр'      → '2'
    """
    if not s:
        return "1"
    s = s.strip()

    # Компактный числовой код: только цифры, 1-3 символа, первая ≥ 1
    # Примеры: "1", "2", "21", "211", "311"
    if re.match(r'^\d{1,3}$', s):
        digits = s
        adults = int(digits[0])
        if adults < 1:
            adults = 1
        # Если вся строка — 1 цифра, это просто кол-во взрослых
        if len(digits) == 1:
            return str(adults)
        # 2 цифры: взрослые + дети
        if len(digits) == 2:
            children = int(digits[1])
            result = str(adults)
            if children:
                result += str(children)
            return result
        # 3 цифры: взрослые + дети + младенцы
        if len(digits) == 3:
            children = int(digits[1])
            infants  = int(digits[2])
            result = str(adults)
            if children:
                result += str(children)
            if infants:
                result += str(infants)
            return result

    # Текстовый формат: "2 взр, 1 реб, 1 млад"
    adults = children = infants = 0
    for part in re.split(r'[,;]+', s):
        part = part.strip().lower()
        m = re.search(r"\d+", part)
        n = int(m.group()) if m else 1
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
    adults = adults or 1
    result = str(adults)
    if children:
        result += str(children)
    if infants:
        result += str(infants)
    return result


def _parse_quick_search(text: str) -> tuple | None:
    """
    Парсит строку свободного ввода.
    Возвращает (origin_city, dest_city, depart_date, return_date, passengers_part) или None.

    Алгоритм:
      1. Нормализуем разделители маршрута: " - ", "→", "—" → " | "
         (дефис ВНУТРИ слова — Санкт-Петербург — не трогаем).
      2. Токенизируем, ищем индексы дат (ДД.ММ).
      3. Нет даты → не наш формат, возвращаем None.
      4. Всё до первой даты — токены городов.
      5. Пробуем найти пару городов через get_iata или по разделителю "|".
    """
    text = text.strip()
    # Нормализуем разделители: " - " → "|", "→", "—" → "|", дефис внутри слова не трогаем
    normalized = re.sub(r'[→—>]|(?<=\s)-(?=\s)|(?<=\s)-$|^-(?=\s)', '|', text)
    normalized = re.sub(r'\s*\|\s*', ' | ', normalized)
    tokens = normalized.split()

    date_indices = [i for i, t in enumerate(tokens) if _DATE_RE.match(t)]
    if not date_indices:
        return None

    first_date_idx = date_indices[0]
    depart_date = tokens[first_date_idx]

    return_date = None
    after_depart = first_date_idx + 1
    if after_depart < len(tokens) and _DATE_RE.match(tokens[after_depart]):
        return_date = tokens[after_depart]
        passengers_part = " ".join(tokens[after_depart + 1:])
    else:
        passengers_part = " ".join(tokens[after_depart:])

    city_tokens = [t for t in tokens[:first_date_idx] if t != "|"]
    if not city_tokens:
        return None

    # Явный разделитель "|"
    pipe_pos = next(
        (i for i, t in enumerate(tokens[:first_date_idx]) if t == "|"), None
    )
    if pipe_pos is not None:
        origin_city = " ".join(t for t in tokens[:pipe_pos]).strip()
        dest_city   = " ".join(t for t in tokens[pipe_pos + 1:first_date_idx]).strip()
        if origin_city and dest_city:
            return origin_city, dest_city, depart_date, return_date, passengers_part

    # Перебор вариантов разбиения
    n = len(city_tokens)
    for split in range(1, n):
        o = " ".join(city_tokens[:split])
        d = " ".join(city_tokens[split:])
        if o.lower() == "везде" or d.lower() == "везде":
            return o, d, depart_date, return_date, passengers_part
        o_iata = get_iata(o) or CITY_TO_IATA.get(_normalize_name(o))
        d_iata = get_iata(d) or CITY_TO_IATA.get(_normalize_name(d))
        if o_iata and d_iata:
            return o, d, depart_date, return_date, passengers_part

    # Один из городов "Везде"
    if city_tokens[0].lower() == "везде" and n >= 2:
        return "везде", " ".join(city_tokens[1:]), depart_date, return_date, passengers_part
    if city_tokens[-1].lower() == "везде" and n >= 2:
        return " ".join(city_tokens[:-1]), "везде", depart_date, return_date, passengers_part

    return None


def _format_datetime(dt_str: str) -> str:
    if not dt_str:
        return "??:??"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        return dt.strftime("%H:%M")
    except Exception:
        return dt_str.split('T')[1][:5] if 'T' in dt_str else "??:??"


def _format_duration(minutes: int) -> str:
    if not minutes:
        return "—"
    hours = minutes // 60
    mins  = minutes % 60
    parts = []
    if hours: parts.append(f"{hours}ч")
    if mins:  parts.append(f"{mins}м")
    return " ".join(parts) if parts else "—"


# ── Основной обработчик ───────────────────────────────────────────────────────

async def handle_flight_request(message: Message) -> None:
    """
    Тихий ручной поиск. Вызывается из handle_any_message (start.py) вне FSM.
    Молча игнорирует нераспознанные сообщения.
    """
    text = (message.text or "").strip()
    logger.info(f"[QuickSearch] Входящий текст: '{text}'")

    parsed = _parse_quick_search(text)
    if not parsed:
        logger.debug(f"[QuickSearch] Не распознан: '{text}'")
        return

    origin_city, dest_city, depart_date, return_date, passengers_part = parsed
    logger.info(
        f"[QuickSearch] origin='{origin_city}' dest='{dest_city}' "
        f"depart='{depart_date}' return='{return_date}' pax='{passengers_part}'"
    )

    is_origin_everywhere = origin_city.strip().lower() == "везде"
    is_dest_everywhere   = dest_city.strip().lower()   == "везде"

    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\nУкажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return

    # ── Тип рейса ─────────────────────────────────────────────────
    flight_type = "all"
    direct_only = transfers_only = False
    if passengers_part:
        pl = passengers_part.lower()
        if "прям" in pl or "direct" in pl:
            flight_type = "direct"
            direct_only = True
        elif "пересад" in pl or "transfer" in pl or "с пересад" in pl:
            flight_type = "transfer"
            transfers_only = True

    # ── Валидация городов ─────────────────────────────────────────
    orig_iata_check, _ = _resolve_city(origin_city) if not is_origin_everywhere else (None, None)
    dest_iata_check, _ = _resolve_city(dest_city)   if not is_dest_everywhere   else (None, None)

    if orig_iata_check and dest_iata_check and orig_iata_check == dest_iata_check:
        await message.answer(
            "❌ Город вылета и прибытия не могут совпадать.\nПожалуйста, выберите разные города.",
            reply_markup=CANCEL_KB
        )
        return

    if return_date:
        nd = normalize_date(depart_date)
        nr = normalize_date(return_date)
        if nr and nd and nr <= nd:
            await message.answer(
                "❌ Дата возврата не может быть раньше или равна дате вылета.\n"
                "Укажите правильную дату возврата.",
                reply_markup=CANCEL_KB
            )
            return

    # ── Везде — делегируем ────────────────────────────────────────
    if is_origin_everywhere or is_dest_everywhere:
        passengers_code = parse_passengers(passengers_part or "")
        await handle_everywhere_search_manual(
            message=message,
            origin_city=origin_city,
            dest_city=dest_city,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code,
            is_origin_everywhere=is_origin_everywhere,
            is_dest_everywhere=is_dest_everywhere,
        )
        return

    # ── Резолв городов ────────────────────────────────────────────
    dest_iata, dest_name = _resolve_city(dest_city)
    if not dest_iata:
        # Тихо игнорируем — не мусорим в чат
        logger.debug(f"[QuickSearch] Не найден город прилёта: '{dest_city}'")
        return

    orig_iata, origin_name = _resolve_city(origin_city)
    if not orig_iata:
        logger.debug(f"[QuickSearch] Не найден город вылета: '{origin_city}'")
        return

    # Если metro (MOW) → ищем по всем аэропортам
    origins = (
        [ap for ap, _ in MULTI_AIRPORT_CITIES[orig_iata]]
        if orig_iata in MULTI_AIRPORT_CITIES
        else [orig_iata]
    )

    passengers_code  = parse_passengers(passengers_part or "")
    passenger_desc_s = build_passenger_desc(passengers_code)
    display_depart   = format_user_date(depart_date)
    display_return   = format_user_date(return_date) if return_date else None
    is_roundtrip     = bool(return_date)

    await message.answer("🔍 Ищу билеты...")

    all_flights: list = []
    for orig in origins:
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(depart_date),
            normalize_date(return_date) if return_date else None,
            direct=direct_only,
        )
        if direct_only:
            flights = [f for f in flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            flights = [f for f in flights if f.get("transfers", 0) > 0]
        for f in flights:
            f["origin"] = orig
        all_flights.extend(flights)
        await asyncio.sleep(0.3)

    # ── Нет прямых → предлагаем с пересадками ────────────────────
    if direct_only and not all_flights:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Показать рейсы с пересадками",
                                  callback_data="show_transfers_fallback")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
        await message.answer(
            "😔 Прямых рейсов на эти даты не найдено.\n"
            "Хотите посмотреть варианты с пересадками? Они часто дешевле!",
            reply_markup=kb,
        )
        return

    # ── Вообще нет рейсов ─────────────────────────────────────────
    if not all_flights:
        origin_iata = origins[0]
        d1 = format_avia_link_date(depart_date)
        d2 = format_avia_link_date(return_date) if return_date else ""
        clean_link    = f"https://www.aviasales.ru/search/{origin_iata}{d1}{dest_iata}{d2}{passengers_code}"
        partner_link  = await convert_to_partner_link(clean_link)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales", url=partner_link)],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
        await message.answer(
            "😔 Билеты не найдены.\nНа Aviasales могут быть рейсы с пересадками — попробуйте:",
            reply_markup=kb,
        )
        return

    # ── Сохраняем в кэш и показываем результат ───────────────────
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights":           all_flights,
        "dest_iata":         dest_iata,
        "origin_iata":       origins[0],
        "origin_name":       origin_name,
        "dest_name":         dest_name,
        "is_roundtrip":      is_roundtrip,
        "display_depart":    display_depart,
        "display_return":    display_return,
        "original_depart":   depart_date,
        "original_return":   return_date,
        "passenger_desc":    passenger_desc_s,
        "passengers_code":   passengers_code,
        "passenger_code":    passengers_code,
        "origin_everywhere": False,
        "dest_everywhere":   False,
        "flight_type":       flight_type,
        "need_return":       is_roundtrip,
        "adults":            int(passengers_code[0]) if passengers_code else 1,
        "children":          int(passengers_code[1]) if len(passengers_code) > 1 else 0,
        "infants":           int(passengers_code[2]) if len(passengers_code) > 2 else 0,
    })

    top_flight = find_cheapest_flight_on_exact_date(all_flights, depart_date, return_date)
    price      = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata = top_flight.get("origin", origins[0])
    origin_name  = get_city_name(origin_iata) or IATA_TO_CITY.get(origin_iata, origin_iata)
    dest_name    = get_city_name(dest_iata)   or IATA_TO_CITY.get(dest_iata, dest_iata)

    duration  = _format_duration(top_flight.get("duration", 0))
    transfers = top_flight.get("transfers", 0)

    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport   = AIRPORT_NAMES.get(dest_iata, dest_iata)

    if transfers == 0:
        transfer_text = "✈️ Прямой рейс"
    elif transfers == 1:
        transfer_text = "✈️ 1 пересадка"
    else:
        transfer_text = f"✈️ {transfers} пересадки"

    price_per_pax = int(float(price)) if price != "?" else 0
    try:
        num_adults = int(passengers_code[0])
    except (IndexError, ValueError):
        num_adults = 1
    estimated_total = price_per_pax * num_adults if price != "?" else "?"

    text = (
        f"✅ <b>Самый дешёвый вариант на {display_depart} ({passenger_desc_s}):</b>\n"
        f"🛫 <b>{origin_name} → {dest_name}</b>\n"
        f"📍 ({origin_iata}) → ({dest_iata})\n"
        f"📅 Туда: {display_depart}\n"
        f"⏱️ Продолжительность: {duration}\n"
        f"{transfer_text}\n"
    )

    airline       = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    if airline or flight_number:
        airline_display = _AIRLINE_NAMES.get(airline, airline)
        text += f"✈️ {airline_display} {flight_number}\n".strip() + "\n"

    if price != "?":
        text += f"\n💰 <b>Цена за 1 пассажира:</b> {price_per_pax} ₽"
        if num_adults > 1:
            text += f"\n🧮 <b>Примерно за {num_adults} взрослых:</b> ~{estimated_total} ₽"
    else:
        text += f"\n💰 <b>Цена:</b> уточните на Aviasales"

    text += f"\n📅 <b>Туда:</b> {display_depart}"
    if is_roundtrip and display_return:
        text += f"\n↩️ <b>Обратно:</b> {display_return}"

    # ── Ссылки ────────────────────────────────────────────────────
    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_date,
            passengers_code=passengers_code,
            return_date=return_date if is_roundtrip else None,
        )
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    fallback_link = generate_booking_link(
        flight=top_flight,
        origin=origin_iata,
        dest=dest_iata,
        depart_date=depart_date,
        passengers_code=passengers_code,
        return_date=return_date if is_roundtrip else None,
    )
    if not fallback_link.startswith(("http://", "https://")):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    booking_link  = await convert_to_partner_link(booking_link)
    fallback_link = await convert_to_partner_link(fallback_link)

    kb_buttons = []
    if booking_link:
        kb_buttons.append([
            InlineKeyboardButton(text=f"✈️ Посмотреть детали за {price} ₽", url=booking_link)
        ])
    kb_buttons.append([InlineKeyboardButton(text="🔍 Все варианты на эти даты", url=fallback_link)])
    kb_buttons.append([InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"watch_all_{cache_id}")])
    kb_buttons.append([InlineKeyboardButton(text="✏️ Изменить данные", callback_data=f"edit_from_results_{cache_id}")])

    import os
    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-1, [
            InlineKeyboardButton(text=f"🚖 Трансфер в {dest_name}", url=transfer_link)
        ])

    kb_buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await message.answer(text, parse_mode="HTML", reply_markup=kb)
    logger.info(f"[QuickSearch] Результат: {origin_name}→{dest_name} {price}₽")