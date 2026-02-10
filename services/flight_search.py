import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from urllib.parse import urlparse, urlunparse
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# --- Константы ---
API_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
MARKUP = 0.05  # 5% наценка
MAX_RETRIES = 3
RETRY_DELAY = 1

# --- Функции для работы с API ---
async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,  # YYYY-MM-DD
    return_date: Optional[str],  # YYYY-MM-DD
    passengers: str = "1",  # e.g., "1", "2", "21", "211"
    currency: str = "RUB"
) -> List[Dict]:
    """
    Поиск билетов через Travelpayouts API v3.
    """
    params = {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "depart_date": depart_date,  # Format: YYYY-MM-DD
        "currency": currency.lower(),
        "passengers": passengers,  # Code: 1 adult, 21 adult+child, 211 adult+child+infant
    }
    if return_date:
        params["return_date"] = return_date

    # This is a mock implementation as the real API requires token and partner_marker
    # In a real scenario, you would make an HTTP request here.
    # Example response structure expected from API:
    # [
    #     {
    #         "price": 10000,
    #         "transfers": 0,
    #         "airline": "SU",
    #         "flight_number": "1234",
    #         "departure_at": "2023-10-26T10:00:00+03:00",
    #         "arrival_at": "2023-10-26T15:00:00+03:00",
    #         "link": "/search/MOW1003IST21"  # <-- This is the booking link part
    #     },
    #     ...
    # ]
    # For demonstration, returning a mock result with a typical link structure
    mock_result = [
        {
            "price": 10800,
            "transfers": 0,
            "airline": "SU",
            "flight_number": "1777",
            "departure_at": f"{depart_date}T10:00:00+03:00",
            "arrival_at": f"{depart_date}T15:00:00+03:00",
            # The link from the API will contain the original passenger code
            "link": f"/search/{origin.upper()}{depart_date.replace('-', '')[:4]}{destination.upper()}{passengers}",
            # Some APIs might return deep_link
            # "deep_link": "https://www.aviasales.ru/search/MOW1003IST21?t=..."
        }
    ]
    # Simulate API delay
    await asyncio.sleep(0.2)
    return mock_result


def build_passenger_desc(code: str) -> str:
    """
    Преобразует числовое обозначение пассажиров в текстовое описание.
    1 -> "1 взрослый", 21 -> "2 взрослых, 1 ребёнок", 211 -> "2 взрослых, 1 ребёнок, 1 младенец"
    """
    code_str = str(code)
    adults = int(code_str[0]) if code_str else 1
    children = int(code_str[1]) if len(code_str) > 1 and code_str[1].isdigit() else 0
    infants = int(code_str[2]) if len(code_str) > 2 and code_str[2].isdigit() else 0

    desc_parts = []
    if adults:
        if adults == 1:
            desc_parts.append("1 взрослый")
        elif 2 <= adults <= 4:
            desc_parts.append(f"{adults} взрослых")
        else:
            desc_parts.append(f"{adults} взрослых")
    if children:
        if children == 1:
            desc_parts.append("1 ребёнок")
        else:
            desc_parts.append(f"{children} детей")
    if infants:
        if infants == 1:
            desc_parts.append("1 младенец")
        else:
            desc_parts.append(f"{infants} младенцев")

    return ", ".join(desc_parts) if desc_parts else "1 взрослый"


def format_avia_link_date(date_str: str) -> str:  # YYYY-MM-DD -> DDMM
    """Форматирует дату для ссылки Aviasales."""
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return date_obj.strftime("%d%m")
    except ValueError:
        logger.error(f"Invalid date format for Aviasales link: {date_str}")
        return "0101"  # Fallback


def add_marker_to_url(url: str, marker: str, sub_id: str = "telegram") -> str:
    """Добавляет маркер партнера к URL."""
    parsed = urlparse(url)
    query_parts = [f"marker={marker}", f"sub_id={sub_id}"]
    if parsed.query:
        query_parts.insert(0, parsed.query)
    new_query = "&".join(query_parts)
    return urlunparse(parsed._replace(query=new_query))


def _update_passengers_in_link(link: str, passengers_code: str) -> str:
    """
    Полностью заменяет код пассажиров в маршруте Aviasales.
    Поддерживает коды: 1, 2, 21, 211 и т.д.
    """
    if not link or not passengers_code:
        return link

    # Извлекаем маршрут из URL
    if link.startswith('/'):
        path = link
        is_absolute = False
    else:
        parsed = urlparse(link)
        path = parsed.path
        is_absolute = True

    # Ищем маршрут вида /search/...
    if '/search/' not in path:
        return link

    search_part = path.split('/search/', 1)[1]

    # Разделяем маршрут и параметры
    if '?' in search_part:
        route, query = search_part.split('?', 1)
    else:
        route, query = search_part, ""

    # Удаляем старый код пассажиров (все цифры в конце маршрута)
    i = len(route) - 1
    while i >= 0 and route[i].isdigit():
        i -= 1

    # Формируем новый маршрут
    new_route = route[:i + 1] + passengers_code

    # Собираем обратно
    if query:
        final_path = f"/search/{new_route}?{query}"
    else:
        final_path = f"/search/{new_route}"

    if is_absolute:
        return urlunparse(parsed._replace(path=final_path))
    else:
        return final_path


def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    """
    Генерирует ссылку для бронирования на Aviasales.
    Использует существующую ссылку из API (если есть) и обновляет код пассажиров.
    Или генерирует новую на основе данных.
    """
    # Если в рейсе уже есть ссылка от API - модифицируем её
    existing_link = flight.get("link") or flight.get("deep_link")
    if existing_link and existing_link.startswith('/search/'):
        # Используем функцию для обновления кода пассажиров
        updated_link = _update_passengers_in_link(existing_link, passengers_code)
        # Если ссылка была относительной (/search/...), делаем её абсолютной
        if updated_link.startswith('/'):
             marker = os.getenv("TRAFFIC_SOURCE", "").strip()
             base_url = f"https://www.aviasales.ru{updated_link}"
             if marker:
                 sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
                 return add_marker_to_url(base_url, marker, sub_id)
             return base_url
        return updated_link # Ссылка уже абсолютная

    # Иначе генерируем новую ссылку
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""

    if return_date:
        route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    else:
        route = f"{origin}{d1}{dest}{passengers_code}"

    base_url = f"https://www.aviasales.ru/search/{route}"
    marker = os.getenv("TRAFFIC_SOURCE", "").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()

    if marker:
        return add_marker_to_url(base_url, marker, sub_id)
    return base_url


async def get_cheapest_flight(flights: List[Dict]) -> Optional[Dict]:
    """Возвращает самый дешёвый рейс из списка."""
    if not flights:
        return None
    # Сортируем по цене, используя 'price' или 'value'
    sorted_flights = sorted(flights, key=lambda f: f.get("price", f.get("value", float('inf'))))
    return sorted_flights[0]


async def get_direct_flight(flights: List[Dict]) -> Optional[Dict]:
    """Возвращает прямой рейс из списка (с 0 пересадок), если есть."""
    for flight in flights:
        if flight.get("transfers", 0) == 0:
            return flight
    return None


async def search_with_retries(*args, **kwargs):
    """Обертка для поиска с повторными попытками."""
    for attempt in range(MAX_RETRIES):
        try:
            results = await search_flights(*args, **kwargs)
            return results
        except Exception as e:
            logger.warning(f"Search attempt {attempt + 1} failed: {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error("All search attempts failed.")
                raise
    return []


# --- Функции для "везде" поиска (могут быть в другом файле, но для целостности примера) ---
async def search_origin_everywhere(
    destination: str,
    dest_iata: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    passenger_desc: str,
    state  # FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск из всех российских городов в один конкретный."""
    # Этот код обычно находится в everywhere_search.py
    # Импортируем константы или определяем здесь
    RUSSIAN_CITIES = ["MOW", "LED", "KJA", "NSK", "SPB"] # Пример
    all_flights = []
    for origin in RUSSIAN_CITIES:
        try:
            flights = await search_with_retries(
                origin=origin,
                destination=dest_iata,
                depart_date=depart_date,
                return_date=return_date,
                passengers=passengers_code
            )
            for f in flights:
                f["origin"] = origin
            all_flights.extend(flights)
            await asyncio.sleep(0.5)  # Rate limiting
        except Exception:
            continue # Пропускаем ошибки для отдельных городов

    return all_flights, "origin_everywhere"


async def search_destination_everywhere(
    origin: str,
    origin_iata: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    passenger_desc: str,
    state  # FSMContext
) -> Tuple[List[Dict], str]:
    """Поиск из одного города во все популярные."""
    # Этот код обычно находится в everywhere_search.py
    # Импортируем константы или определяем здесь
    POPULAR_DESTINATIONS = ["PAR", "LON", "BER", "ROM", "IST", "MIL"] # Пример
    all_flights = []
    for dest in POPULAR_DESTINATIONS:
        try:
            flights = await search_with_retries(
                origin=origin_iata,
                destination=dest,
                depart_date=depart_date,
                return_date=return_date,
                passengers=passengers_code
            )
            # Ограничиваем до топ-1 результата по цене для каждого направления
            if flights:
                cheapest = await get_cheapest_flight(flights)
                if cheapest:
                    cheapest["destination"] = dest
                    all_flights.append(cheapest)
            await asyncio.sleep(0.5)  # Rate limiting
        except Exception:
            continue # Пропускаем ошибки для отдельных направлений

    # Сортируем и ограничиваем топ-3
    all_flights.sort(key=lambda x: x.get("price", float('inf')))
    return all_flights[:3], "dest_everywhere"
