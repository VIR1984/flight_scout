# test_search.py
import asyncio
from services.flight_search import search_one_way

async def test():
    res = await search_one_way("MOW", "DXB", "15.03")
    print("Результат:", res)

asyncio.run(test())