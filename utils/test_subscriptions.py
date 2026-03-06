#!/usr/bin/env python3
"""
test_subscriptions.py — тест подписочной системы FlightBot Scout.

Запуск прямо на сервере:
    cd /app && python test_subscriptions.py

Что проверяет:
  Блок 1: Redis — соединение, методы подписок
  Блок 2: Подписки — структура, кулдауны, даты
  Блок 3: Aviasales API — токен, доступность
  Блок 4: Реальный поиск по маршрутам из каждой подписки
  Блок 5: Слежение за ценой (price watches)
  Итог:   таблица статусов + диагноз почему не шлются уведомления
"""

import asyncio, os, sys, time, json, calendar
from datetime import date, timedelta, datetime
from typing import Optional

# ── Цвета ────────────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[96m"; W = "\033[1m"; Z = "\033[0m"
def ok(m):   print(f"{G}  ✅ {m}{Z}");   _cnt("ok")
def warn(m): print(f"{Y}  ⚠️  {m}{Z}");  _cnt("w")
def err(m):  print(f"{R}  ❌ {m}{Z}");   _cnt("e")
def info(m): print(f"{B}  ℹ️  {m}{Z}")
def hdr(m):  print(f"\n{W}{'═'*44}\n  {m}\n{'═'*44}{Z}")

_stats = {"ok": 0, "w": 0, "e": 0}
def _cnt(k): _stats[k] += 1

# ── Фикс Windows asyncio: SelectorEventLoop вместо ProactorEventLoop ──────────
# ProactorEventLoop (дефолт Windows) обрывает TCP-соединения к Upstash
# при долгих паузах между командами (WinError 121 / TimeoutError).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# ── Загрузка .env ─────────────────────────────────────────────────────────────
def load_env():
    _here = os.path.dirname(os.path.abspath(__file__))
    for path in [
        ".env",
        "/app/.env",
        os.path.join(_here, ".env"),
        os.path.join(_here, "..", ".env"),
        os.path.join(_here, "..", "..", ".env"),
    ]:
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            info(f".env загружен: {path}")
            return
    info(".env не найден — используем переменные окружения")

# ── Дата поиска (копия логики hot_deals_sender.py) ────────────────────────────
def resolve_search_date(sub: dict) -> date:
    MIN_AHEAD = 14
    today = date.today()
    min_d = today + timedelta(days=MIN_AHEAD)
    for mk in sub.get("travel_months", []):
        try:
            m, y = map(int, mk.split("_"))
            c = date(y, m, 15)
            if c >= min_d: return c
            last = calendar.monthrange(y, m)[1]
            end  = date(y, m, last)
            if end >= min_d: return end
        except Exception: pass
    tm, ty = sub.get("travel_month"), sub.get("travel_year")
    if tm and ty:
        try:
            c = date(ty, tm, 15)
            if c >= min_d: return c
        except Exception: pass
    return today + timedelta(days=30)

# ── КАТЕГОРИИ (синхронизировано с handlers/hot_deals.py) ─────────────────────
CATEGORIES = {
    "sea":    ("🏖 Морские курорты",     ["AYT","HRG","SSH","RHO","DLM","LCA","TFS","PMI","CFU","HER","PFO","AER","SIP","BUS"]),
    "world":  ("🌍 Путешествия по миру", ["DXB","BKK","SIN","KUL","HKT","CMB","NBO","GRU","JFK","LAX","YYZ","ICN","TYO","PEK","DEL"]),
    "russia": ("🇷🇺 По России",          ["AER","LED","KZN","OVB","SVX","ROV","UFA","CEK","KRR","VOG","MCX","GRV","KUF","IKT","VVO"]),
    "custom": ("🔍 Свой маршрут",        []),
}

SUB_COOLDOWN   = 12 * 3600   # 12 часов
ROUTE_COOLDOWN = 86400       # 24 часа
DROP_THRESHOLD = 0.10        # 10%
GROUPED_URL    = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 1: Redis
# ═══════════════════════════════════════════════════════════════════════════════
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
    try:
        await r.set("flightbot:_test", b"1", ex=5)
        assert await r.get("flightbot:_test") == b"1"
        await r.delete("flightbot:_test")
        ok("Запись/чтение/удаление ключа OK")
    except Exception as e:
        err(f"Запись ключа: {e}")
    return r

# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 2: Загрузка и валидация подписок
# ═══════════════════════════════════════════════════════════════════════════════
async def check_subscriptions(r):
    hdr("БЛОК 2 — Подписки")
    prefix = "flight_bot:"
    all_keys = await r.smembers(f"{prefix}hotsubs_all")
    if not all_keys:
        warn("hotsubs_all пустой — нет ни одной подписки"); return []

    # Загружаем все ключи одним pipeline вместо N отдельных r.get()
    # Один round-trip к Upstash = нет риска разрыва TCP между командами
    all_keys_list = list(all_keys)
    try:
        pipe = r.pipeline(transaction=False)
        for key in all_keys_list:
            pipe.get(key)
        raw_values = await pipe.execute()
    except Exception as e:
        err(f"Pipeline ошибка при загрузке подписок: {e}"); return []

    subs, dead = [], 0
    for key, raw in zip(all_keys_list, raw_values):
        if not raw: dead += 1; continue
        try:
            sub = json.loads(raw)
            key_s = key.decode() if isinstance(key, bytes) else key
            parts = key_s.split(":")
            user_id = int(parts[-2])
            sub_id  = parts[-1]
            subs.append((user_id, sub_id, sub))
        except Exception: dead += 1

    hot    = [s for s in subs if s[2].get("sub_type") == "hot"]
    digest = [s for s in subs if s[2].get("sub_type") == "digest"]
    ok(f"Подписок: {len(subs)}  (🔥 горячих: {len(hot)}, 📰 дайджест: {len(digest)})")
    if dead: warn(f"«Мёртвых» ключей в hotsubs_all: {dead}")

    now = time.time()

    # Собираем все ключи кулдаунов и грузим одним pipeline
    cd_keys_map = {}
    for user_id, sub_id, sub in subs:
        cat = sub.get("category", "?")
        if cat == "custom":
            cd_pool = sub.get("dest_iata_list", [])[:10]
        else:
            _, cd_pool = CATEGORIES.get(cat, ("", []))
        cd_keys_map[sub_id] = [f"{prefix}route_cd:{sub_id}:{d}" for d in cd_pool]

    all_cd_keys = [k for keys in cd_keys_map.values() for k in keys]
    cd_values = {}
    if all_cd_keys:
        try:
            pipe2 = r.pipeline(transaction=False)
            for k in all_cd_keys:
                pipe2.get(k)
            cd_vals = await pipe2.execute()
            cd_values = dict(zip(all_cd_keys, cd_vals))
        except Exception as e:
            warn(f"Pipeline кулдауны: {e}")

    for user_id, sub_id, sub in subs:
        t = "🔥 hot" if sub.get("sub_type") == "hot" else "📰 digest"
        print(f"\n  {t}  sub={sub_id}  user={user_id}")

        # Города вылета
        orgs = sub.get("origins", [])
        if orgs:
            iatas = [o.get("iata","?") for o in orgs]
            info(f"Вылет: {', '.join(iatas)}")
        elif sub.get("origin_iata"):
            info(f"Вылет: {sub['origin_iata']}")
        else:
            err(f"sub={sub_id}: НЕТ города вылета!"); continue

        # Категория и пул назначений
        cat = sub.get("category","?")
        if cat == "custom":
            pool = sub.get("dest_iata_list",[])
            info(f"Категория: свой вариант  ({len(pool)} назначений: {', '.join(pool[:5])}{'…' if len(pool)>5 else ''})")
        else:
            _, pool = CATEGORIES.get(cat, ("",[]));
            info(f"Категория: {cat}  ({len(pool)} назначений в справочнике)")
        if not pool:
            err(f"sub={sub_id}: пустой пул назначений — уведомления невозможны!")

        # Бюджет
        mp = sub.get("max_price",0); pax = sub.get("passengers",1)
        _nb = '\u202f'
        budget_str = "без ограничений" if not mp else f"{mp:,} ₽/чел.".replace(",", _nb)
        info(f"Бюджет: {budget_str}  · {pax} пас.")

        # Дата поиска
        sd = resolve_search_date(sub); ahead = (sd - date.today()).days
        if ahead < 14:
            err(f"sub={sub_id}: дата поиска {sd} = +{ahead} дней — grouped_prices не вернёт данные (<14 дней)")
        else:
            ok(f"Дата поиска: {sd}  (+{ahead} дней)")

        # Кулдаун подписки
        last = sub.get("last_notified", 0)
        if last:
            ago  = int(now - last)
            left = SUB_COOLDOWN - ago
            ago_s = f"{ago//3600}ч {(ago%3600)//60}м назад"
            if left > 0:
                warn(f"SUB_COOLDOWN: ещё {left//3600}ч {(left%3600)//60}м  (последнее {ago_s})")
            else:
                ok(f"SUB_COOLDOWN: прошёл (последнее {ago_s})")
        else:
            ok("SUB_COOLDOWN: никогда не отправлялась — готова")

        # Кулдауны маршрутов (из уже загруженного batch)
        cd_keys = cd_keys_map.get(sub_id, [])
        if cd_keys:
            vals  = [cd_values.get(k) for k in cd_keys]
            on_cd = sum(1 for v in vals if v)
            total = len(cd_keys)
            if on_cd == total:
                err(f"ВСЕ {total} маршрутов на кулдауне 24ч — уведомление не уйдёт!")
            elif on_cd > 0:
                warn(f"Маршруты на кулдауне: {on_cd}/{total}")
            else:
                ok(f"Кулдаун маршрутов: 0/{total} — все свободны")

    return subs

# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 3: Aviasales API
# ═══════════════════════════════════════════════════════════════════════════════
async def check_api():
    hdr("БЛОК 3 — Aviasales API")
    token = os.getenv("AVIASALES_TOKEN","").strip()
    if not token:
        err("AVIASALES_TOKEN не задан"); return False
    ok(f"AVIASALES_TOKEN задан (длина {len(token)})")

    import aiohttp
    test_date = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
    params = {"origin":"MOW","destination":"BKK","departure_at":test_date,
              "currency":"rub","token":token,"group_by":"departure_at"}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(GROUPED_URL, params=params, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 401: err("API: 401 — токен неверный"); return False
                if r.status == 429: warn("API: 429 Rate limit — токен рабочий"); return True
                if r.status != 200: err(f"API: HTTP {r.status}"); return False
                data = await r.json()
                if not data.get("success"): err(f"API: success=false, error={data.get('error')}"); return False
                cnt = len(data.get("data",{}))
                if cnt:  ok(f"API: OK — MOW→BKK {test_date}: {cnt} вариантов")
                else:    warn(f"API: ответ OK но рейсов нет (MOW→BKK {test_date})")
                return True
    except Exception as e:
        err(f"API: {e}"); return False

# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 4: Реальный поиск по маршрутам подписок
# ═══════════════════════════════════════════════════════════════════════════════
async def check_routes(r, subs: list):
    hdr("БЛОК 4 — Поиск рейсов по подпискам")
    if not subs: warn("Нет подписок"); return {}

    token = os.getenv("AVIASALES_TOKEN","").strip()
    if not token: warn("AVIASALES_TOKEN не задан — пропускаем"); return {}

    import aiohttp
    results = {}
    prefix  = "flight_bot:"
    rc = [r]  # список чтобы можно было переназначить r внутри вложенных функций

    for user_id, sub_id, sub in subs:
        t = "🔥" if sub.get("sub_type") == "hot" else "📰"
        print(f"\n  {t} sub={sub_id}  user={user_id}")

        # Города вылета
        orgs = sub.get("origins",[])
        origin_iatas = [o["iata"] for o in orgs if o.get("iata")] if orgs else (
            [sub["origin_iata"]] if sub.get("origin_iata") else []
        )
        if not origin_iatas:
            err(f"sub={sub_id}: нет вылета"); results[sub_id]={"status":"no_origin"}; continue

        cat  = sub.get("category","world")
        pool = sub.get("dest_iata_list",[]) if cat=="custom" else CATEGORIES.get(cat,("",["BKK"]))[1]
        if not pool:
            err(f"sub={sub_id}: пустой пул назначений"); results[sub_id]={"status":"no_pool"}; continue

        sd        = resolve_search_date(sub).strftime("%Y-%m-%d")
        max_price = sub.get("max_price", 0)
        passengers= sub.get("passengers", 1)

        candidates = []
        stats = {"no_data":0, "over_budget":0, "api_err":0, "ok":0}

        async with aiohttp.ClientSession() as session:
            for origin in origin_iatas:
                dests_to_check = [d for d in pool if d != origin][:10]
                for dest in dests_to_check:
                    params = {"origin":origin,"destination":dest,"departure_at":sd,
                              "currency":"rub","token":token,"group_by":"departure_at"}
                    try:
                        async with session.get(GROUPED_URL, params=params,
                                               timeout=aiohttp.ClientTimeout(total=12)) as resp:
                            if resp.status == 429:
                                warn(f"    {origin}→{dest}: 429 Rate limit"); await asyncio.sleep(3); continue
                            if resp.status != 200:
                                stats["api_err"] += 1; continue
                            data = await resp.json()
                            if not data.get("success"):
                                stats["api_err"] += 1; continue
                            raw_data = data.get("data", {})
                            # API может вернуть либо dict (группировка по датам),
                            # либо list (прямой список рейсов) — обрабатываем оба
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
                            price = min(f.get("price") or f.get("value") or 999999 for f in flights)
                            # Бюджет с учётом пассажиров
                            if max_price and price > max_price:
                                stats["over_budget"] += 1
                                print(f"      {origin}→{dest}: {price:,} ₽  > бюджет {max_price:,} ₽".replace(",","\u202f"))
                                continue
                            # Baseline — хранится как JSON {"avg": 15941.76}
                            raw_bl = await rc[0].get(f"{prefix}baseline:{origin}:{dest}")
                            baseline = None
                            if raw_bl:
                                try:
                                    bl_data = json.loads(raw_bl)
                                    baseline = float(bl_data["avg"]) if isinstance(bl_data, dict) else float(bl_data)
                                except Exception:
                                    try: baseline = float(raw_bl)
                                    except Exception: pass
                            drop_ok = True
                            if baseline:
                                drop = (baseline - price) / baseline
                                if drop < DROP_THRESHOLD:
                                    drop_ok = False
                                    print(f"      {origin}→{dest}: {price:,} ₽  drop={drop:.1%} < {DROP_THRESHOLD:.0%} — ниже порога снижения".replace(",","\u202f"))
                            # Кулдаун маршрута
                            on_cd = await rc[0].exists(f"{prefix}route_cd:{sub_id}:{dest}") > 0
                            stats["ok"] += 1
                            flag = ""
                            if on_cd:   flag += " [КУЛДАУН 24ч]"
                            if not drop_ok: flag += " [DROP<10%]"
                            marker = "⚠️" if flag else "✓"
                            print(f"      {marker} {origin}→{dest}: {price:,} ₽{flag}".replace(",","\u202f"))
                            candidates.append({"price":price,"origin":origin,"dest":dest,
                                               "on_cooldown":on_cd,"drop_ok":drop_ok})
                    except asyncio.TimeoutError:
                        stats["api_err"] += 1; print(f"      {origin}→{dest}: timeout")
                    except Exception as e:
                        err_str = str(e)
                        # Upstash разрывает TCP при паузах — переподключаемся и продолжаем
                        if "10054" in err_str or "ConnectionReset" in err_str or "ConnectionError" in err_str:
                            print(f"      {origin}→{dest}: Redis разрыв — переподключение...")
                            try:
                                await rc[0].aclose()
                            except Exception:
                                pass
                            try:
                                rc[0] = await make_redis()
                            except Exception as re:
                                err(f"Переподключение Redis не удалось: {re}"); return results
                            stats["api_err"] += 1
                        else:
                            stats["api_err"] += 1; print(f"      {origin}→{dest}: {e}")
                    await asyncio.sleep(0.25)

        sendable = [c for c in candidates if not c["on_cooldown"] and c["drop_ok"]]
        print()
        if sendable:
            best = min(sendable, key=lambda c: c["price"])
            ok(f"sub={sub_id}: ГОТОВО к отправке  {best['origin']}→{best['dest']} {best['price']:,} ₽  (всего кандидатов: {len(sendable)})".replace(",","\u202f"))
            results[sub_id] = {"status":"ready","best":f"{best['origin']}→{best['dest']} {best['price']}₽","count":len(sendable)}
        elif candidates:
            blocked = [c for c in candidates if c["on_cooldown"]]
            low_drop= [c for c in candidates if not c["drop_ok"]]
            reason  = []
            if blocked:  reason.append(f"{len(blocked)} на кулдауне 24ч")
            if low_drop: reason.append(f"{len(low_drop)} снижение <10%")
            warn(f"sub={sub_id}: рейсы НАЙДЕНЫ ({len(candidates)}) но заблокированы — {', '.join(reason)}")
            results[sub_id] = {"status":"blocked","reason":", ".join(reason),"count":len(candidates)}
        else:
            parts = []
            if stats["no_data"]:     parts.append(f"{stats['no_data']} маршрутов — нет данных API")
            if stats["over_budget"]: parts.append(f"{stats['over_budget']} — выше бюджета")
            if stats["api_err"]:     parts.append(f"{stats['api_err']} — ошибки API")
            reason = "; ".join(parts) or "неизвестно"
            warn(f"sub={sub_id}: нет кандидатов — {reason}")
            results[sub_id] = {"status":"no_candidates","reason":reason}

    return results

# ═══════════════════════════════════════════════════════════════════════════════
# БЛОК 5: Price watches
# ═══════════════════════════════════════════════════════════════════════════════
async def check_watches(r):
    hdr("БЛОК 5 — Слежение за ценой")
    prefix = "flight_bot:"
    keys = await r.keys(f"{prefix}watch:*")
    if not keys:
        info("Нет активных отслеживаний"); return
    ok(f"Активных отслеживаний: {len(keys)}")
    token = os.getenv("AVIASALES_TOKEN","").strip()

    # Грузим первые 5 ключей одним pipeline
    watch_keys = keys[:5]
    try:
        pipe = r.pipeline(transaction=False)
        for key in watch_keys:
            pipe.get(key)
        raw_values = await pipe.execute()
    except Exception as e:
        err(f"Pipeline ошибка при загрузке отслеживаний: {e}"); return

    import aiohttp
    for key, raw in zip(watch_keys, raw_values):
        if not raw: continue
        try:
            w = json.loads(raw)
            orig  = w.get("origin","?"); dest = w.get("dest","?")
            cur   = w.get("current_price",0)
            uid   = w.get("user_id","?")
            print(f"\n  🔎 {orig}→{dest}  текущая цена: {cur:,} ₽  user={uid}".replace(",","\u202f"))
            # Проверяем актуальную цену
            if token and orig != "?" and dest != "?":
                sd = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
                dd = w.get("depart_date", sd)
                params = {"origin":orig,"destination":dest,"departure_at":dd,
                          "currency":"rub","token":token,"group_by":"departure_at"}
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(GROUPED_URL, params=params,
                                         timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                flights = list(data.get("data",{}).values())
                                if flights:
                                    new_price = min(f.get("price") or f.get("value") or 0 for f in flights)
                                    change = ((new_price - cur) / cur * 100) if cur else 0
                                    sign = "📈" if change > 0 else ("📉" if change < 0 else "➡️")
                                    print(f"     {sign} Актуальная цена: {new_price:,} ₽  ({change:+.1f}%)".replace(",","\u202f"))
                                else:
                                    print(f"     Нет данных для {orig}→{dest} {dd}")
                except Exception as e:
                    print(f"     API: {e}")
        except Exception: pass

# ═══════════════════════════════════════════════════════════════════════════════
# ИТОГ
# ═══════════════════════════════════════════════════════════════════════════════
def summary(results: dict):
    hdr("ИТОГОВЫЙ ОТЧЁТ")
    print(f"\n  {G}✅ OK:{Z}       {_stats['ok']}")
    print(f"  {Y}⚠️  Предупрежд.:{Z} {_stats['w']}")
    print(f"  {R}❌ Ошибок:{Z}    {_stats['e']}")

    if results:
        print(f"\n  {'sub_id':<22} {'статус':<14} {'детали'}")
        print(f"  {'─'*22} {'─'*14} {'─'*32}")
        for sub_id, r in results.items():
            s = r.get("status","?")
            col = G if s=="ready" else (Y if s in ("blocked","no_candidates") else R)
            detail = r.get("best", r.get("reason",""))
            print(f"  {sub_id:<22} {col}{s:<14}{Z} {detail}")

    print()
    diag = []
    if any(r.get("status")=="blocked" for r in results.values()):
        diag.append(f"{Y}Уведомления блокируются кулдауном — подожди 24ч или сбрось через Redis{Z}")
    if any(r.get("status")=="no_candidates" for r in results.values()):
        diag.append(f"{Y}Нет данных по маршрутам — проверь токен, категорию, дату{Z}")
    if any(r.get("status")=="ready" for r in results.values()):
        diag.append(f"{G}Есть готовые к отправке — уведомления придут в следующем цикле (каждые 3ч){Z}")

    for d in diag:
        print(f"  → {d}")

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
async def make_redis():
    """Создаёт свежее Redis-соединение."""
    from redis.asyncio import from_url
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    r = from_url(url, decode_responses=False)
    await r.ping()
    return r


async def main():
    print(f"\n{W}{'═'*44}")
    print(f"  FlightBot Scout — Тест подписок")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*44}{Z}")

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

    # Блок 4 выполняется долго (много HTTP-запросов) — Upstash закрывает
    # TCP-соединение после ~2-3 мин простоя. Пересоздаём клиент перед блоком 5.
    try:
        await r.aclose()
    except Exception:
        pass
    r = await make_redis()
    if not r:
        err("Redis недоступен для блока 5"); summary(results); return

    await check_watches(r)
    summary(results)
    try:
        await r.aclose()
    except Exception:
        pass

if __name__ == "__main__":
    asyncio.run(main())