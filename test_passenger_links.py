#!/usr/bin/env python3
"""
Тестовый скрипт для отладки функции обновления количества пассажиров в ссылках Aviasales.
Проверяет корректность замены последней цифры маршрута (всегда "1" от API) на полный код пассажиров.
"""
import asyncio
import os
import sys
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Добавляем корневую директорию проекта в sys.path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from services.flight_search import (
    search_flights,
    update_passengers_in_link,
    generate_booking_link,
    normalize_date,
    format_avia_link_date,
    parse_passengers,
    format_passenger_desc
)
from utils.cities import CITY_TO_IATA, IATA_TO_CITY

if not os.getenv("AVIASALES_TOKEN"):
    # Если нет - устанавливаем явно
    os.environ["AVIASALES_TOKEN"] = "1caae407b6969cff40dec4a4a7b8f03a"
    print("🔧 Токен установлен вручную для теста")
else:
    print(f"🔧 Токен загружен из окружения: {os.getenv('AVIASALES_TOKEN', '')[:8]}...")

# Загружаем .env (только если нужно для других переменных)
load_dotenv(override=False) 

def print_header():
    print("\n" + "="*80)
    print("🧪 ТЕСТ ФУНКЦИИ ОБНОВЛЕНИЯ ПАССАЖИРОВ В ССЫЛКАХ AVIASALES")
    print("="*80)
    print("\nℹ️  Логика работы:")
    print("   • Ссылки от API всегда заканчиваются цифрой '1' (1 пассажир)")
    print("   • Наша задача: заменить эту '1' на полный код пассажиров (например, '211')")
    print("   • Формат кода: [взрослые][дети][младенцы] (1-3 цифры, первая не 0)")
    print("   • Пример: /search/MOW1903BCN26031 → /search/MOW1903BCN2603211")

def print_section(title):
    print(f"\n{'─'*80}")
    print(f"📌 {title}")
    print(f"{'─'*80}")

def analyze_link(link: str) -> dict:
    """Анализирует структуру ссылки и извлекает информацию"""
    result = {
        "is_relative": link.startswith('/'),
        "has_query": '?' in link,
        "passenger_digit": None,
        "route_part": None,
        "query_params": {}
    }
    
    # Извлекаем маршрутную часть
    if link.startswith('/'):
        path = link
    else:
        parsed = urlparse(link)
        path = parsed.path
        result["query_params"] = parse_qs(parsed.query)
    
    if '/search/' in path:
        route_part = path.split('/search/', 1)[1]
        if '?' in route_part:
            route_part = route_part.split('?')[0]
        result["route_part"] = route_part
        
        # === ИСПРАВЛЕНИЕ: извлекаем ПОСЛЕДНЮЮ цифру в маршруте (код пассажиров) ===
        # Ищем последнюю цифру в конце маршрута
        if route_part:
            # Находим последовательность цифр в конце строки
            import re
            match = re.search(r'(\d+)$', route_part)
            if match:
                result["passenger_digit"] = match.group(1)  # Например: '211'
    
    return result

def validate_passengers_code(code: str) -> bool:
    """Проверяет валидность кода пассажиров"""
    return bool(re.match(r'^[1-9]\d{0,2}$', code))

async def test_with_api():
    """Тест через вызов реального API Aviasales"""
    print_section("РЕЖИМ 1: ТЕСТ ЧЕРЕЗ API AVIASALES")
    
    # Запрашиваем данные у пользователя
    origin_city = await asyncio.get_event_loop().run_in_executor(
        None, input, "📍 Город вылета (например, Москва): "
    )
    dest_city = await asyncio.get_event_loop().run_in_executor(
        None, input, "📍 Город прилета (например, Сочи): "
    )
    
    # Преобразуем в IATA коды
    origin_iata = CITY_TO_IATA.get(origin_city.strip().lower())
    dest_iata = CITY_TO_IATA.get(dest_city.strip().lower())
    
    if not origin_iata:
        print(f"\n❌ Неизвестный город вылета: '{origin_city}'")
        print(f"   Доступные города: {', '.join(list(CITY_TO_IATA.keys())[:10])}...")
        return
    if not dest_iata:
        print(f"\n❌ Неизвестный город прилета: '{dest_city}'")
        print(f"   Доступные города: {', '.join(list(CITY_TO_IATA.keys())[:10])}...")
        return
    
    print(f"\n✅ Города распознаны:")
    print(f"   Вылет: {IATA_TO_CITY.get(origin_iata, origin_iata)} ({origin_iata})")
    print(f"   Прилет: {IATA_TO_CITY.get(dest_iata, dest_iata)} ({dest_iata})")
    
    depart_date = await asyncio.get_event_loop().run_in_executor(
        None, input, "\n📅 Дата вылета (ДД.ММ, например 10.03): "
    )
    while not re.match(r'^\d{1,2}\.\d{1,2}$', depart_date.strip()):
        print("❌ Неверный формат даты. Используйте ДД.ММ (например, 10.03)")
        depart_date = await asyncio.get_event_loop().run_in_executor(
            None, input, "📅 Дата вылета (ДД.ММ): "
        )
    
    return_date_input = await asyncio.get_event_loop().run_in_executor(
        None, input, "📅 Дата возврата (оставьте пустым для одного направления): "
    )
    return_date = return_date_input.strip() if return_date_input.strip() else None
    
    if return_date and not re.match(r'^\d{1,2}\.\d{1,2}$', return_date):
        print("❌ Неверный формат даты возврата. Используйте ДД.ММ (например, 15.03)")
        return
    
    passengers_input = await asyncio.get_event_loop().run_in_executor(
        None, input, "👥 Пассажиры (примеры: '2 взр', '211', '1 взр, 1 реб'): "
    )
    
    # Парсим пассажиров
    passengers_code = parse_passengers(passengers_input.strip())
    passenger_desc = format_passenger_desc(passengers_code)
    
    if not validate_passengers_code(passengers_code):
        print(f"\n❌ Неверный код пассажиров: '{passengers_code}'")
        print("   Код должен быть от 1 до 999, первая цифра не может быть 0")
        return
    
    print(f"\n✅ Пассажиры распознаны: {passenger_desc} (код: {passengers_code})")
    
    # Нормализуем даты
    depart_date_normalized = normalize_date(depart_date.strip())
    return_date_normalized = normalize_date(return_date.strip()) if return_date else None
    
    print_section("ВЫПОЛНЕНИЕ ПОИСКА")
    print(f"🔍 Запрос к API Aviasales...")
    print(f"   Маршрут: {origin_iata} → {dest_iata}")
    print(f"   Вылет: {depart_date} → {depart_date_normalized}")
    if return_date:
        print(f"   Возврат: {return_date} → {return_date_normalized}")
    print(f"   Пассажиры: {passenger_desc} (код: {passengers_code})")
    
    # Выполняем поиск
    try:
        flights = await search_flights(
            origin=origin_iata,
            destination=dest_iata,
            depart_date=depart_date_normalized,
            return_date=return_date_normalized
        )
        
        if not flights:
            print("\n❌ Рейсы не найдены.")
            print("   Совет: попробуйте другие даты или направление")
            return
        
        # Берем первый рейс
        first_flight = flights[0]
        original_link = first_flight.get("link") or first_flight.get("deep_link")
        
        if not original_link:
            print("\n❌ В ответе API нет ссылки на бронирование.")
            print(f"   Ответ API: {first_flight}")
            return
        
        print(f"\n✅ Найдено {len(flights)} рейсов. Анализирую первый...")
        
        # Анализируем исходную ссылку
        link_analysis = analyze_link(original_link)
        
        print_section("ИСХОДНАЯ ССЫЛКА ОТ API")
        print(f"🔗 Ссылка:")
        print(f"   {original_link}")
        print(f"\n📊 Анализ ссылки:")
        print(f"   • Тип: {'относительная' if link_analysis['is_relative'] else 'абсолютная'}")
        print(f"   • Маршрут в ссылке: {link_analysis['route_part'] or 'не найден'}")
        print(f"   • Пассажиры в ссылке (последняя цифра): {link_analysis['passenger_digit'] or 'не найдена'}")
        if link_analysis['query_params']:
            print(f"   • Параметры запроса: {', '.join(link_analysis['query_params'].keys())}")
        
        # Модифицируем ссылку
        print_section("МОДИФИКАЦИЯ ССЫЛКИ")
        print(f"✏️  Обновляю количество пассажиров с '1' на '{passengers_code}'...")
        
        modified_link = update_passengers_in_link(original_link, passengers_code)
        modified_analysis = analyze_link(modified_link)
        
        print(f"\n✅ Модифицированная ссылка:")
        print(f"   {modified_link}")
        print(f"\n📊 Анализ модифицированной ссылки:")
        print(f"   • Новый маршрут: {modified_analysis['route_part'] or 'не найден'}")
        print(f"   • Пассажиры в ссылке (последняя цифра): {modified_analysis['passenger_digit'] or 'не найдена'}")
        
        # Генерируем ссылку через generate_booking_link для сравнения
        print_section("СРАВНЕНИЕ С ГЕНЕРИРУЕМОЙ ССЫЛКОЙ")
        generated_link = generate_booking_link(
            flight=first_flight,
            origin=origin_iata,
            dest=dest_iata,
            depart_date=depart_date.strip(),
            passengers_code=passengers_code,
            return_date=return_date
        )
        
        print(f"🔗 Ссылка через generate_booking_link:")
        print(f"   {generated_link}")
        
        # Сравнение
        print_section("РЕЗУЛЬТАТЫ ТЕСТА")
        original_passengers = link_analysis['passenger_digit'] or 'N/A'
        modified_passengers = modified_analysis['passenger_digit'] or 'N/A'
        
        print(f"✅ Исходные пассажиры в ссылке API: {original_passengers}")
        print(f"✅ Целевые пассажиры: {passengers_code}")
        print(f"✅ Пассажиры в модифицированной ссылке: {modified_passengers}")
        
        if modified_passengers == passengers_code:
            print(f"\n🎉 УСПЕХ: Количество пассажиров корректно обновлено!")
        else:
            print(f"\n❌ ОШИБКА: Пассажиры не обновлены корректно!")
            print(f"   Ожидалось: {passengers_code}, получено: {modified_passengers}")
        
        print(f"\n🔍 Рекомендация:")
        print(f"   Скопируйте модифицированную ссылку и проверьте в браузере:")
        print(f"   {modified_link}")
        
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()

async def test_manual_link():
    """Тест с ручным вводом ссылки от API"""
    print_section("РЕЖИМ 2: РУЧНОЙ ТЕСТ ССЫЛКИ")
    
    print("ℹ️  Вставьте ссылку из ответа API Aviasales")
    print("    Примеры:")
    print("    • /search/MOW0111BCN1?t=...")
    print("    • https://www.aviasales.ru/search/MOW1903BCN26031?t=...")
    
    original_link = await asyncio.get_event_loop().run_in_executor(
        None, input, "\n🔗 Вставьте ссылку: "
    )
    original_link = original_link.strip()
    
    if not original_link:
        print("❌ Ссылка не введена.")
        return
    
    passengers_input = await asyncio.get_event_loop().run_in_executor(
        None, input, "👥 Пассажиры (примеры: '2 взр', '211', '1 взр, 1 реб'): "
    )
    
    passengers_code = parse_passengers(passengers_input.strip())
    passenger_desc = format_passenger_desc(passengers_code)
    
    if not validate_passengers_code(passengers_code):
        print(f"\n❌ Неверный код пассажиров: '{passengers_code}'")
        return
    
    print(f"\n✅ Пассажиры: {passenger_desc} (код: {passengers_code})")
    
    # Анализируем исходную ссылку
    link_analysis = analyze_link(original_link)
    
    print_section("ИСХОДНАЯ ССЫЛКА")
    print(f"🔗 Ссылка:")
    print(f"   {original_link}")
    print(f"\n📊 Анализ:")
    print(f"   • Пассажиры в ссылке: {link_analysis['passenger_digit'] or 'не найдена'}")
    
    # Модифицируем ссылку
    print_section("МОДИФИКАЦИЯ")
    print(f"✏️  Обновляю количество пассажиров на '{passengers_code}'...")
    
    modified_link = update_passengers_in_link(original_link, passengers_code)
    modified_analysis = analyze_link(modified_link)
    
    print(f"\n✅ Результат:")
    print(f"   {modified_link}")
    print(f"\n📊 Анализ результата:")
    print(f"   • Пассажиры в ссылке: {modified_analysis['passenger_digit'] or 'не найдена'}")
    
    # Проверка
    print_section("ПРОВЕРКА")
    expected = passengers_code
    actual = modified_analysis['passenger_digit'] or 'N/A'
    
    if actual == expected:
        print(f"✅ УСПЕХ: Пассажиры корректно обновлены с '1' на '{expected}'")
    else:
        print(f"❌ ОШИБКА: Ожидалось '{expected}', получено '{actual}'")
    
    print(f"\n🔍 Проверьте ссылку в браузере:")
    print(f"   {modified_link}")

async def run_comprehensive_test():
    """Запускает набор автоматических тестов"""
    print_section("АВТОМАТИЧЕСКИЕ ТЕСТЫ")
    
    test_cases = [
        ("/search/MOW0111BCN1?t=...", "21", "/search/MOW0111BCN21?t=..."),
        ("/search/MOW1903BCN26031?t=...", "3", "/search/MOW1903BCN26033?t=..."),
        ("/search/MOW1903BCN26031", "211", "/search/MOW1903BCN2603211"),
        ("/search/LED1505DXB1", "2", "/search/LED1505DXB2"),
        ("https://www.aviasales.ru/search/MOW1903BCN26031?t=...", "321", 
         "https://www.aviasales.ru/search/MOW1903BCN2603321?t=..."),
        ("/search/DME1006AER1", "1", "/search/DME1006AER1"),
        ("/search/SVO2007LED1", "9", "/search/SVO2007LED9"),
        ("/search/KZN0508OVB1", "12", "/search/KZN0508OVB12"),
        ("/search/UFA1509KJA1", "321", "/search/UFA1509KJA321"),
    ]
    
    passed = 0
    failed = 0
    
    for original, code, expected in test_cases:
        result = update_passengers_in_link(original, code)
        status = "✅" if result == expected else "❌"
        
        if result == expected:
            passed += 1
        else:
            failed += 1
        
        print(f"{status} Вход: {original[:40]}... | Код: {code} → Результат: {result == expected}")
        if result != expected:
            print(f"   Ожидалось: {expected}")
            print(f"   Получено:  {result}")
    
    print_section("ИТОГИ АВТОТЕСТОВ")
    print(f"✅ Пройдено: {passed}/{len(test_cases)}")
    print(f"❌ Ошибок: {failed}/{len(test_cases)}")
    
    if failed == 0:
        print("\n🎉 Все тесты пройдены успешно!")
    else:
        print("\n⚠️  Некоторые тесты не пройдены. Проверьте логику функции update_passengers_in_link")

async def main():
    print_header()
    
    # Проверяем наличие токена
    if not os.getenv("AVIASALES_TOKEN"):
        print("\n⚠️  ВНИМАНИЕ: Не найден AVIASALES_TOKEN в .env файле")
        print("   Режим тестирования через API будет недоступен")
        print("   Для работы API-тестов добавьте в .env: AVIASALES_TOKEN=ваш_токен")
    
    while True:
        print("\n" + "="*80)
        print("Выберите режим тестирования:")
        print("1. Тест через реальный API Aviasales (требуется AVIASALES_TOKEN)")
        print("2. Ручной тест с вводом ссылки")
        print("3. Автоматические тесты (набор проверок)")
        print("0. Выход")
        print("="*80)
        
        choice = await asyncio.get_event_loop().run_in_executor(
            None, input, "\nВаш выбор (0-3): "
        )
        choice = choice.strip()
        
        if choice == "1":
            if not os.getenv("AVIASALES_TOKEN"):
                print("\n❌ AVIASALES_TOKEN не найден. Добавьте его в .env файл")
                continue
            await test_with_api()
        elif choice == "2":
            await test_manual_link()
        elif choice == "3":
            await run_comprehensive_test()
        elif choice == "0":
            print("\n👋 Выход из теста. Удачи в отладке!")
            break
        else:
            print("\n❌ Неверный выбор. Введите число от 0 до 3.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Тест прерван пользователем. До свидания!")