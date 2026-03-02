# test_flystack.py
"""
Тестовый скрипт для проверки FlyStack API
Не зависит от кода бота - можно запускать отдельно
"""

import os
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

# Конфигурация
FLYSTACK_BASE_URL = "https://api.flystack.dev/v1"
API_KEY = os.getenv("FLYSTACK_API_KEY", "").strip()

print("=" * 60)
print("🧪 ТЕСТ FLYSTACK API")
print("=" * 60)
print(f"📅 Дата запуска: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"🔑 API Key: {API_KEY[:4]}...{API_KEY[-4:] if len(API_KEY) > 8 else 'НЕ НАЙДЕН'}")
print("=" * 60)

async def test_api_key():
    """Проверка валидности API ключа"""
    print("\n🔍 ТЕСТ 1: Проверка API ключа...")
    
    if not API_KEY:
        print("❌ ОШИБКА: FLYSTACK_API_KEY не установлен в .env")
        return False
    
    if len(API_KEY) < 32:
        print(f"❌ ОШИБКА: API ключ слишком короткий ({len(API_KEY)} символов)")
        return False
    
    print("✅ API ключ найден и выглядит валидным")
    return True

async def test_airlines_endpoint():
    """Тест эндпоинта /airlines (самый простой)"""
    print("\n🔍 ТЕСТ 2: Эндпоинт /airlines...")
    
    url = f"{FLYSTACK_BASE_URL}/airlines"
    params = {"api_key": API_KEY, "limit": 3}
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                print(f"📡 Статус ответа: {resp.status}")
                
                if resp.status == 401:
                    print("❌ ОШИБКА 401: Неверный API ключ")
                    return False
                elif resp.status == 403:
                    print("❌ ОШИБКА 403: Доступ запрещён (проверьте ключ)")
                    return False
                elif resp.status == 429:
                    print("⚠️ ПРЕДУПРЕЖДЕНИЕ 429: Превышен лимит запросов")
                    return False
                elif resp.status != 200:
                    print(f"❌ ОШИБКА {resp.status}: {await resp.text()}")
                    return False
                
                data = await resp.json()
                airlines = data.get("data", [])
                print(f"✅ Получено {len(airlines)} авиакомпаний")
                
                if airlines:
                    print(f"📋 Пример: {airlines[0].get('name', 'N/A')} ({airlines[0].get('iata', 'N/A')})")
                
                return True
    except Exception as e:
        print(f"❌ ОШИБКА соединения: {e}")
        return False

async def test_flight_details():
    """Тест получения информации о рейсе"""
    print("\n🔍 ТЕСТ 3: Информация о рейсе (SU381)...")
    
    url = f"{FLYSTACK_BASE_URL}/flight"
    params = {
        "api_key": API_KEY,
        "airline": "SU",
        "flight_number": "381",
        "departure_date": "2026-03-15"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                print(f"📡 Статус ответа: {resp.status}")
                
                if resp.status == 404:
                    print("⚠️ Рейс не найден (это нормально для будущего)")
                    return True
                elif resp.status != 200:
                    print(f"❌ ОШИБКА {resp.status}: {await resp.text()}")
                    return False
                
                data = await resp.json()
                flight_data = data.get("data", {})
                
                if flight_data:
                    print("✅ Данные о рейсе получены:")
                    print(f"   ✈️ Самолёт: {flight_data.get('aircraft_type', 'Не указано')}")
                    print(f"   🍽️ Питание: {flight_data.get('meal_service', 'Не указано')}")
                    print(f"   🧳 Багаж: {flight_data.get('baggage_allowance', 'Не указано')}")
                    print(f"   📶 Wi-Fi: {flight_data.get('wifi', 'Не указано')}")
                    print(f"   ⚡ Статус: {flight_data.get('status', 'Не указано')}")
                else:
                    print("⚠️ Данные о рейсе пустые (это нормально)")
                
                return True
    except Exception as e:
        print(f"❌ ОШИБКА соединения: {e}")
        return False

async def test_airport_info():
    """Тест информации об аэропорте"""
    print("\n🔍 ТЕСТ 4: Информация об аэропорте (SVO)...")
    
    url = f"{FLYSTACK_BASE_URL}/airports"
    params = {
        "api_key": API_KEY,
        "iata_code": "SVO"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                print(f"📡 Статус ответа: {resp.status}")
                
                if resp.status != 200:
                    print(f"❌ ОШИБКА {resp.status}: {await resp.text()}")
                    return False
                
                data = await resp.json()
                airport_data = data.get("data", {})
                
                if airport_data:
                    print("✅ Данные об аэропорте получены:")
                    print(f"   🛫 Название: {airport_data.get('name', 'Не указано')}")
                    print(f"   📍 Город: {airport_data.get('city', 'Не указано')}")
                    print(f"   🌍 Страна: {airport_data.get('country', 'Не указано')}")
                    print(f"   🕐 Часовой пояс: {airport_data.get('timezone', 'Не указано')}")
                else:
                    print("⚠️ Данные об аэропорте пустые")
                
                return True
    except Exception as e:
        print(f"❌ ОШИБКА соединения: {e}")
        return False

async def test_airline_info():
    """Тест информации об авиакомпании"""
    print("\n🔍 ТЕСТ 5: Информация об авиакомпании (SU)...")
    
    url = f"{FLYSTACK_BASE_URL}/airlines"
    params = {
        "api_key": API_KEY,
        "iata_code": "SU"
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as resp:
                print(f"📡 Статус ответа: {resp.status}")
                
                if resp.status != 200:
                    print(f"❌ ОШИБКА {resp.status}: {await resp.text()}")
                    return False
                
                data = await resp.json()
                # API может вернуть список или объект
                airline_data = data.get("data", [])
                if isinstance(airline_data, list) and airline_data:
                    airline_data = airline_data[0]
                
                if airline_data:
                    print("✅ Данные об авиакомпании получены:")
                    print(f"   ✈️ Название: {airline_data.get('name', 'Не указано')}")
                    print(f"   🌍 Страна: {airline_data.get('country', 'Не указано')}")
                    print(f"   🔢 IATA: {airline_data.get('iata', 'Не указано')}")
                    print(f"   🌐 Сайт: {airline_data.get('website', 'Не указано')}")
                else:
                    print("⚠️ Данные об авиакомпании пустые")
                
                return True
    except Exception as e:
        print(f"❌ ОШИБКА соединения: {e}")
        return False

async def main():
    """Запуск всех тестов"""
    print("\n🚀 ЗАПУСК ТЕСТОВ...\n")
    
    results = []
    
    # Тест 1: API ключ
    results.append(("API ключ", await test_api_key()))
    
    if not results[0][1]:
        print("\n❌ ТЕСТЫ ПРЕРВАНЫ: Неверный API ключ")
        print_summary(results)
        return
    
    # Тест 2: Airlines endpoint
    results.append(("Эндпоинт /airlines", await test_airlines_endpoint()))
    
    # Тест 3: Flight details
    results.append(("Информация о рейсе", await test_flight_details()))
    
    # Тест 4: Airport info
    results.append(("Информация об аэропорте", await test_airport_info()))
    
    # Тест 5: Airline info
    results.append(("Информация об авиакомпании", await test_airline_info()))
    
    # Итоги
    print_summary(results)

def print_summary(results):
    """Вывод итогов тестирования"""
    print("\n" + "=" * 60)
    print("📊 ИТОГИ ТЕСТИРОВАНИЯ")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} | {test_name}")
    
    print("=" * 60)
    print(f"📈 Пройдено: {passed}/{total} тестов")
    
    if passed == total:
        print("🎉 ВСЕ ТЕСТЫ ПРОЙДЕНЫ! API работает корректно.")
    elif passed > 0:
        print("⚠️ ЧАСТЬ ТЕСТОВ ПРОЙДЕНА. Проверьте ошибки выше.")
    else:
        print("❌ ВСЕ ТЕСТЫ ПРОВАЛЕНЫ. Проверьте API ключ и соединение.")
    
    print("=" * 60 + "\n")

if __name__ == "__main__":
    asyncio.run(main())