import os
import asyncio
import aiohttp
import hashlib
import re
from typing import List, Dict, Optional
from urllib.parse import urlparse, urlunparse
from datetime import datetime, date
from utils.logger import logger
from utils.flight_utils import parse_passengers


# ══════════════════════════════════════════════════════════════════
# Конфигурация
# ══════════════════════════════════════════════════════════════════

# Data API (grouped_prices, кеш ~48ч) — фоновые задачи, горячие предложения
AVIASALES_GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"

# Real-time Search API — поиск по запросу пользователя
AVIASALES_SEARCH_URL         = "https://api.travelpayouts.com/v1/flight_search"
AVIASALES_SEARCH_RESULTS_URL = "https://api.travelpayouts.com/v1/flight_search_results"

AVIASALES_TOKEN  = os.getenv("AVIASALES_TOKEN", "").strip()
AVIASALES_MARKER = os.getenv("AVIASALES_MARKER", "").strip()
AVIASALES_HOST   = os.getenv("AVIASALES_HOST", "beta.aviasales.ru").strip()

# ── Глобальный HTTP-коннектор ─────────────────────────────────────────────────
# Вместо нового ClientSession() на каждый запрос переиспользуем TCP-соединения.
# Экономит ~150-300ms (TCP handshake + TLS) на каждом вызове search_flights*.
_http_connector: Optional[aiohttp.TCPConnector] = None

def _get_http_connector() -> aiohttp.TCPConnector:
    """Возвращает (или создаёт) глобальный TCPConnector."""
    global _http_connector
    if _http_connector is None or _http_connector.closed:
        _http_connector = aiohttp.TCPConnector(
            limit=30,              # макс. параллельных соединений
            limit_per_host=10,     # до 10 на один хост
            ttl_dns_cache=300,     # DNS кеш 5 минут
            enable_cleanup_closed=True,
        )
    return _http_connector

def _http_session() -> aiohttp.ClientSession:
    """Создаёт сессию с общим коннектором (не закрывает его при выходе)."""
    return aiohttp.ClientSession(
        connector=_get_http_connector(),
        connector_owner=False,
    )


# ══════════════════════════════════════════════════════════════════
# Утилиты: даты
# ══════════════════════════════════════════════════════════════════

def normalize_date(date_str: str) -> str:
    """
    Преобразует дату любого формата в ГГГГ-ММ-ДД.
    Поддерживает: 'ДД.ММ', 'ГГГГ-ММ-ДД' (возвращает как есть).
    Год определяется динамически — ближайший будущий.
    """
    if not date_str:
        return date_str
    if len(date_str) == 10 and date_str[4] == '-':
        return date_str
    try:
        day, month = map(int, date_str.split('.'))
        today = date.today()
        year  = today.year
        try:
            target = date(year, month, day)
        except ValueError:
            return date_str
        if target < today:
            year += 1
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return date_str


def format_avia_link_date(date_str: str) -> str:
    """
    Форматирует дату → ДДММ для ссылки Aviasales.
    Принимает 'ДД.ММ' или 'ГГГГ-ММ-ДД'.
    """
    try:
        if len(date_str) == 10 and date_str[4] == '-':
            _, month, day = date_str.split('-')
            return f"{day}{month}"
        day, month = date_str.split('.')
        return f"{day}{month}"
    except Exception:
        return date_str.replace('.', '').replace('-', '')


# ══════════════════════════════════════════════════════════════════
# Утилиты: ссылки
# ══════════════════════════════════════════════════════════════════

def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None,
) -> str:
    """
    Генерирует ссылку на Aviasales.
    В одну сторону: ORIGДДММDESTПАСС     → MOW1003AER1
    Туда-обратно:   ORIGДДММDESTДДММПАСС → MOW1003AER1503211
    """
    if not passengers_code or not isinstance(passengers_code, str):
        passengers_code = "1"
    passengers_code = re.sub(r'\D', '', passengers_code)[:3]
    if not passengers_code or passengers_code[0] == '0':
        passengers_code = "1"

    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    return f"https://www.aviasales.ru/search/{route}"


def update_passengers_in_link(link: str, passengers_code: str) -> str:
    """Заменяет код пассажиров в существующей ссылке Aviasales."""
    if not link or not passengers_code or not passengers_code.isdigit():
        return link
    if not re.match(r'^[1-9]\d{0,2}$', passengers_code):
        return link

    is_relative = link.startswith('/')
    parsed = None if is_relative else urlparse(link)
    path   = link if is_relative else parsed.path

    if '/search/' not in path:
        return link

    _, search_part = path.split('/search/', 1)

    if '?' in search_part:
        route, query = search_part.split('?', 1)
        has_query = True
    else:
        route, query = search_part, ""
        has_query = False

    new_route = (route[:-1] + passengers_code) if (route and route[-1].isdigit()) else (route + passengers_code)
    new_path  = f"/search/{new_route}" + (f"?{query}" if has_query else "")
    return new_path if is_relative else urlunparse(parsed._replace(path=new_path))


# ══════════════════════════════════════════════════════════════════
# Утилиты: пассажиры и форматирование
# ══════════════════════════════════════════════════════════════════

def format_passenger_desc(code: str) -> str:
    """'211' → '2 взр., 1 реб., 1 мл.'"""
    try:
        adults   = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants  = int(code[2]) if len(code) > 2 else 0
        parts = []
        if adults:   parts.append(f"{adults} взр.")
        if children: parts.append(f"{children} реб.")
        if infants:  parts.append(f"{infants} мл.")
        return ", ".join(parts) if parts else "1 взр."
    except Exception:
        return "1 взр."


def format_duration(minutes: int) -> str:
    """125 → '2ч 5м'"""
    if not minutes:
        return "—"
    parts = []
    if minutes // 60: parts.append(f"{minutes // 60}ч")
    if minutes % 60:  parts.append(f"{minutes % 60}м")
    return " ".join(parts) if parts else "—"


def find_cheapest_flight_on_exact_date(
    flights: List[Dict],
    requested_depart_date: str,
    requested_return_date: Optional[str] = None,
) -> Optional[Dict]:
    """Самый дешёвый рейс на точную дату; если не найден — глобальный минимум."""
    if not flights:
        return None
    req_dep = normalize_date(requested_depart_date)
    req_ret = normalize_date(requested_return_date) if requested_return_date else None

    exact = [
        f for f in flights
        if f.get("departure_at", "")[:10] == req_dep
        and (not req_ret or f.get("return_at", "")[:10] == req_ret)
    ]
    pool = exact if exact else flights
    return min(pool, key=lambda f: f.get("value") or f.get("price") or 999_999_999)


# ══════════════════════════════════════════════════════════════════
# Real-time Search API
# Только по запросу пользователя (кнопка «Найти билеты»)
# ══════════════════════════════════════════════════════════════════

def _build_rt_signature(
    token: str, marker: str, host: str, locale: str,
    passengers: Dict, segments: List[Dict], trip_class: str = "Y",
) -> str:
    """
    MD5-подпись по правилам Travelpayouts.
    token:host:locale:marker:adults:children:infants:
    date1:origin1:dest1[:...]:trip_class:user_ip
    """
    parts = [
        token, host, locale, marker,
        str(passengers.get("adults",   1)),
        str(passengers.get("children", 0)),
        str(passengers.get("infants",  0)),
    ]
    for seg in segments:
        parts += [seg["date"], seg["origin"], seg["destination"]]
    parts += [trip_class, "127.0.0.1"]
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _normalize_rt_proposals(
    proposals: List[Dict],
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    rub_rate: float,
) -> List[Dict]:
    """
    Конвертирует proposals real-time API → стандартный формат бота.
    Поля: value, price, departure_at, return_at, arrival_at,
          origin, destination, transfers, duration,
          airline, flight_number, link, deep_link, _source
    """
    result: List[Dict] = []
    seen_prices: set   = set()

    for p in proposals:
        try:
            # ── Цена в рублях ─────────────────────────────────────
            raw = p.get("min_price") or p.get("price", 0)
            price_rub = int(
                (list(raw.values())[0] * rub_rate) if isinstance(raw, dict)
                else (float(raw) * rub_rate)
            )
            if price_rub <= 0 or price_rub in seen_prices:
                continue
            seen_prices.add(price_rub)

            # ── Сегменты ──────────────────────────────────────────
            segments = p.get("segment", [])
            if not segments:
                continue

            flights_out = segments[0].get("flight", [])
            if not flights_out:
                continue

            departure_at  = flights_out[0].get("departure", f"{depart_date}T00:00:00")
            arrival_at    = flights_out[-1].get("arrival", "")
            transfers_out = len(flights_out) - 1

            return_at      = ""
            transfers_back = 0
            if len(segments) > 1:
                flights_back   = segments[1].get("flight", [])
                transfers_back = len(flights_back) - 1
                if flights_back:
                    return_at = flights_back[0].get("departure", "")

            # ── Длительность, авиакомпания ─────────────────────────
            duration_min  = (segments[0].get("duration", 0) // 60) or 0
            airline       = flights_out[0].get("marketing_carrier") or flights_out[0].get("operating_carrier", "")
            flight_number = flights_out[0].get("number", "")

            # ── Ссылка ─────────────────────────────────────────────
            booking_link = p.get("url") or p.get("link") or generate_booking_link(
                flight={},
                origin=origin,
                dest=destination,
                depart_date=depart_date,
                passengers_code=passengers_code,
                return_date=return_date,
            )

            result.append({
                "value":         price_rub,
                "price":         price_rub,
                "departure_at":  departure_at,
                "return_at":     return_at,
                "arrival_at":    arrival_at,
                "origin":        origin,
                "destination":   destination,
                "transfers":     max(transfers_out, transfers_back),
                "duration":      duration_min,
                "airline":       airline,
                "flight_number": flight_number,
                "link":          booking_link,
                "deep_link":     booking_link,
                "_source":       "realtime",
            })

        except Exception as e:
            logger.debug(f"[RT] Ошибка нормализации: {e}")
            continue

    result.sort(key=lambda f: f["value"])
    return result


async def search_flights_realtime(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    adults: int = 1,
    children: int = 0,
    infants: int = 0,
    trip_class: str = "Y",
    locale: str = "ru",
    poll_timeout: int = 45,
    poll_interval: float = 2.0,
) -> List[Dict]:
    """
    Real-time поиск через Travelpayouts v1/flight_search.

    Шаги:
      1. POST /v1/flight_search          → search_id
      2. Поллинг GET /v1/flight_search_results?uuid=...
         до финального {search_id: ...} или таймаута poll_timeout сек
      3. Нормализация → стандартный формат бота

    При любой ошибке — автофолбэк на cached API (search_flights).
    """
    if not AVIASALES_TOKEN or not AVIASALES_MARKER:
        logger.warning("⚠️ [RT] Не заданы TOKEN или MARKER — фолбэк на cached")
        return await search_flights(origin, destination, depart_date, return_date)

    pax_code   = str(adults) + (str(children) if children else "") + (str(infants) if infants else "")
    passengers = {"adults": adults, "children": children, "infants": infants}
    segments   = [{"origin": origin, "destination": destination, "date": depart_date}]
    if return_date:
        segments.append({"origin": destination, "destination": origin, "date": return_date})

    signature = _build_rt_signature(
        token=AVIASALES_TOKEN, marker=AVIASALES_MARKER,
        host=AVIASALES_HOST,   locale=locale,
        passengers=passengers, segments=segments,
    )
    payload = {
        "signature":  signature,
        "marker":     AVIASALES_MARKER,
        "host":       AVIASALES_HOST,
        "user_ip":    "127.0.0.1",
        "locale":     locale,
        "trip_class": trip_class,
        "passengers": passengers,
        "segments":   segments,
    }

    async with _http_session() as session:

        # ── 1. Запуск поиска ────────────────────────────────────
        try:
            async with session.post(
                AVIASALES_SEARCH_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    logger.error(f"❌ [RT] POST {resp.status}: {(await resp.text())[:300]}")
                    return await search_flights(origin, destination, depart_date, return_date)

                init = await resp.json()
                search_id = init.get("search_id") or init.get("meta", {}).get("uuid")
                if not search_id:
                    logger.error(f"❌ [RT] Нет search_id: {str(init)[:200]}")
                    return await search_flights(origin, destination, depart_date, return_date)

                logger.info(f"✅ [RT] search_id={search_id}")

        except asyncio.TimeoutError:
            logger.error("❌ [RT] Таймаут при запуске")
            return await search_flights(origin, destination, depart_date, return_date)
        except Exception as e:
            logger.error(f"❌ [RT] Ошибка запуска: {e}")
            return await search_flights(origin, destination, depart_date, return_date)

        # ── 2. Поллинг результатов ──────────────────────────────
        all_proposals: List[Dict] = []
        currency_rates: Dict      = {}
        deadline = asyncio.get_event_loop().time() + poll_timeout

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                async with session.get(
                    AVIASALES_SEARCH_RESULTS_URL,
                    params={"uuid": search_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning(f"⚠️ [RT] GET {r.status}")
                        continue

                    chunk = await r.json(content_type=None)

                    if isinstance(chunk, dict):
                        if "currency_rates" in chunk:
                            currency_rates = chunk["currency_rates"]
                        # Финальный ответ содержит только ключ search_id
                        if list(chunk.keys()) == ["search_id"]:
                            logger.info(f"✅ [RT] Завершён, proposals={len(all_proposals)}")
                            break
                        proposals = chunk.get("proposals", [])
                    elif isinstance(chunk, list):
                        proposals = chunk
                    else:
                        proposals = []

                    if proposals:
                        all_proposals.extend(proposals)
                        logger.debug(f"[RT] +{len(proposals)} (итого {len(all_proposals)})")

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"❌ [RT] Поллинг: {e}")
                continue

    if not all_proposals:
        logger.warning("⚠️ [RT] Нет предложений — фолбэк на cached")
        return await search_flights(origin, destination, depart_date, return_date)

    # ── 3. Нормализация ─────────────────────────────────────────
    rub_rate = float(currency_rates.get("rub", 1.0) or 1.0)
    flights  = _normalize_rt_proposals(
        proposals=all_proposals,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date,
        passengers_code=pax_code,
        rub_rate=rub_rate,
    )
    logger.info(f"✅ [RT] Нормализовано {len(flights)} рейсов")
    return flights


# ══════════════════════════════════════════════════════════════════
# Cached / Data API
# Для: PriceWatcher, HotDealsSender, поиска «Везде»
# ══════════════════════════════════════════════════════════════════

async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    direct: bool = False,
) -> List[Dict]:
    """
    Поиск через Data API (grouped_prices, кеш ~48ч).
    Не требует MARKER — только TOKEN.
    Используется для фоновых задач: мониторинг цен, горячие предложения.
    """
    if not AVIASALES_TOKEN:
        logger.warning("⚠️ [Cache] AVIASALES_TOKEN не задан")
        return []

    params: Dict = {
        "origin":       origin,
        "destination":  destination,
        "departure_at": depart_date,
        "currency":     currency,
        "token":        AVIASALES_TOKEN,
        "group_by":     "departure_at",
        "direct":       "true" if direct else "false",
    }
    if return_date:
        params["return_at"] = return_date
        try:
            d1   = datetime.fromisoformat(depart_date)
            d2   = datetime.fromisoformat(return_date)
            days = (d2 - d1).days
            if days > 0:
                params["min_trip_duration"] = days
                params["max_trip_duration"] = days
        except Exception:
            pass

    try:
        async with _http_session() as session:
            async with session.get(
                AVIASALES_GROUPED_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status == 429:
                    logger.warning("⚠️ [Cache] Rate limit 429, ждём 60с...")
                    await asyncio.sleep(60)
                    return []
                if response.status != 200:
                    logger.error(f"❌ [Cache] {response.status}: {(await response.text())[:200]}")
                    return []

                data = await response.json()
                if not data.get("success"):
                    logger.error(f"❌ [Cache] error: {data.get('error')}")
                    return []

                flights = []
                for date_key, flight in data.get("data", {}).items():
                    flight["value"]        = flight.get("price")
                    flight["departure_at"] = flight.get("departure_at", f"{date_key}T00:00:00+03:00")
                    flight["return_at"]    = flight.get("return_at", "")
                    flight["origin"]       = flight.get("origin", origin)
                    flight["destination"]  = flight.get("destination", destination)
                    flight["_source"]      = "cached"
                    flights.append(flight)
                return flights

    except asyncio.TimeoutError:
        logger.error("❌ [Cache] Таймаут")
        return []
    except Exception as e:
        logger.error(f"❌ [Cache] Ошибка: {e}")
        return []

# ══════════════════════════════════════════════════════════════════
# Multi-segment (составной маршрут) — real-time API
# ══════════════════════════════════════════════════════════════════