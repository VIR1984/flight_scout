"""
services/chat_handler.py

Переносит FSM-логику Telegram-бота (handlers/start.py) в веб-контекст.
Состояние хранится в dict (session state), а не в aiogram FSM.

Шаги диалога (step):
  None         → приветствие / главное меню
  'route'      → ввод маршрута
  'depart'     → дата вылета
  'return_ask' → нужен ли обратный?
  'return_date'→ дата возврата
  'flight_type'→ тип рейса (прямой / с пересадкой / любой)
  'adults'     → число взрослых
  'children'   → дети?
  'infants'    → младенцы?
  'searching'  → поиск запущен
"""

import re
import asyncio
from typing import Optional
from datetime import date, datetime

from services.flight_search import (
    search_flights_realtime,
    generate_booking_link,
    normalize_date,
    format_passenger_desc,
    find_cheapest_flight_on_exact_date,
    format_duration,
)
from utils.cities_loader import get_iata, get_city_name, fuzzy_get_iata
from utils.logger import logger


# ── Вспомогательные ────────────────────────────────────────────────

def _validate_date(s: str) -> bool:
    try:
        day, month = map(int, s.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except Exception:
        return False


def _parse_route(text: str):
    """Возвращает (origin_raw, dest_raw) или (None, None)."""
    text = text.strip().lower()
    for sep in [r'\s+[-→—>]+\s+', r'[→—>]+', r'(?<=[а-яёa-z])-(?=[а-яёa-z])']:
        if re.search(sep, text):
            parts = re.split(sep, text, maxsplit=1)
            if len(parts) == 2:
                return parts[0].strip(), parts[1].strip()
    parts = text.split(maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, None


def _fmt_flight(f: dict, origin_name: str, dest_name: str, pax_code: str,
                depart_date: str, return_date: Optional[str] = None) -> dict:
    """Форматирует рейс для фронтенда."""
    dep_at  = f.get("departure_at", "")
    arr_at  = f.get("arrival_at", "")
    dep_time = dep_at[11:16] if len(dep_at) > 15 else "—"
    arr_time = arr_at[11:16] if len(arr_at) > 15 else "—"

    transfers = f.get("transfers", 0)
    stops_label = "Прямой" if transfers == 0 else f"{transfers} пересадка"

    link = f.get("link") or generate_booking_link(
        flight=f,
        origin=f.get("origin", ""),
        dest=f.get("destination", ""),
        depart_date=depart_date,
        passengers_code=pax_code,
        return_date=return_date,
    )

    return {
        "airline":      f.get("airline", ""),
        "flight_number": f.get("flight_number", ""),
        "dep_time":     dep_time,
        "arr_time":     arr_time,
        "origin":       f.get("origin", ""),
        "dest":         f.get("destination", ""),
        "origin_name":  origin_name,
        "dest_name":    dest_name,
        "duration":     format_duration(f.get("duration", 0)),
        "price":        f.get("value") or f.get("price", 0),
        "stops":        transfers,
        "stops_label":  stops_label,
        "link":         link,
    }


# ── Главный обработчик ──────────────────────────────────────────────

async def handle_user_message(text: str, state: dict, session_id: str) -> dict:
    """
    Принимает текст пользователя и текущее состояние FSM,
    возвращает ответ в формате:
    {
      "type": "message",
      "text": "...",
      "buttons": [...],       # inline-кнопки (список строк)
      "flights": [...],       # карточки рейсов (если есть)
      "step": "...",          # текущий шаг для отладки
    }
    """
    step = state.get("step")

    # ── Быстрые команды (всегда работают) ──────────────────────────
    if text in ["🔥 Горящие", "🔥 Горящие предложения", "/hot"]:
        state.clear()
        return {
            "type": "message",
            "text": "🔥 *Горящие предложения* доступны в разделе «Deals» справа. Там всегда актуальные билеты ниже рынка!\n\nИли начнём поиск конкретного маршрута?",
            "buttons": ["✏️ Ввести маршрут", "🌍 Куда угодно"]
        }

    if text in ["🌍 Куда угодно", "/everywhere"]:
        state.clear()
        state["step"] = "everywhere_origin"
        return {
            "type": "message",
            "text": "🌍 Поиск *«Куда угодно»*! Введите город вылета:",
            "buttons": ["Москва", "Санкт-Петербург", "Екатеринбург"]
        }

    if text in ["↩️ Назад", "Отмена", "/cancel", "Главное меню"]:
        state.clear()
        return _welcome()

    # ── Приветствие ─────────────────────────────────────────────────
    if step is None or text in ["/start", "✈️ Новый поиск"]:
        state.clear()
        if text in ["/start", "✈️ Новый поиск"]:
            state["step"] = "route"
            return {
                "type": "message",
                "text": "Отлично! Введите маршрут в формате:\n*Москва — Сочи* или *MOW AER*\n\nМожно также: *Питер → Дубай*, *Казань Анталья*",
                "step": "route"
            }
        return _welcome()

    # ── ШАГ: маршрут ────────────────────────────────────────────────
    if step == "route" or step is None:
        if step is None:
            state["step"] = "route"

        origin_raw, dest_raw = _parse_route(text)

        if not origin_raw or not dest_raw:
            # Попробовать как один город → предложить выбор
            return {
                "type": "message",
                "text": "Укажите маршрут в формате *Откуда — Куда*.\nНапример: *Москва — Сочи* или *LED DXB*",
                "step": "route"
            }

        origin_iata = get_iata(origin_raw) or fuzzy_get_iata(origin_raw)
        dest_iata   = get_iata(dest_raw)   or fuzzy_get_iata(dest_raw)

        if not origin_iata:
            return {"type": "message", "text": f"❓ Не нашёл аэропорт «{origin_raw}». Попробуйте ввести IATA-код (MOW, LED).", "step": "route"}
        if not dest_iata:
            return {"type": "message", "text": f"❓ Не нашёл аэропорт «{dest_raw}». Попробуйте ввести IATA-код (AER, DXB).", "step": "route"}

        state["origin"]      = origin_iata
        state["dest"]        = dest_iata
        state["origin_name"] = get_city_name(origin_iata) or origin_raw.title()
        state["dest_name"]   = get_city_name(dest_iata)   or dest_raw.title()
        state["step"]        = "depart"

        return {
            "type": "message",
            "text": f"✅ Маршрут: *{state['origin_name']} ({origin_iata}) → {state['dest_name']} ({dest_iata})*\n\nВведите дату вылета в формате *ДД.ММ* (например, *15.04*):",
            "step": "depart"
        }

    # ── ШАГ: дата вылета ────────────────────────────────────────────
    if step == "depart":
        if not _validate_date(text):
            return {"type": "message", "text": "📅 Введите дату в формате *ДД.ММ*, например: *25.04*", "step": "depart"}

        state["depart_date"] = normalize_date(text)
        state["step"]        = "return_ask"

        return {
            "type": "message",
            "text": f"📅 Дата вылета: *{text}*\n\nНужен обратный билет?",
            "buttons": ["Только туда", "Туда-обратно"],
            "step": "return_ask"
        }

    # ── ШАГ: нужен ли возврат ───────────────────────────────────────
    if step == "return_ask":
        if "обратно" in text.lower() or text == "Туда-обратно":
            state["step"] = "return_date"
            return {"type": "message", "text": "📅 Введите дату обратного рейса (*ДД.ММ*):", "step": "return_date"}
        else:
            state["return_date"] = None
            state["step"]        = "flight_type"
            return {
                "type": "message",
                "text": "Тип рейса:",
                "buttons": ["✈️ Прямые рейсы", "🔄 С пересадкой", "🌐 Все варианты"],
                "step": "flight_type"
            }

    # ── ШАГ: дата возврата ──────────────────────────────────────────
    if step == "return_date":
        if not _validate_date(text):
            return {"type": "message", "text": "📅 Введите дату обратного рейса в формате *ДД.ММ*:", "step": "return_date"}
        state["return_date"] = normalize_date(text)
        state["step"]        = "flight_type"
        return {
            "type": "message",
            "text": f"📅 Обратный рейс: *{text}*\n\nТип рейса:",
            "buttons": ["✈️ Прямые рейсы", "🔄 С пересадкой", "🌐 Все варианты"],
            "step": "flight_type"
        }

    # ── ШАГ: тип рейса ──────────────────────────────────────────────
    if step == "flight_type":
        if "прям" in text.lower():
            state["flight_type"] = "direct"
        elif "пересадк" in text.lower():
            state["flight_type"] = "transfer"
        else:
            state["flight_type"] = "all"
        state["step"] = "adults"
        return {
            "type": "message",
            "text": "👤 Сколько взрослых пассажиров?",
            "buttons": ["1", "2", "3", "4"],
            "step": "adults"
        }

    # ── ШАГ: взрослые ───────────────────────────────────────────────
    if step == "adults":
        try:
            n = int(re.search(r'\d+', text).group())
            state["adults"] = max(1, min(n, 9))
        except Exception:
            return {"type": "message", "text": "Введите число взрослых (1–9):", "step": "adults"}
        state["step"] = "children"
        return {
            "type": "message",
            "text": "👶 Есть дети (2–12 лет)?",
            "buttons": ["Нет", "1 ребёнок", "2 ребёнка"],
            "step": "children"
        }

    # ── ШАГ: дети ───────────────────────────────────────────────────
    if step == "children":
        if "нет" in text.lower() or text == "0":
            state["children"] = 0
        else:
            try:
                state["children"] = int(re.search(r'\d+', text).group())
            except Exception:
                state["children"] = 0
        state["step"] = "infants"
        return {
            "type": "message",
            "text": "👶 Есть младенцы (до 2 лет)?",
            "buttons": ["Нет", "1 младенец"],
            "step": "infants"
        }

    # ── ШАГ: младенцы → запуск поиска ───────────────────────────────
    if step == "infants":
        if "нет" in text.lower() or text == "0":
            state["infants"] = 0
        else:
            try:
                state["infants"] = int(re.search(r'\d+', text).group())
            except Exception:
                state["infants"] = 0
        state["step"] = "searching"
        return await _do_search(state)

    # ── После поиска: дополнительные команды ────────────────────────
    if step == "searching":
        if "отследить" in text.lower() or "📊" in text:
            return {
                "type": "message",
                "text": "🔔 Маршрут добавлен в отслеживание! Уведомлю при изменении цены более чем на 10%.",
                "buttons": ["✈️ Новый поиск", "🔥 Горящие предложения"]
            }
        if "все" in text.lower() or "больше" in text.lower():
            return {"type": "message", "text": "Показываю все результаты поиска — используйте фильтры справа."}
        state.clear()
        state["step"] = "route"
        return {
            "type": "message",
            "text": "Введите новый маршрут (например, *Москва — Дубай*):",
            "step": "route"
        }

    # ── Everywhere: город вылета ─────────────────────────────────────
    if step == "everywhere_origin":
        iata = get_iata(text) or fuzzy_get_iata(text)
        if not iata:
            return {"type": "message", "text": f"❓ Не нашёл «{text}». Введите название города:", "step": "everywhere_origin"}
        state["origin"]      = iata
        state["origin_name"] = get_city_name(iata) or text.title()
        state["step"]        = "everywhere_date"
        return {
            "type": "message",
            "text": f"✅ Вылет из *{state['origin_name']}*\n\nВведите месяц вылета (например, *апрель* или *04*):",
            "step": "everywhere_date"
        }

    # Fallback
    state["step"] = "route"
    return {
        "type": "message",
        "text": "🤔 Не совсем понял. Введите маршрут: *Откуда — Куда* (например, *Москва — Сочи*)",
        "step": "route"
    }


# ── Поиск ──────────────────────────────────────────────────────────

async def _do_search(state: dict) -> dict:
    origin      = state.get("origin", "")
    dest        = state.get("dest", "")
    depart_date = state.get("depart_date", "")
    return_date = state.get("return_date")
    adults      = state.get("adults", 1)
    children    = state.get("children", 0)
    infants     = state.get("infants", 0)
    origin_name = state.get("origin_name", origin)
    dest_name   = state.get("dest_name", dest)
    flight_type = state.get("flight_type", "all")

    pax_code = str(adults)
    if children: pax_code += str(children)
    if infants:  pax_code += str(infants)
    pax_desc = format_passenger_desc(pax_code)

    try:
        flights = await search_flights_realtime(
            origin=origin, destination=dest,
            depart_date=depart_date, return_date=return_date,
            adults=adults, children=children, infants=infants,
        )
    except Exception as e:
        logger.error(f"Ошибка поиска: {e}")
        flights = []

    # Фильтр по типу
    if flight_type == "direct":
        flights = [f for f in flights if f.get("transfers", 0) == 0]
    elif flight_type == "transfer":
        flights = [f for f in flights if f.get("transfers", 0) > 0]

    if not flights:
        return {
            "type": "message",
            "text": f"😔 По маршруту *{origin_name} → {dest_name}* на {depart_date[:10]} рейсов не нашлось.\n\nПопробуйте другие даты или направление.",
            "buttons": ["✈️ Новый поиск", "📅 Другие даты"]
        }

    # Топ-5
    top = flights[:5]
    formatted = [_fmt_flight(f, origin_name, dest_name, pax_code, depart_date, return_date) for f in top]

    cheapest_price = formatted[0]["price"] if formatted else 0

    text = (
        f"✅ Найдено *{len(flights)} рейсов* "
        f"*{origin_name} → {dest_name}*, {depart_date[8:10]}.{depart_date[5:7]}\n"
        f"👤 {pax_desc}\n"
        f"💰 Лучшая цена: *{cheapest_price:,} ₽*"
    )

    return {
        "type": "message",
        "text": text,
        "flights": formatted,
        "total": len(flights),
        "buttons": [f"📋 Все {len(flights)} рейсов", "📊 Отследить цену", "📅 Другие даты"],
        "step": "searching"
    }


def _welcome() -> dict:
    return {
        "type": "message",
        "text": "Привет! Я *Scout* — ваш ИИ-помощник по поиску авиабилетов ✈️\n\nОткуда летим?",
        "buttons": ["✏️ Ввести маршрут", "🌍 Куда угодно", "🔥 Горящие предложения"],
        "step": None
    }
