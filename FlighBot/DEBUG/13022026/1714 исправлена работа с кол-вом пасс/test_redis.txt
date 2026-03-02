# test_redis.py
import asyncio
import os
from dotenv import load_dotenv
from utils.redis_client import redis_client

load_dotenv()


async def test():
    try:
        await redis_client.connect()

        if not redis_client.is_enabled():
            print("⚠️ Redis недоступен, тест не выполнен")
            return

        print("✓ Подключение к Redis успешно")

        # Тест записи
        await redis_client.set_search_cache("test_key", {"hello": "world"})
        data = await redis_client.get_search_cache("test_key")
        print(f"✓ Данные прочитаны: {data}")

        # Тест первого пользователя
        first_time = await redis_client.is_first_time_user(12345)
        print(f"Пользователь 12345 первый раз? {first_time}")

        # Очистка
        await redis_client.delete_search_cache("test_key")

        await redis_client.close()
        print("✓ Соединение закрыто")
    except Exception as e:
        import traceback
        print(f"✗ Ошибка: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test())
