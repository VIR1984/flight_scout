#!/usr/bin/env python3
"""
test_subscriptions.py — тест подписочной системы FlightBot Scout.

Запуск локально (тест-бот):
    cd "ТЕСТ WOW Bilet" && python test_subscriptions.py

Запуск на сервере:
    cd /app && python test_subscriptions.py

Что проверяет:
  Блок 1: Redis — соединение, prefix (dev/prod)
  Блок 2: Подписки — структура, кулдауны, кеш пустых маршрутов, даты
  Блок 3: Aviasales API — токен, grouped_prices endpoint
  Блок 4: Реальный поиск по маршрутам из каждой подписки
  Блок 5: Слежение за ценой (price watches)
  Итог:   таблица статусов + диагноз почему не шлются уведомления
"""

import asyncio, os, sys, time, json, calendar
from datetime import date, timedelta, datetime

# ── Цвета ─────────────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[96m"
W = "\033[1m";  Z = "\033[0m"

_stats = {"ok": 0, "w": 0, "e": 0}
def _cnt(k): _stats[k] += 1
def ok(m):   print(f"{G}  ✅ {m}{Z}");  _cnt("ok")
def warn(m): print(f"{Y}  ⚠️  {m}{Z}"); _cnt("w")
def err(m):  print(f"{R}  ❌ {m}{Z}");  _cnt("e")
def info(m): print(f"{B}  ℹ️  {m}{Z}")
def hdr(m):  print(f"\n{W}{'═'*48}\n  {m}\n{'═'*48}{Z}")

# ── Фикс Windows asyncio ───────────────────────────────────────────────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Загрузка .env ──────────────────────────────────────────────────────────────
def load_env():
    _here = os.path.dirname(os.path.abspath(__file__))
    for path in [
        ".env",
        "/app/.env",
        os.path.join(_here, ".env"),
        os.path.join(_here, "..", ".env"),
    ]:
        if os.path.exists(path):
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            info(f".env загружен: {path}")
            return
    info(".env не найден — используем переменные окружения")

# ── Prefix (синхронизировано с utils/redis_client.py) ─────────────────────────
# RedisClient.__init__: env = os.getenv("BOT_ENV", "prod")
#                       self.prefix = f"flight_bot:{env}:"
def get_prefix() -> str:
    env = os.getenv("BOT_ENV", "prod")
    return f"flight_bot:{env}:"

# ── КАТЕГОРИИ (синхронизировано с handlers/hot_deals.py) ──────────────────────
CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",    ["AYT","HRG","SSH","RHO","DLM","LCA","TFS","PMI","CFU","HER","PFO","AER","SIP","BUS"]),
    "world":  ("🌍 Путешествия по миру", ["DXB","BKK","SIN","KUL","HKT","CMB","NBO","GRU","JFK","LAX","YYZ","ICN","TYO","PEK","DEL"]),
    "russia": ("🇷🇺 По России",          ["AER","LED","KZN","OVB","SVX","ROV","UFA","CEK","KRR","VOG","MCX","GRV","KUF","IKT","VVO"]),
    "custom": ("🔍 Свой маршрут",        []),
}

# ── Константы (синхронизировано с services/hot_deals_sender.py) ───────────────
DROP_THRESHOLD = 0.10       # уведомлять только при снижении >= 10% от базовой
ROUTE_COOLDOWN = 86400      # кулдаун на маршрут: 24 часа
SUB_COOLDOWN   = 12 * 3600  # общий таймер подписки: 12 часов
MIN_DAYS_AHEAD = 14         # grouped_prices стабильно работает от 14 дней

# ── API URL (синхронизировано с services/flight_search.py) ────────────────────
# Старый файл использовал prices_for_dates — НЕВЕРНО.
# Текущий код использует grouped_prices.
GROUPED_URL = "https://api.travelpayouts.com/aviasales/v3/grouped_prices"

# ── Дата поиска (копия логики services/hot_deals_sender.py) ───────────────────
def resolve_search_date(sub: dict) -> date:
    today  = date.today()
    min_d  = today + timedelta(days=MIN_DAYS_AHEAD)
    for mk in sub.get("travel_months", []):
        try:
            m, y = map(int, mk.split("_"))
            c    = date(y, m, 15)
            if c >= min_d:
                return c
            last = calendar.monthrange(y, m)[1]
            end  = date(y, m, last)
            if end >= min_d:
                return end
        except Exception:
            pass
    # fallback: старые поля travel_month / travel_year
    tm, ty = sub.get("travel_month"), sub.get("travel_year")
    if tm and ty:
        try:
            c = date(int(ty), int(tm), 15)
            if c >= min_d:
                return c
        except Exception:
            pass
    return today + timedelta(days=30)


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 1 — Redis
# ═══════════════════════════════════════════════════════════════════════════════
async def make_redis():
    """Создаёт свежее Redis-соединение."""
    from redis.asyncio import from_url
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    r = from_url(url, decode_responses=False)
    await r.ping()
    return r


async def check_redis():
    hdr("БЛОК 1 — Redis")
    url = os.getenv("REDIS_URL", "")
    if not url:
        err("REDIS_URL не задан"); return None

    try:
        from redis.asyncio import from_url
        r = from_url(url, decode_responses=False)
        await r.ping()
        ok(f"Подключение OK  ({url.split('@')[-1]})")
    except Exception as e:
        err(f"Подключение: {e}"); return None

    prefix = get_prefix()
    env    = os.getenv("BOT_ENV", "prod")
    info(f"BOT_ENV={env}  →  prefix={prefix}")

    try:
        await r.set(f"{prefix}_healthcheck", b"1", ex=5)
        assert await r.get(f"{prefix}_healthcheck") == b"1"
        await r.delete(f"{prefix}_healthcheck")
        ok("Запись / чтение / удаление ключа OK")
    except Exception as e:
        err(f"Запись ключа: {e}")

    return r


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 2 — Загрузка и валидация подписок
# ═══════════════════════════════════════════════════════════════════════════════
async def check_subscriptions(r):
    hdr("БЛОК 2 — Подписки")
    prefix = get_prefix()

    # hotsubs_all — set со всеми полными ключами подписок
    all_keys = await r.smembers(f"{prefix}hotsubs_all")
    if not all_keys:
        warn("hotsubs_all пустой — нет ни одной подписки"); return []

    # Pipeline: один round-trip к Upstash вместо N отдельных GET
    all_keys_list = list(all_keys)
    try:
        pipe = r.pipeline(transaction=False)
        for key in all_keys_list:
            pipe.get(key)
        raw_values = await pipe.execute()
    except Exception as e:
        warn(f"Pipeline ошибка: {e} — fallback по одному")
        raw_values = []
        for key in all_keys_list:
            try:    raw_values.append(await r.get(key))
            except: raw_values.append(None)

    subs, dead = [], 0
    for key, raw in zip(all_keys_list, raw_values):
        if not raw:
            dead += 1; continue
        try:
            sub   = json.loads(raw)
            key_s = key.decode() if isinstance(key, bytes) else key
            # Формат ключа: flight_bot:{env}:hotsub:{user_id}:{sub_id}
            parts   = key_s.split(":")
            user_id = int(parts[-2])
            sub_id  = parts[-1]
            subs.append((user_id, sub_id, sub))
        except Exception:
            dead += 1

    hot    = [s for s in subs if s[2].get("sub_type") == "hot"]
    digest = [s for s in subs if s[2].get("sub_type") == "digest"]
    ok(f"Подписок загружено: {len(subs)}  (🔥 hot: {len(hot)}  📰 digest: {len(digest)})")
    if dead:
        warn(f"«Мёртвых» ключей в hotsubs_all (нет данных): {dead}")

    now = time.time()

    # Собираем все ключи кулдаунов и route_empty одним batch-запросом
    cd_keys_map    = {}   # sub_id → [ключи кулдаунов маршрутов]
    empty_keys_map = {}   # sub_id → [ключи route_empty]

    for user_id, sub_id, sub in subs:
        cat = sub.get("category", "?")
        pool = (sub.get("dest_iata_list", [])[:15] if cat == "custom"
                else CATEGORIES.get(cat, ("", []))[1])

        orgs = sub.get("origins", [])
        origin_iatas = (
            [o.get("iata", "") for o in orgs if o.get("iata")] if orgs
            else ([sub["origin_iata"]] if sub.get("origin_iata") else [])
        )

        # route_cd — per-sub, per-dest
        cd_keys_map[sub_id] = [
            f"{prefix}route_cd:{sub_id}:{d}" for d in pool
        ]
        # route_empty — глобальный (не per-sub), per-origin-dest
        empty_keys_map[sub_id] = [
            f"{prefix}route_empty:{o}:{d}"
            for o in origin_iatas for d in pool if d != o
        ][:20]

    all_batch_keys = (
        [k for v in cd_keys_map.values() for k in v] +
        [k for v in empty_keys_map.values() for k in v]
    )
    cd_vals    = {}
    empty_vals = {}

    if all_batch_keys:
        try:
            pipe2 = r.pipeline(transaction=False)
            for k in all_batch_keys:
                pipe2.exists(k)
            batch_results = await pipe2.execute()
            cd_all_keys   = [k for v in cd_keys_map.values() for k in v]
            empty_all_keys= [k for v in empty_keys_map.values() for k in v]
            n_cd          = len(cd_all_keys)
            for k, v in zip(cd_all_keys, batch_results[:n_cd]):
                cd_vals[k] = v
            for k, v in zip(empty_all_keys, batch_results[n_cd:]):
                empty_vals[k] = v
        except Exception as e:
            warn(f"Pipeline кулдауны/empty: {e}")

    for user_id, sub_id, sub in subs:
        t = "🔥 hot" if sub.get("sub_type") == "hot" else "📰 digest"
        print(f"\n  {t}  sub={sub_id}  user={user_id}")

        # Города вылета
        orgs = sub.get("origins", [])
        if orgs:
            iatas = [o.get("iata", "?") for o in orgs]
            info(f"Вылет: {', '.join(iatas)}")
        elif sub.get("origin_iata"):
            info(f"Вылет: {sub['origin_iata']}")
        else:
            err(f"sub={sub_id}: НЕТ города вылета — уведомления невозможны!"); continue

        # Категория и пул назначений
        cat = sub.get("category", "?")
        if cat == "custom":
            pool = sub.get("dest_iata_list", [])
            info(f"Категория: custom  ({len(pool)} назначений: "
                 f"{', '.join(pool[:5])}{'…' if len(pool) > 5 else ''})")
        else:
            _, pool = CATEGORIES.get(cat, ("?", []))
            info(f"Категория: {cat}  ({len(pool)} назначений в справочнике)")
        if not pool:
            err(f"sub={sub_id}: пустой пул назначений — уведомления невозможны!")

        # Бюджет
        mp  = sub.get("max_price", 0)
        pax = sub.get("passengers", 1)
        nb  = "\u202f"
        budget_str = "без ограничений" if not mp else f"{mp:,} ₽/чел.".replace(",", nb)
        info(f"Бюджет: {budget_str}  · {pax} пас.")

        # Дата поиска
        sd    = resolve_search_date(sub)
        ahead = (sd - date.today()).days
        if ahead < MIN_DAYS_AHEAD:
            err(f"Дата поиска {sd} = +{ahead} дн — grouped_prices не вернёт данные (нужно ≥{MIN_DAYS_AHEAD})")
        else:
            ok(f"Дата поиска: {sd}  (+{ahead} дней)")

        # Кулдаун подписки (last_notified → SUB_COOLDOWN = 12 ч)
        last = sub.get("last_notified", 0)
        if last:
            ago  = int(now - last)
            left = SUB_COOLDOWN - ago
            ago_s = f"{ago // 3600}ч {(ago % 3600) // 60}м назад"
            if left > 0:
                warn(f"SUB_COOLDOWN: ещё {left // 3600}ч {(left % 3600) // 60}м  (отправлялась {ago_s})")
            else:
                ok(f"SUB_COOLDOWN: прошёл  (отправлялась {ago_s})")
        else:
            ok("SUB_COOLDOWN: никогда не отправлялась — готова")

        # Кулдауны маршрутов (route_cd, 24 ч)
        cd_keys = cd_keys_map.get(sub_id, [])
        if cd_keys:
            on_cd = sum(1 for k in cd_keys if cd_vals.get(k))
            total = len(cd_keys)
            if on_cd == total:
                err(f"ВСЕ {total} маршрутов на кулдауне 24ч — уведомление не уйдёт!")
            elif on_cd > 0:
                warn(f"Маршруты на кулдауне route_cd: {on_cd}/{total}")
            else:
                ok(f"Кулдаун маршрутов route_cd: 0/{total} — все свободны")

        # Кеш пустых маршрутов route_empty (48 ч, из empty_route_cache)
        e_keys = empty_keys_map.get(sub_id, [])
        if e_keys:
            cached_empty = sum(1 for k in e_keys if empty_vals.get(k))
            if cached_empty == len(e_keys):
                warn(f"Все {len(e_keys)} маршрутов в кеше «нет данных» (48ч) — API-запросов не будет")
            elif cached_empty > 0:
                info(f"Маршрутов в кеше route_empty: {cached_empty}/{len(e_keys)} (будут пропущены)")
            else:
                ok(f"Кеш route_empty: 0/{len(e_keys)} — все маршруты будут проверены API")

    return subs


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 3 — Aviasales API
# ═══════════════════════════════════════════════════════════════════════════════
async def check_api():
    hdr("БЛОК 3 — Aviasales API (grouped_prices)")
    token = os.getenv("AVIASALES_TOKEN", "").strip()
    if not token:
        err("AVIASALES_TOKEN не задан"); return False
    ok(f"AVIASALES_TOKEN задан (длина {len(token)})")

    import aiohttp
    test_date = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
    params = {
        "origin": "MOW", "destination": "BKK",
        "departure_at": test_date,
        "currency": "rub", "token": token,
        "group_by": "departure_at",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(GROUPED_URL, params=params,
                             timeout=aiohttp.ClientTimeout(total=12)) as resp:
                if resp.status == 401:
                    err("API: 401 — токен неверный"); return False
                if resp.status == 429:
                    warn("API: 429 Rate limit — токен рабочий"); return True
                if resp.status != 200:
                    err(f"API: HTTP {resp.status}"); return False
                data = await resp.json()
                if not data.get("success"):
                    err(f"API: success=false, error={data.get('error')}"); return False
                cnt = len(data.get("data", {}))
                if cnt:
                    ok(f"API OK — MOW→BKK {test_date}: {cnt} вариантов")
                else:
                    warn(f"API ответил OK но рейсов нет (MOW→BKK {test_date})")
                return True
    except Exception as e:
        err(f"API: {e}"); return False


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 4 — Реальный поиск по маршрутам подписок
# ═══════════════════════════════════════════════════════════════════════════════
async def check_routes(r, subs: list):
    hdr("БЛОК 4 — Поиск рейсов по подпискам")
    if not subs:
        warn("Нет подписок"); return {}

    token = os.getenv("AVIASALES_TOKEN", "").strip()
    if not token:
        warn("AVIASALES_TOKEN не задан — пропускаем"); return {}

    import aiohttp
    results = {}
    prefix  = get_prefix()
    rc      = [r]   # список для переприсвоения при реконнекте

    for user_id, sub_id, sub in subs:
        t = "🔥" if sub.get("sub_type") == "hot" else "📰"
        print(f"\n  {t} sub={sub_id}  user={user_id}")

        # Города вылета
        orgs = sub.get("origins", [])
        origin_iatas = (
            [o["iata"] for o in orgs if o.get("iata")] if orgs
            else ([sub["origin_iata"]] if sub.get("origin_iata") else [])
        )
        if not origin_iatas:
            err(f"sub={sub_id}: нет вылета")
            results[sub_id] = {"status": "no_origin"}; continue

        cat  = sub.get("category", "world")
        pool = (sub.get("dest_iata_list", []) if cat == "custom"
                else CATEGORIES.get(cat, ("", ["BKK"]))[1])
        if not pool:
            err(f"sub={sub_id}: пустой пул назначений")
            results[sub_id] = {"status": "no_pool"}; continue

        sd         = resolve_search_date(sub).strftime("%Y-%m-%d")
        max_price  = sub.get("max_price", 0)
        candidates = []
        stats      = {"no_data": 0, "over_budget": 0, "api_err": 0,
                      "ok": 0, "cached_empty": 0}

        async with aiohttp.ClientSession() as session:
            for origin in origin_iatas:
                dests = [d for d in pool if d != origin][:10]
                for dest in dests:

                    # Кеш пустых маршрутов (route_empty, 48 ч)
                    try:
                        is_cached_empty = await rc[0].exists(
                            f"{prefix}route_empty:{origin}:{dest}"
                        ) > 0
                    except Exception:
                        is_cached_empty = False
                    if is_cached_empty:
                        stats["cached_empty"] += 1
                        print(f"      ⏭  {origin}→{dest}: пропуск (route_empty 48ч)")
                        continue

                    params = {
                        "origin": origin, "destination": dest,
                        "departure_at": sd,
                        "currency": "rub", "token": token,
                        "group_by": "departure_at",
                    }
                    try:
                        async with session.get(
                            GROUPED_URL, params=params,
                            timeout=aiohttp.ClientTimeout(total=12)
                        ) as resp:
                            if resp.status == 429:
                                warn(f"    {origin}→{dest}: 429 Rate limit")
                                await asyncio.sleep(3); continue
                            if resp.status != 200:
                                stats["api_err"] += 1; continue
                            data = await resp.json()
                            if not data.get("success"):
                                stats["api_err"] += 1; continue

                            # grouped_prices возвращает dict {дата: {...}}
                            raw_data = data.get("data", {})
                            if isinstance(raw_data, dict):
                                flights = list(raw_data.values())
                            elif isinstance(raw_data, list):
                                flights = raw_data
                            else:
                                flights = []

                            if not flights:
                                stats["no_data"] += 1
                                print(f"      {origin}→{dest}: нет данных")
                                continue

                            price = min(
                                f.get("price") or f.get("value") or 999_999
                                for f in flights
                            )

                            # Бюджет
                            if max_price and price > max_price:
                                stats["over_budget"] += 1
                                print(f"      {origin}→{dest}: {price:,} ₽  > бюджет {max_price:,} ₽".replace(",", "\u202f"))
                                continue

                            # Baseline EMA (utils/redis_client.py → get_baseline_price)
                            # Хранится как JSON: {"avg": 15941.76}
                            baseline = None
                            try:
                                raw_bl = await rc[0].get(
                                    f"{prefix}baseline:{origin}:{dest}"
                                )
                                if raw_bl:
                                    bl_data  = json.loads(raw_bl)
                                    baseline = float(bl_data["avg"]) if isinstance(bl_data, dict) else float(bl_data)
                            except Exception:
                                pass

                            drop_ok = True
                            if baseline:
                                drop = (baseline - price) / baseline
                                if drop < DROP_THRESHOLD:
                                    drop_ok = False
                                    print(f"      {origin}→{dest}: {price:,} ₽  drop={drop:.1%} < {DROP_THRESHOLD:.0%}".replace(",", "\u202f"))

                            # Кулдаун маршрута (route_cd, 24 ч, per-sub)
                            on_cd = False
                            try:
                                on_cd = await rc[0].exists(
                                    f"{prefix}route_cd:{sub_id}:{dest}"
                                ) > 0
                            except Exception:
                                pass

                            stats["ok"] += 1
                            flag = ""
                            if on_cd:      flag += " [КУЛДАУН 24ч]"
                            if not drop_ok: flag += " [DROP<10%]"
                            marker = "⚠️" if flag else "✓"
                            bl_str = f"  базовая={baseline:.0f}₽" if baseline else ""
                            print(f"      {marker} {origin}→{dest}: {price:,} ₽{bl_str}{flag}".replace(",", "\u202f"))
                            candidates.append({
                                "price": price, "origin": origin, "dest": dest,
                                "on_cooldown": on_cd, "drop_ok": drop_ok,
                            })

                    except asyncio.TimeoutError:
                        stats["api_err"] += 1
                        print(f"      {origin}→{dest}: timeout")
                    except Exception as e:
                        es = str(e)
                        # Upstash разрывает TCP при долгих паузах — переподключаемся
                        if any(x in es for x in ("10054", "ConnectionReset", "ConnectionError")):
                            print(f"      {origin}→{dest}: Redis разрыв — переподключение…")
                            try:    await rc[0].aclose()
                            except: pass
                            try:
                                rc[0] = await make_redis()
                            except Exception as re:
                                err(f"Переподключение Redis не удалось: {re}")
                                return results
                        else:
                            stats["api_err"] += 1
                            print(f"      {origin}→{dest}: {e}")
                    await asyncio.sleep(0.25)

        sendable = [c for c in candidates if not c["on_cooldown"] and c["drop_ok"]]
        print()
        if sendable:
            best = min(sendable, key=lambda c: c["price"])
            ok(f"sub={sub_id}: ГОТОВО к отправке  "
               f"{best['origin']}→{best['dest']} {best['price']:,} ₽  "
               f"(кандидатов: {len(sendable)})".replace(",", "\u202f"))
            results[sub_id] = {
                "status": "ready",
                "best":   f"{best['origin']}→{best['dest']} {best['price']}₽",
                "count":  len(sendable),
            }
        elif candidates:
            blocked   = [c for c in candidates if c["on_cooldown"]]
            low_drop  = [c for c in candidates if not c["drop_ok"]]
            reason    = []
            if blocked:   reason.append(f"{len(blocked)} на кулдауне 24ч")
            if low_drop:  reason.append(f"{len(low_drop)} снижение <10%")
            warn(f"sub={sub_id}: рейсы НАЙДЕНЫ ({len(candidates)}) но заблокированы — {', '.join(reason)}")
            results[sub_id] = {"status": "blocked", "reason": ", ".join(reason), "count": len(candidates)}
        else:
            parts = []
            if stats["no_data"]:      parts.append(f"{stats['no_data']} маршрутов — нет данных API")
            if stats["over_budget"]:  parts.append(f"{stats['over_budget']} — выше бюджета")
            if stats["api_err"]:      parts.append(f"{stats['api_err']} — ошибки API")
            if stats["cached_empty"]: parts.append(f"{stats['cached_empty']} — пропущено (route_empty 48ч)")
            reason = "; ".join(parts) or "неизвестно"
            warn(f"sub={sub_id}: нет кандидатов — {reason}")
            results[sub_id] = {"status": "no_candidates", "reason": reason}

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 5 — Price watches
# ═══════════════════════════════════════════════════════════════════════════════
async def check_watches(r):
    hdr("БЛОК 5 — Слежение за ценой (price watches)")
    prefix = get_prefix()

    # get_all_watch_keys использует SCAN по pattern watch:*
    # (utils/redis_client.py → get_all_watch_keys)
    pattern = f"{prefix}watch:*"
    cursor, keys = 0, []
    while True:
        cursor, batch = await r.scan(cursor=cursor, match=pattern, count=100)
        keys.extend(batch)
        if cursor == 0:
            break

    if not keys:
        info("Нет активных отслеживаний"); return
    ok(f"Активных отслеживаний: {len(keys)}")

    token = os.getenv("AVIASALES_TOKEN", "").strip()

    # Pipeline: загружаем первые 5
    watch_keys = keys[:5]
    try:
        pipe = r.pipeline(transaction=False)
        for key in watch_keys:
            pipe.get(key)
        raw_values = await pipe.execute()
    except Exception as e:
        err(f"Pipeline watches: {e}"); return

    import aiohttp
    for key, raw in zip(watch_keys, raw_values):
        if not raw: continue
        try:
            w    = json.loads(raw)
            orig = w.get("origin", "?")
            dest = w.get("dest", "?")
            cur  = w.get("current_price", 0)
            uid  = w.get("user_id", "?")
            dd   = w.get("depart_date", "")
            nb   = "\u202f"
            print(f"\n  🔎 {orig}→{dest}  текущая={cur:,} ₽  user={uid}".replace(",", nb))
            if dd: info(f"Дата вылета: {dd}")

            if token and orig not in ("?", "") and dest not in ("?", ""):
                search_date = dd or (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
                params = {
                    "origin": orig, "destination": dest,
                    "departure_at": search_date,
                    "currency": "rub", "token": token,
                    "group_by": "departure_at",
                }
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(GROUPED_URL, params=params,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data    = await resp.json()
                                flights = list(data.get("data", {}).values())
                                if flights:
                                    new_price = min(
                                        f.get("price") or f.get("value") or 0
                                        for f in flights
                                    )
                                    change = ((new_price - cur) / cur * 100) if cur else 0
                                    sign   = "📈" if change > 0 else ("📉" if change < 0 else "➡️")
                                    print(f"     {sign} Актуально: {new_price:,} ₽  ({change:+.1f}%)".replace(",", nb))
                                else:
                                    print(f"     Нет данных для {orig}→{dest} {search_date}")
                except Exception as e:
                    print(f"     API: {e}")
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# ИТОГОВЫЙ ОТЧЁТ
# ═══════════════════════════════════════════════════════════════════════════════
def summary(results: dict):
    hdr("ИТОГОВЫЙ ОТЧЁТ")
    print(f"\n  {G}✅ OK:{Z}           {_stats['ok']}")
    print(f"  {Y}⚠️  Предупрежд.:{Z}  {_stats['w']}")
    print(f"  {R}❌ Ошибок:{Z}        {_stats['e']}")

    if results:
        print(f"\n  {'sub_id':<22} {'статус':<16} {'детали'}")
        print(f"  {'─'*22} {'─'*16} {'─'*34}")
        for sub_id, res in results.items():
            s      = res.get("status", "?")
            col    = G if s == "ready" else (Y if s in ("blocked", "no_candidates") else R)
            detail = res.get("best", res.get("reason", ""))
            print(f"  {sub_id:<22} {col}{s:<16}{Z} {detail}")

    print()
    diag = []
    if any(r.get("status") == "blocked" for r in results.values()):
        diag.append(f"{Y}→ Уведомления блокируются кулдауном — подожди 24ч или сбрось route_cd:* ключи в Redis{Z}")
    if any(r.get("status") == "no_candidates" for r in results.values()):
        diag.append(f"{Y}→ Нет данных по маршрутам — проверь токен, категорию, дату и кеш route_empty:*{Z}")
    if any(r.get("status") == "ready" for r in results.values()):
        diag.append(f"{G}→ Есть готовые к отправке — уведомления придут в следующем цикле hot_deals_sender (каждые 3ч){Z}")
    for d in diag:
        print(f"  {d}")
    print()

    if _stats["e"] == 0 and _stats["w"] == 0:
        ok("Всё в порядке!")
    elif _stats["e"] == 0:
        warn("Есть предупреждения — см. детали выше")
    else:
        err("Есть критические ошибки")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
async def main():
    print(f"\n{W}{'═'*48}")
    print(f"  FlightBot Scout — Тест подписок")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*48}{Z}")

    load_env()

    r = await check_redis()
    if not r:
        err("Redis недоступен — дальше невозможно")
        summary({}); return

    subs    = await check_subscriptions(r)
    api_ok  = await check_api()
    results = {}

    if api_ok and subs:
        results = await check_routes(r, subs) or {}
    elif not api_ok:
        warn("API недоступен — пропускаем тест маршрутов")
    elif not subs:
        warn("Нет подписок для теста")

    # Блок 4 долгий — Upstash закрывает TCP после ~2-3 мин простоя.
    # Пересоздаём соединение перед блоком 5.
    try:    await r.aclose()
    except: pass
    r = await make_redis()
    if not r:
        err("Redis недоступен для блока 5"); summary(results); return

    await check_watches(r)
    summary(results)

    try:    await r.aclose()
    except: pass


if __name__ == "__main__":
    asyncio.run(main())