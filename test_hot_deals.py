#!/usr/bin/env python3
"""
test_hot_deals.py — тест горячих предложений и дайджеста.

Запуск:
    cd <папка_бота>
    python test_hot_deals.py

Требует переменных окружения (файл .env или экспорт):
    AVIASALES_TOKEN=...
    REDIS_URL=redis://localhost:6379/0       # необязательно для dry-run
    BOT_TOKEN=...                            # необязательно, только для реальной отправки

Режимы:
    python test_hot_deals.py              — сухой прогон (no Redis, no send)
    python test_hot_deals.py --send       — отправить уведомление на TELEGRAM_TEST_USER_ID
    python test_hot_deals.py --special    — проверить Special Offers API
"""

import os
import sys
import asyncio
import aiohttp
import json
import time
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple

# ── Загружаем .env если есть ──────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

AVIASALES_TOKEN = os.getenv("AVIASALES_TOKEN", "").strip()
BOT_TOKEN       = os.getenv("BOT_TOKEN", "").strip()
TEST_USER_ID    = int(os.getenv("TELEGRAM_TEST_USER_ID", "0"))
REDIS_URL       = os.getenv("REDIS_URL", "")
SEND_REAL       = "--send" in sys.argv
CHECK_SPECIAL   = "--special" in sys.argv

# ── Цвета для вывода ──────────────────────────────────────────────────────────
OK   = "✅"
FAIL = "❌"
WARN = "⚠️ "
INFO = "ℹ️ "
SEP  = "─" * 60

CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",   ["AYT", "HRG", "RHO", "DLM", "AER", "HER"]),
    "city":   ("🏙️ Городские поездки", ["IST", "BCN", "CDG", "FCO", "PRG", "BUD"]),
    "world":  ("🌍 Путешествия по миру",["DXB", "BKK", "SIN", "HKT", "ICN"]),
    "russia": ("🇷🇺 По России",          ["AER", "LED", "KZN", "OVB", "SVX", "KRR"]),
}

# ── Тестовые подписки (имитируем реальные данные из Redis) ────────────────────
TEST_SUBS = [
    {
        "sub_type":     "hot",
        "origin_iata":  "MOW",
        "origin_name":  "Москва",
        "category":     "sea",
        "max_price":    30000,
        "passengers":   2,
        "travel_months": [f"{(date.today().month % 12) + 1}_{date.today().year + (1 if date.today().month == 12 else 0)}"],
        "last_notified": 0,
    },
    {
        "sub_type":     "hot",
        "origin_iata":  "LED",
        "origin_name":  "Санкт-Петербург",
        "category":     "city",
        "max_price":    25000,
        "passengers":   1,
        "travel_months": [],
        "last_notified": 0,
    },
    {
        "sub_type":     "digest",
        "frequency":    "daily",
        "origin_iata":  "MOW",
        "origin_name":  "Москва",
        "category":     "world",
        "max_price":    0,
        "passengers":   1,
        "travel_months": [],
        "last_notified": 0,
    },
]


# ════════════════════════════════════════════════════════════════
# 1. Базовые проверки окружения
# ════════════════════════════════════════════════════════════════

def check_env():
    print(f"\n{SEP}")
    print("1. ПРОВЕРКА ОКРУЖЕНИЯ")
    print(SEP)

    token_ok = bool(AVIASALES_TOKEN)
    print(f"  {OK if token_ok else FAIL} AVIASALES_TOKEN: {'задан (' + AVIASALES_TOKEN[:6] + '...)' if token_ok else 'НЕ ЗАДАН — поиск рейсов не будет работать!'}")

    redis_ok = bool(REDIS_URL)
    print(f"  {OK if redis_ok else WARN} REDIS_URL: {'задан' if redis_ok else 'не задан — подписки не хранятся, тест в dry-run режиме'}")

    bot_ok = bool(BOT_TOKEN)
    print(f"  {OK if bot_ok else WARN} BOT_TOKEN: {'задан' if bot_ok else 'не задан — реальная отправка недоступна'}")

    if SEND_REAL:
        uid_ok = TEST_USER_ID > 0
        print(f"  {OK if uid_ok else FAIL} TELEGRAM_TEST_USER_ID: {TEST_USER_ID if uid_ok else 'НЕ ЗАДАН — укажите свой user_id для теста --send'}")

    return token_ok


# ════════════════════════════════════════════════════════════════
# 2. Тест API поиска рейсов (grouped_prices)
# ════════════════════════════════════════════════════════════════

async def test_flight_search(origin: str, dest: str, depart_date: str) -> Optional[Dict]:
    """Тест одного направления через grouped_prices API."""
    params = {
        "origin":       origin,
        "destination":  dest,
        "departure_at": depart_date,
        "currency":     "rub",
        "token":        AVIASALES_TOKEN,
        "group_by":     "departure_at",
        "direct":       "false",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.travelpayouts.com/aviasales/v3/grouped_prices",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    return {"_error": "rate_limit_429"}
                if resp.status != 200:
                    text = await resp.text()
                    return {"_error": f"HTTP {resp.status}: {text[:100]}"}
                data = await resp.json()
                if not data.get("success"):
                    return {"_error": data.get("error", "unknown")}
                flights = list(data.get("data", {}).values())
                if not flights:
                    return None
                cheapest = min(flights, key=lambda f: f.get("price") or 999999)
                cheapest["_source"] = "grouped_prices"
                return cheapest
    except asyncio.TimeoutError:
        return {"_error": "timeout"}
    except Exception as e:
        return {"_error": str(e)}


async def check_api():
    print(f"\n{SEP}")
    print("2. ТЕСТ API ПОИСКА РЕЙСОВ (grouped_prices)")
    print(SEP)

    if not AVIASALES_TOKEN:
        print(f"  {FAIL} Пропускаем — нет токена")
        return False

    # Тестируем 3 направления
    test_routes = [
        ("MOW", "AER", (date.today() + timedelta(days=30)).strftime("%Y-%m")),
        ("MOW", "IST", (date.today() + timedelta(days=45)).strftime("%Y-%m")),
        ("LED", "AYT", (date.today() + timedelta(days=60)).strftime("%Y-%m")),
    ]

    ok_count = 0
    for origin, dest, dep in test_routes:
        result = await test_flight_search(origin, dest, dep)
        if result and "_error" not in result:
            price = result.get("price") or result.get("value", "?")
            transfers = result.get("transfers", "?")
            airline = result.get("airline", "?")
            print(f"  {OK} {origin} → {dest} ({dep}): {price} ₽, {transfers} пересадок, {airline}")
            ok_count += 1
        elif result and result.get("_error") == "rate_limit_429":
            print(f"  {WARN} {origin} → {dest}: Rate limit 429 — подождите 60с")
        elif result is None:
            print(f"  {WARN} {origin} → {dest}: Нет данных в кэше на {dep}")
        else:
            print(f"  {FAIL} {origin} → {dest}: {result.get('_error')}")

    api_ok = ok_count > 0
    print(f"\n  Итого: {ok_count}/{len(test_routes)} направлений вернули данные")
    return api_ok


# ════════════════════════════════════════════════════════════════
# 3. Тест Special Offers API
# ════════════════════════════════════════════════════════════════

async def check_special_offers():
    print(f"\n{SEP}")
    print("3. SPECIAL OFFERS API (v2/prices/special-offers)")
    print(SEP)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://api.travelpayouts.com/v2/prices/special-offers",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                print(f"  {INFO} HTTP статус: {resp.status}")
                print(f"  {INFO} Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                content = await resp.text()
                print(f"  {INFO} Размер ответа: {len(content)} байт")
                print(f"  {INFO} Первые 300 символов:\n    {content[:300]}")

                if resp.status == 200:
                    print(f"\n  {OK} API отвечает — формат XML")
                    print(f"  {INFO} Этот endpoint возвращает акционные предложения авиакомпаний в XML.")
                    print(f"  {INFO} Для горячих предложений в боте НЕ используется — там grouped_prices.")
                    print(f"  {WARN} Рекомендация: использовать для отдельного раздела 'Акции авиакомпаний'.")
                else:
                    print(f"  {WARN} Нестандартный статус {resp.status}")
    except Exception as e:
        print(f"  {FAIL} Ошибка: {e}")


# ════════════════════════════════════════════════════════════════
# 4. Симуляция логики горячих предложений
# ════════════════════════════════════════════════════════════════

async def simulate_hot_sub(sub: dict) -> dict:
    """Симулирует _check_hot_sub без Redis и без отправки."""
    origin   = sub["origin_iata"]
    cat_name, dests = CATEGORIES.get(sub["category"], ("", []))
    max_price = sub.get("max_price", 0)

    # Дата поиска
    today = date.today()
    months = sub.get("travel_months", [])
    search_date = today + timedelta(days=30)
    for mk in months:
        try:
            m, y = map(int, mk.split("_"))
            candidate = date(y, m, 15)
            if candidate >= today:
                search_date = candidate
                break
        except Exception:
            pass

    depart_str = search_date.strftime("%Y-%m")
    scan_dests = [d for d in dests if d != origin]

    results = []
    print(f"\n    Сканируем {len(scan_dests)} направлений из {origin} на {depart_str}:")

    for dest in scan_dests[:5]:  # ограничиваем до 5 для скорости теста
        flight = await test_flight_search(origin, dest, depart_str)
        if flight and "_error" not in flight:
            price = flight.get("price") or flight.get("value") or 0
            if price:
                budget_ok = (max_price == 0 or price <= max_price)
                status = OK if budget_ok else WARN
                note = "" if budget_ok else f" (выше бюджета {max_price}₽)"
                print(f"      {status} {origin}→{dest}: {price}₽{note}")
                if budget_ok:
                    results.append((price, dest, flight))
        elif flight and flight.get("_error") == "rate_limit_429":
            print(f"      {WARN} {dest}: rate limit")
            await asyncio.sleep(5)
        else:
            print(f"      — {dest}: нет данных")
        await asyncio.sleep(0.3)

    if results:
        results.sort(key=lambda x: x[0])
        best_price, best_dest, best_flight = results[0]
        return {"found": True, "price": best_price, "dest": best_dest, "flight": best_flight}
    return {"found": False}


async def check_hot_logic():
    print(f"\n{SEP}")
    print("4. СИМУЛЯЦИЯ ЛОГИКИ ГОРЯЧИХ ПРЕДЛОЖЕНИЙ")
    print(SEP)

    if not AVIASALES_TOKEN:
        print(f"  {FAIL} Пропускаем — нет токена")
        return

    hot_subs   = [s for s in TEST_SUBS if s["sub_type"] == "hot"]
    digest_subs = [s for s in TEST_SUBS if s["sub_type"] == "digest"]

    print(f"\n  Тестовых горячих подписок: {len(hot_subs)}")
    print(f"  Тестовых дайджест-подписок: {len(digest_subs)}")

    # Тест горячих
    for i, sub in enumerate(hot_subs, 1):
        cat_label = CATEGORIES.get(sub["category"], ("",))[0]
        print(f"\n  [{i}] Горячая подписка: {sub['origin_name']} → {cat_label} (бюджет: {sub['max_price'] or 'любой'}₽)")
        result = await simulate_hot_sub(sub)
        if result["found"]:
            print(f"    {OK} Найден кандидат: {sub['origin_iata']}→{result['dest']} за {result['price']}₽")
            print(f"    {OK} Уведомление БУДЕТ отправлено")
        else:
            print(f"    {WARN} Нет кандидатов — уведомление не будет отправлено")
            print(f"    {INFO} Причины: нет рейсов в кэше / все выше бюджета / rate limit")

    # Тест дайджест
    for i, sub in enumerate(digest_subs, 1):
        cat_label = CATEGORIES.get(sub["category"], ("",))[0]
        print(f"\n  [{i}] Дайджест: {sub['origin_name']} → {cat_label}")
        result = await simulate_hot_sub(sub)
        if result["found"]:
            print(f"    {OK} Дайджест найдёт предложения — отправка в 09:00 МСК")
        else:
            print(f"    {WARN} Нет предложений для дайджеста")


# ════════════════════════════════════════════════════════════════
# 5. Проверка Redis (если задан)
# ════════════════════════════════════════════════════════════════

async def check_redis():
    print(f"\n{SEP}")
    print("5. ПРОВЕРКА REDIS")
    print(SEP)

    if not REDIS_URL:
        print(f"  {WARN} REDIS_URL не задан — пропускаем")
        print(f"  {INFO} Без Redis подписки не сохраняются и горячие предложения не работают!")
        return False

    try:
        from redis import asyncio as aioredis
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await client.ping()
        print(f"  {OK} Подключение к Redis: OK")

        # Считаем подписки
        prefix = "flight_bot:"
        all_keys = await client.smembers(f"{prefix}hotsubs_all")
        print(f"  {INFO} Всего ключей подписок в hotsubs_all: {len(all_keys)}")

        hot_count = digest_count = dead_count = 0
        for key in all_keys:
            raw = await client.get(key)
            if not raw:
                dead_count += 1
                continue
            sub = json.loads(raw)
            if sub.get("sub_type") == "hot":
                hot_count += 1
            elif sub.get("sub_type") == "digest":
                digest_count += 1

            # Показываем последнее уведомление
            last = sub.get("last_notified", 0)
            if last:
                ago = int(time.time() - last)
                parts = key.split(":")
                uid = parts[-2] if len(parts) >= 2 else "?"
                print(f"    • user={uid} тип={sub.get('sub_type')} из={sub.get('origin_iata')} "
                      f"кат={sub.get('category')} — последнее уведомление {ago//3600}ч назад")

        print(f"\n  {OK} Горячих подписок: {hot_count}")
        print(f"  {OK} Дайджест-подписок: {digest_count}")
        if dead_count:
            print(f"  {WARN} Мёртвых ключей (истекли): {dead_count}")

        # Проверяем baseline цены
        baseline_keys = await client.keys(f"{prefix}baseline:*")
        print(f"  {INFO} Baseline цен в Redis: {len(baseline_keys)}")

        await client.aclose()
        return True

    except Exception as e:
        print(f"  {FAIL} Redis недоступен: {e}")
        return False


# ════════════════════════════════════════════════════════════════
# 6. Реальная отправка тестового уведомления
# ════════════════════════════════════════════════════════════════

async def send_test_notification():
    print(f"\n{SEP}")
    print("6. РЕАЛЬНАЯ ОТПРАВКА ТЕСТОВОГО УВЕДОМЛЕНИЯ")
    print(SEP)

    if not BOT_TOKEN:
        print(f"  {FAIL} BOT_TOKEN не задан")
        return
    if not TEST_USER_ID:
        print(f"  {FAIL} TELEGRAM_TEST_USER_ID не задан")
        return
    if not AVIASALES_TOKEN:
        print(f"  {FAIL} AVIASALES_TOKEN не задан")
        return

    # Ищем реальный рейс
    depart = (date.today() + timedelta(days=30)).strftime("%Y-%m")
    print(f"  Ищем рейс MOW → AYT на {depart}...")
    flight = await test_flight_search("MOW", "AYT", depart)

    if not flight or "_error" in flight:
        print(f"  {WARN} Рейс не найден, отправляем тест без цены")
        price_str = "нет данных"
        link = "https://www.aviasales.ru"
    else:
        price = flight.get("price") or flight.get("value", 0)
        price_str = f"{price:,} ₽".replace(",", "\u202f")
        dep_date = flight.get("departure_at", "")[:10]
        link = f"https://www.aviasales.ru/search/MOW{dep_date.replace('-','')}AYT1"
        print(f"  {OK} Найден рейс: {price_str}, {flight.get('airline','')} {flight.get('flight_number','')}")

    text = (
        f"🧪 <b>ТЕСТ горячих предложений</b>\n\n"
        f"🔥 Горячее предложение!\n\n"
        f"📍 🏖️ Морские курорты\n"
        f"✈️ <b>Москва → Анталья</b>\n"
        f"📅 Примерно: {depart}\n"
        f"💰 <b>{price_str}</b> / чел.\n\n"
        f"⏰ <i>Это тестовое сообщение — проверка отправки уведомлений.</i>"
    )

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": TEST_USER_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps({"inline_keyboard": [
                        [{"text": f"✈️ Проверить на Aviasales", "url": link}]
                    ]})
                }
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    print(f"  {OK} Сообщение отправлено на user_id={TEST_USER_ID}")
                    print(f"  {OK} Проверьте Telegram — уведомление должно прийти!")
                else:
                    print(f"  {FAIL} Telegram API вернул ошибку: {data.get('description')}")
    except Exception as e:
        print(f"  {FAIL} Ошибка отправки: {e}")


# ════════════════════════════════════════════════════════════════
# 7. Итог и рекомендации
# ════════════════════════════════════════════════════════════════

def print_summary(env_ok: bool, api_ok: bool, redis_ok: bool):
    print(f"\n{SEP}")
    print("ИТОГ И РЕКОМЕНДАЦИИ")
    print(SEP)

    all_ok = env_ok and api_ok
    print(f"\n  Статус: {'ВСЁ РАБОТАЕТ' if all_ok else 'ЕСТЬ ПРОБЛЕМЫ'}")
    print()

    if not env_ok:
        print(f"  {FAIL} Задайте AVIASALES_TOKEN в .env — без него ничего не работает")

    if not api_ok:
        print(f"  {WARN} API не вернул рейсы. Возможные причины:")
        print(f"        • Нет данных в кэше на выбранные даты (попробуйте другой месяц)")
        print(f"        • Rate limit (429) — подождите несколько минут")
        print(f"        • Некорректный токен")

    if not redis_ok:
        print(f"  {WARN} Без Redis:")
        print(f"        • Подписки не сохраняются между перезапусками")
        print(f"        • Baseline цены не накапливаются (DROP_THRESHOLD не работает)")
        print(f"        • Кулдаун маршрутов не работает — возможны дубли уведомлений")

    print(f"\n  {INFO} Special Offers API (v2/prices/special-offers):")
    print(f"        • Возвращает XML с акционными предложениями авиакомпаний")
    print(f"        • Не требует токена — публичный endpoint")
    print(f"        • Используется для маркетинговых акций, не для мониторинга цен")
    print(f"        • Рекомендация: добавить как отдельный раздел 'Акции' в боте")

    print(f"\n  {INFO} Как работают горячие предложения:")
    print(f"        • Каждые 3 часа бот проверяет ВСЕ подписки типа 'hot'")
    print(f"        • Для каждой подписки — перебирает все направления категории")
    print(f"        • Если цена упала на ≥10% от baseline ИЛИ ниже бюджета → отправляет")
    print(f"        • Кулдаун 24ч на маршрут, 12ч на подписку — защита от спама")

    print(f"\n  {INFO} Как работает дайджест:")
    print(f"        • Ежедневно в 09:00 МСК — для frequency='daily'")
    print(f"        • По понедельникам в 09:00 МСК — для frequency='weekly'")
    print(f"        • Топ-3 дешёвых направления из категории → одно сообщение")

    print()


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  ТЕСТ: ГОРЯЧИЕ ПРЕДЛОЖЕНИЯ И ДАЙДЖЕСТ")
    print(f"  Дата: {date.today()}")
    print("=" * 60)

    env_ok   = check_env()
    api_ok   = await check_api() if env_ok else False
    redis_ok = await check_redis()

    if CHECK_SPECIAL or True:  # всегда проверяем Special Offers
        await check_special_offers()

    if env_ok:
        await check_hot_logic()

    if SEND_REAL:
        await send_test_notification()

    print_summary(env_ok, api_ok, redis_ok)


if __name__ == "__main__":
    asyncio.run(main())