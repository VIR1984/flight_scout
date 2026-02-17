import os
import asyncio
import aiohttp
import re
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from datetime import datetime
from utils.logger import logger


# Конфигурация API
AVIASALES_GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"
AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()

def normalize_date(date_str: str) -> str:
    """Преобразует дату ДД.ММ в формат ГГГГ-ММ-ДД для 2026 года (или 2027 для январь/февраль)"""
    try:
        day, month = map(int, date_str.split('.'))
        year = 2026
        if month < 2 or (month == 2 and day < 8):
            year = 2027
        return f"{year}-{month:02d}-{day:02d}"
    except Exception:
        return date_str

def format_avia_link_date(date_str: str) -> str:
    """Форматирует дату ДД.ММ → ДДММ для ссылки Aviasales"""
    try:
        day, month = date_str.split('.')
        return f"{day}{month}"
    except Exception:
        return date_str.replace('.', '')


async def search_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: Optional[str] = None,
    currency: str = "rub",
    direct: bool = False
) -> List[Dict]:
    """
    Ищет авиабилеты через Travelpayouts API (grouped_prices).
    Возвращает список рейсов, совместимый с остальным кодом.
    """
    if not AVIASALES_TOKEN:
        logger.warning("⚠️ AVIASALES_TOKEN не установлен — поиск авиабилетов недоступен")
        return []

    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart_date,
        "currency": currency,
        "token": AVIASALES_TOKEN,
        "group_by": "departure_at",
        "direct": "true" if direct else "false"
    }

    if return_date:
        params["return_at"] = return_date
        # Опционально: задать длительность поездки (в днях)===
        try:
            d1 = datetime.fromisoformat(depart_date)
            d2 = datetime.fromisoformat(return_date)
            trip_days = (d2 - d1).days
            if trip_days > 0:
                params["min_trip_duration"] = trip_days
                params["max_trip_duration"] = trip_days
        except Exception:
            pass  # игнорируем ошибки парсинга

    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(AVIASALES_GROUPED_URL, params=params, timeout=10) as response:
                if response.status == 429:
                    logger.warning("⚠️ Достигнут лимит API Aviasales (429). Ждём 60 секунд...")
                    await asyncio.sleep(60)
                    return []
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"❌ Ошибка API Aviasales: {response.status} - {error_text}")
                    return []
                
                data = await response.json()
                if not data.get("success"):
                    logger.error(f"❌ API вернул ошибку: {data.get('error')}")
                    return []
                
                grouped_flights = data.get("data", {})
                flights = []
                for date_key, flight in grouped_flights.items():
                    # Приводим к формату, совместимому с остальным кодом
                    flight["value"] = flight.get("price")
                    flight["departure_at"] = flight.get("departure_at", f"{date_key}T00:00:00+03:00")
                    flight["return_at"] = flight.get("return_at", "")
                    flight["origin"] = flight.get("origin", origin)
                    flight["destination"] = flight.get("destination", destination)
                    flights.append(flight)

                

                return flights

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к Aviasales API")
            return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к Aviasales API: {e}")
            return []


def generate_booking_link(
    flight: Dict,
    origin: str,
    dest: str,
    depart_date: str,
    passengers_code: str = "1",
    return_date: Optional[str] = None
) -> str:
    """
    Генерирует ссылку для бронирования на Aviasales с ПОЛНЫМ кодом пассажиров.
    """
    print(f"[DEBUG generate_booking_link] Вход: origin='{origin}', dest='{dest}', depart_date='{depart_date}', return_date='{return_date}', passengers_code='{passengers_code}'")
    
    # Валидация и нормализация кода пассажиров
    if not passengers_code or not isinstance(passengers_code, str):
        passengers_code = "1"
    # Убираем всё кроме цифр и оставляем максимум 3 цифры
    passengers_code = re.sub(r'\D', '', passengers_code)[:3]
    # Если после очистки пусто или начинается с 0 — используем "1"
    if not passengers_code or passengers_code[0] == '0':
        passengers_code = "1"
    
    print(f"[DEBUG generate_booking_link] Нормализованный passengers_code: '{passengers_code}'")
    
    # Форматируем даты для ссылки (ДДММ)
    d1 = format_avia_link_date(depart_date)
    d2 = format_avia_link_date(return_date) if return_date else ""
    print(f"[DEBUG generate_booking_link] Отформатированные даты: d1='{d1}', d2='{d2}'")
    
    # Формируем маршрут с ПОЛНЫМ кодом пассажиров
    if return_date:
        # Туда-обратно: MOW1003AER1503211
        route = f"{origin}{d1}{dest}{d2}{passengers_code}"
    else:
        # В одну сторону: AER1003MOW211
        route = f"{origin}{d1}{dest}{passengers_code}"
    
    print(f"[DEBUG generate_booking_link] Сформированный маршрут: '{route}'")
    
    base_url = f"https://www.aviasales.ru/search/{route}"
    print(f"[DEBUG generate_booking_link] Сгенерирована ЧИСТАЯ ссылка: '{base_url}'")
    
    # Возвращаем ЧИСТУЮ ссылку без маркера (преобразование происходит в start.py/everywhere_search.py)
    return base_url

def find_cheapest_flight_on_exact_date(
    flights: List[Dict],
    requested_depart_date: str,
    requested_return_date: Optional[str] = None
) -> Optional[Dict]:
    """
    Находит самый дешёвый рейс, соответствующий *точно* запрошенным датам.
    """
    exact_flights = []
    req_depart = normalize_date(requested_depart_date)
    req_return = normalize_date(requested_return_date) if requested_return_date else None

    for flight in flights:
        flight_depart = flight.get("departure_at", "")[:10]
        flight_return = flight.get("return_at", "")[:10] if flight.get("return_at") else None

        if flight_depart == req_depart:
            if req_return:
                if flight_return and flight_return == req_return:
                    exact_flights.append(flight)
            else:
                exact_flights.append(flight)

    if not exact_flights:
        return min(flights, key=lambda f: f.get("value") or f.get("price") or 999999999)
    return min(exact_flights, key=lambda f: f.get("value") or f.get("price") or 999999999)


    
    
    # Добавлено тестирование
def update_passengers_in_link(link: str, passengers_code: str) -> str:
    """Обновляет количество пассажиров в ссылке"""
    print(f"[DEBUG update_passengers_in_link] Вход: link='{link}', passengers_code='{passengers_code}'")
    
    # Определяем тип ссылки (абсолютная или относительная)
    is_relative = not link.startswith(('http://', 'https://'))
    if is_relative:
        path = link
        prefix = ''
    else:
        parsed = urlparse(link)
        path = parsed.path
        prefix = f"{parsed.scheme}://{parsed.netloc}"
    
    print(f"[DEBUG update_passengers_in_link] Извлеченный путь: '{path}'")
    print(f"[DEBUG update_passengers_in_link] Тип ссылки: {'относительная' if is_relative else 'абсолютная'}")
    
    # Ищем часть после /search/
    if '/search/' in path:
        route_part = path.split('/search/', 1)[1]
        print(f"[DEBUG update_passengers_in_link] Префикс пути: '{prefix + '/search/'}'")
        print(f"[DEBUG update_passengers_in_link] Часть после /search/: '{route_part}'")
        
        # Проверяем, есть ли строка запроса
        if '?' in route_part:
            route, query = route_part.split('?', 1)
            has_query = True
            print(f"[DEBUG update_passengers_in_link] Найдена строка запроса. Маршрут: '{route}', параметры: '{query}'")
        else:
            route = route_part
            query = ''
            has_query = False
            print(f"[DEBUG update_passengers_in_link] Строки запроса нет. Маршрут: '{route}'")
        
        # Определяем длину кода пассажиров (должна быть 1 цифра!)
        passengers_digits = 1  # Aviasales ожидает ТОЛЬКО количество взрослых здесь
        
        # Извлекаем код пассажиров (последняя цифра)
        if len(route) > passengers_digits and route[-passengers_digits:].isdigit():
            old_passengers = route[-passengers_digits:]
            new_route = route[:-passengers_digits] + passengers_code[0]  # Берем ТОЛЬКО количество взрослых
            print(f"[DEBUG update_passengers_in_link] Заменяем код пассажиров. Было: '{old_passengers}', стало: '{passengers_code[0]}'")
        else:
            new_route = route + passengers_code[0]  # Добавляем количество взрослых
            print(f"[DEBUG update_passengers_in_link] Добавляем код пассажиров. Было: '{route}', стало: '{new_route}'")
        
        # ДОБАВЛЯЕМ полную информацию о пассажирах в параметры запроса
        if has_query:
            # Парсим параметры запроса
            query_params = parse_qs(query)
            
            # Обновляем параметры пассажиров
            query_params['passengers'] = [{
                'adults': int(passengers_code[0]),
                'children': int(passengers_code[1]) if len(passengers_code) > 1 else 0,
                'infants': int(passengers_code[2]) if len(passengers_code) > 2 else 0
            }]
            
            # Формируем новую строку запроса
            new_query = urlencode(query_params, doseq=True)
            new_path = f"/search/{new_route}?{new_query}"
            print(f"[DEBUG update_passengers_in_link] Обновленные параметры запроса: '{new_query}'")
        else:
            new_path = f"/search/{new_route}"
        
        print(f"[DEBUG update_passengers_in_link] Сформирован новый путь: '{new_path}'")
    else:
        # Это не ссылка поиска - возвращаем как есть
        print(f"[DEBUG update_passengers_in_link] Это не ссылка поиска - возвращаем как есть: '{link}'")
        return link
    
    # Возвращаем в исходном формате (сохраняем относительность/абсолютность)
    result = new_path if is_relative else urlunparse(parsed._replace(path=new_path))
    print(f"[DEBUG update_passengers_in_link] Выход: '{result}'")
    return result


    
def parse_passengers(s: str) -> str:
    """
    Парсит строку с пассажирами и возвращает код пассажиров.
    Примеры:
    - "2 взр" → "2"
    - "2 взр, 1 реб" → "21"
    - "2 взр, 1 мл" → "201"
    """
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
    
    # Формируем код пассажиров
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    
    return code

def format_passenger_desc(code: str) -> str:
    """
    Форматирует код пассажиров в читаемое описание.
    Примеры:
    - "1" → "1 взр."
    - "21" → "2 взр., 1 реб."
    - "211" → "2 взр., 1 реб., 1 мл."
    """
    try:
        adults = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants = int(code[2]) if len(code) > 2 else 0
        
        parts = []
        if adults:
            parts.append(f"{adults} взр.")
        if children:
            parts.append(f"{children} реб.")
        if infants:
            parts.append(f"{infants} мл.")
        
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."
        
def format_duration(minutes: int) -> str:
    """
    Форматирует длительность полёта из минут в читаемый вид.
    Примеры:
    - 125 → "2ч 5м"
    - 60 → "1ч"
    - 30 → "30м"
    - 0 → "—"
    """
    if not minutes:
        return "—"
    hours = minutes // 60
    mins = minutes % 60
    parts = []
    if hours:
        parts.append(f"{hours}ч")
    if mins:
        parts.append(f"{mins}м")
    return " ".join(parts) if parts else "—"
