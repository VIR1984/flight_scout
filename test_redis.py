import asyncio
from dotenv import load_dotenv

from utils.redis_client import redis_client


async def main():
    load_dotenv()

    try:
        print("→ Подключаемся к Redis...")
        await redis_client.connect()

        # ===================== TEST: SEARCH CACHE =====================

        cache_id = "test_key"
        test_data = {
            "from": "MOW",
            "to": "IST",
            "price": 12345
        }

        print("→ Записываем данные в кэш...")
        await redis_client.set_search_cache(cache_id, test_data, ttl=5)

        print("→ Читаем данные из кэша...")
        cached_data = await redis_client.get_search_cache(cache_id)
        print("✓ Получено:", cached_data)

        assert cached_data == test_data, "❌ Данные в кэше не совпадают"

        # ===================== TEST: TTL =====================

        print("→ Проверяем TTL (ждём 6 секунд)...")
        await asyncio.sleep(6)

        expired_data = await redis_client.get_search_cache(cache_id)
        print("✓ После TTL:", expired_data)

        assert expired_data is None, "❌ Кэш не удалился по TTL"

        # ===================== TEST: FIRST TIME USER =====================

        user_id = 123456

        print("→ Проверяем first_time_user...")
        first = await redis_client.is_first_time_user(user_id)
        second = await redis_client.is_first_time_user(user_id)

        print("✓ Первый раз:", first)
        print("✓ Второй раз:", second)

        assert first is True, "❌ Пользователь должен быть новым"
        assert second is False, "❌ Пользователь не должен быть новым"

        # ===================== TEST: MONITORING =====================

        count = await redis_client.get_search_cache_count()
        print("✓ Активных search-кэшей:", count)

        print("\n✅ ВСЕ ТЕСТЫ УСПЕШНО ПРОЙДЕНЫ")

    except Exception as e:
        print("\n❌ ОШИБКА ПРИ ТЕСТИРОВАНИИ:")
        print(e)
        import traceback
        traceback.print_exc()

    finally:
        print("\n→ Закрываем соединение с Redis...")
        await redis_client.close()
        print("✓ Соединение закрыто")


if __name__ == "__main__":
    asyncio.run(main())
