# utils/cities_loader.py
import json
import aiohttp
from pathlib import Path
from typing import Dict, Optional, List

CITIES_API_URL = "https://api.travelpayouts.com/data/ru/cities.json"
CACHE_FILE = Path("data/cities_cache.json")
CACHE_TTL_SECONDS = 86400 * 7  # 7 дней

# Глобальные словари (заполняются при инициализации)
CITY_TO_IATA: Dict[str, str] = {}
IATA_TO_CITY: Dict[str, str] = {}
CITIES_DATA: Dict[str, dict] = {}

# Ручные алиасы (поверх API-данных) — для популярных сокращений
MANUAL_ALIASES = {
    "москва": "MOW", "мск": "MOW",
    "санкт-петербург": "LED", "спб": "LED", "питер": "LED", "ленинград": "LED",
    "сочи": "AER", "адлер": "AER",
    "екатеринбург": "SVX", "екб": "SVX",
    "нижний новгород": "GOJ", "нижний": "GOJ",
    "набережные челны": "NBC", "челны": "NBC",
    "южно-сахалинск": "UUS", "сахалин": "UUS",
    "ростов-на-дону": "ROV", "ростов на дону": "ROV",
}

def _normalize_name(name: str) -> str:
    """Приводит название города к ключу для поиска"""
    return name.lower().strip().replace("ё", "е").replace("-", " ").replace("  ", " ")

async def load_cities_from_api() -> bool:
    """Загружает города из API Travelpayouts и строит словари"""
    global CITY_TO_IATA, IATA_TO_CITY, CITIES_DATA
    
    print("=" * 60)
    print("🌍 [CITIES_LOADER] Начало загрузки городов...")
    print("=" * 60)
    
    try:
        # Проверяем локальный кэш
        print(f"[CITIES_LOADER] Проверяем кэш-файл: {CACHE_FILE}")
        if CACHE_FILE.exists():
            import time
            age = time.time() - CACHE_FILE.stat().st_mtime
            age_hours = age / 3600
            print(f"[CITIES_LOADER] Возраст кэша: {age_hours:.2f} часов (максимум: {CACHE_TTL_SECONDS/3600:.0f} часов)")
            
            if age < CACHE_TTL_SECONDS:
                print("[CITIES_LOADER] ✅ Кэш актуален, загружаем из файла...")
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _build_dictionaries(data)
                print(f"[CITIES_LOADER] ✅ Загружено {len(CITY_TO_IATA)} городов из кэша")
                print("=" * 60)
                return True
            else:
                print("[CITIES_LOADER] ⚠️ Кэш устарел, обновляем из API...")
        else:
            print("[CITIES_LOADER] ⚠️ Кэш-файл не найден, загружаем из API...")
        
        # Скачиваем с API
        print(f"[CITIES_LOADER] 🌐 Запрос к API: {CITIES_API_URL}")
        async with aiohttp.ClientSession() as session:
            async with session.get(CITIES_API_URL, timeout=30) as resp:
                print(f"[CITIES_LOADER] 📡 Статус ответа: {resp.status}")
                if resp.status != 200:
                    print(f"[CITIES_LOADER] ❌ Ошибка HTTP {resp.status}")
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
                print(f"[CITIES_LOADER] 📦 Получено {len(data)} записей из API")
        
        # Сохраняем в кэш
        print(f"[CITIES_LOADER] 💾 Сохраняем кэш в {CACHE_FILE}")
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("[CITIES_LOADER] ✅ Кэш сохранён")
        
        _build_dictionaries(data)
        print(f"[CITIES_LOADER] ✅ Загружено {len(CITY_TO_IATA)} городов")
        print(f"[CITIES_LOADER] 📊 IATA кодов: {len(IATA_TO_CITY)}")
        print(f"[CITIES_LOADER] 📊 Детальных записей: {len(CITIES_DATA)}")
        print("=" * 60)
        return True
        
    except Exception as e:
        print(f"[CITIES_LOADER] ❌ Ошибка загрузки: {e}")
        print("[CITIES_LOADER] 🔄 Пробуем загрузить fallback из cities.py...")
        return _load_fallback()

def _load_fallback() -> bool:
    """Загружает города из старого cities.py как fallback"""
    global CITY_TO_IATA, IATA_TO_CITY, CITIES_DATA
    try:
        print("[CITIES_LOADER] 📦 Загружаем fallback из utils.cities...")
        from utils.cities import CITY_TO_IATA as FALLBACK_C2I, IATA_TO_CITY as FALLBACK_I2C
        CITY_TO_IATA.update(FALLBACK_C2I)
        IATA_TO_CITY.update(FALLBACK_I2C)
        print(f"[CITIES_LOADER] ✅ Загружено {len(CITY_TO_IATA)} городов из fallback")
        print("=" * 60)
        return True
    except ImportError as e:
        print(f"[CITIES_LOADER] ❌ Не удалось загрузить fallback: {e}")
        print("=" * 60)
        return False

def _build_dictionaries(data: List[dict]):
    """Строит словари из данных API"""
    global CITY_TO_IATA, IATA_TO_CITY, CITIES_DATA
    
    print("[CITIES_LOADER] 🔨 Построение словарей...")
    CITY_TO_IATA.clear()
    IATA_TO_CITY.clear()
    CITIES_DATA.clear()
    
    skipped_no_airport = 0
    skipped_no_code = 0
    skipped_no_name = 0
    processed = 0
    
    for city in data:
        # Пропускаем города без летного аэропорта
        if not city.get("has_flightable_airport", False):
            skipped_no_airport += 1
            continue
        
        iata = city.get("code")
        if not iata or len(iata) != 3:
            skipped_no_code += 1
            continue
        
        # Берем название: cases['su'] (именительный) или name
        name = city.get("cases", {}).get("su") or city.get("name")
        if not name:
            skipped_no_name += 1
            continue
        
        # Основной маппинг
        norm_name = _normalize_name(name)
        CITY_TO_IATA[norm_name] = iata
        IATA_TO_CITY[iata] = name
        CITIES_DATA[iata] = city
        processed += 1
        
        # Добавляем английское название как алиас
        en_name = city.get("name_translations", {}).get("en")
        if en_name:
            CITY_TO_IATA[_normalize_name(en_name)] = iata
    
    # Добавляем ручные алиасы (перезаписывают API, если есть конфликт)
    manual_added = 0
    for alias, iata in MANUAL_ALIASES.items():
        norm_alias = _normalize_name(alias)
        if norm_alias not in CITY_TO_IATA:
            CITY_TO_IATA[norm_alias] = iata
            manual_added += 1
        if iata not in IATA_TO_CITY:
            IATA_TO_CITY[iata] = alias.capitalize()
            manual_added += 1
    
    print(f"[CITIES_LOADER] 📊 Обработано: {processed}")
    print(f"[CITIES_LOADER] 📊 Пропущено (нет аэропорта): {skipped_no_airport}")
    print(f"[CITIES_LOADER] 📊 Пропущено (нет кода): {skipped_no_code}")
    print(f"[CITIES_LOADER] 📊 Пропущено (нет названия): {skipped_no_name}")
    print(f"[CITIES_LOADER] 📊 Добавлено ручных алиасов: {manual_added}")

def get_iata(city_name: str) -> Optional[str]:
    """Возвращает IATA-код по названию города"""
    if not city_name:
        print(f"[CITIES_LOADER] [get_iata] ⚠️ Пустое название города")
        return None
    
    norm = _normalize_name(city_name)
    result = CITY_TO_IATA.get(norm)
    
    if result:
        print(f"[CITIES_LOADER] [get_iata] ✅ '{city_name}' → '{result}'")
    else:
        print(f"[CITIES_LOADER] [get_iata] ❌ '{city_name}' → не найдено (norm: '{norm}')")
    
    return result


def _levenshtein(a: str, b: str) -> int:
    """Расстояние Левенштейна между двумя строками."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(0 if ca==cb else 1)))
        prev = curr
    return prev[-1]


def fuzzy_get_iata(city_name: str, max_dist: int = 2) -> tuple[Optional[str], Optional[str]]:
    """
    Нечёткий поиск города: исправляет опечатки типа «масква» → «Москва».

    Возвращает (iata, правильное_название) или (None, None).
    max_dist=2 ловит большинство опечаток не давая ложных срабатываний.
    """
    if not city_name or not CITY_TO_IATA:
        return None, None

    q = _normalize_name(city_name)
    if len(q) < 3:
        return None, None

    best_iata  = None
    best_name  = None
    best_dist  = max_dist + 1

    for norm_city, iata in CITY_TO_IATA.items():
        # Пропускаем слишком короткие и слишком длинные названия
        # (не стоит предлагать "Рим" вместо "Ром", или сравнивать с очень длинными)
        if abs(len(norm_city) - len(q)) > 4:
            continue
        d = _levenshtein(q, norm_city)
        if d < best_dist:
            best_dist = d
            best_iata = iata
            best_name = IATA_TO_CITY.get(iata, norm_city.capitalize())

    if best_iata:
        print(f"[CITIES_LOADER] [fuzzy_get_iata] '{city_name}' → '{best_name}' ({best_iata}), dist={best_dist}")
        return best_iata, best_name
    return None, None

def get_city_name(iata: str) -> Optional[str]:
    """Возвращает название города по IATA-коду"""
    if not iata:
        print(f"[CITIES_LOADER] [get_city_name] ⚠️ Пустой IATA код")
        return None
    
    result = IATA_TO_CITY.get(iata)
    
    if result:
        print(f"[CITIES_LOADER] [get_city_name] ✅ '{iata}' → '{result}'")
    else:
        print(f"[CITIES_LOADER] [get_city_name] ❌ '{iata}' → не найдено")
    
    return result

# ── Словарь стран: русское название → ISO-код + топ-4 города ─────────────────
# Топовые города выбраны по популярности авиамаршрутов из России
COUNTRY_TOP_CITIES: Dict[str, List[str]] = {
    # IATA-коды топ-городов по странам
    "AE": ["DXB", "AUH", "SHJ", "RKT"],   # ОАЭ
    "TR": ["IST", "AYT", "ADB", "DLM"],    # Турция
    "TH": ["BKK", "HKT", "CNX", "USM"],    # Таиланд
    "CN": ["PEK", "PVG", "CAN", "CTU"],    # Китай
    "EG": ["CAI", "HRG", "SSH", "LXR"],    # Египет
    "GR": ["ATH", "HER", "RHO", "CFU"],    # Греция
    "ES": ["BCN", "MAD", "PMI", "AGP"],    # Испания
    "IT": ["FCO", "MXP", "VCE", "NAP"],    # Италия
    "FR": ["CDG", "NCE", "LYS", "MRS"],    # Франция
    "DE": ["BER", "FRA", "MUC", "DUS"],    # Германия
    "CZ": ["PRG"],                          # Чехия
    "HU": ["BUD"],                          # Венгрия
    "AT": ["VIE", "SZG", "GRZ", "INN"],    # Австрия
    "NL": ["AMS"],                          # Нидерланды
    "PL": ["WAW", "KRK", "GDN", "WRO"],    # Польша
    "FI": ["HEL", "TMP", "TKU", "OUL"],    # Финляндия
    "SE": ["ARN", "GOT", "MMX", "UME"],    # Швеция
    "NO": ["OSL", "BGO", "TRF", "SVG"],    # Норвегия
    "DK": ["CPH", "AAL", "AAR", "BLL"],    # Дания
    "PT": ["LIS", "OPO", "FAO", "FNC"],    # Португалия
    "SG": ["SIN"],                          # Сингапур
    "MY": ["KUL", "PEN", "BKI", "LGK"],    # Малайзия
    "ID": ["CGK", "DPS", "SUB", "UPG"],    # Индонезия
    "VN": ["HAN", "SGN", "DAD", "HPH"],    # Вьетнам
    "IN": ["DEL", "BOM", "BLR", "MAA"],    # Индия
    "JP": ["NRT", "KIX", "CTS", "FUK"],    # Япония
    "KR": ["ICN", "PUS", "CJU", "TAE"],    # Корея
    "US": ["JFK", "LAX", "MIA", "ORD"],    # США
    "CA": ["YYZ", "YVR", "YUL", "YYC"],    # Канада
    "AU": ["SYD", "MEL", "BNE", "PER"],    # Австралия
    "MV": ["MLE", "GAN"],                   # Мальдивы
    "LK": ["CMB", "HRI"],                   # Шри-Ланка
    "CY": ["LCA", "PFO"],                   # Кипр
    "MT": ["MLA"],                          # Мальта
    "HR": ["ZAG", "SPU", "DBV", "ZAD"],    # Хорватия
    "RS": ["BEG"],                          # Сербия
    "GE": ["TBS", "BUS"],                   # Грузия
    "AM": ["EVN"],                          # Армения
    "AZ": ["GYD"],                          # Азербайджан
    "KZ": ["ALA", "NQZ", "CIT", "URA"],    # Казахстан
    "UZ": ["TAS", "SKD", "BHK", "NCU"],    # Узбекистан
    "BY": ["MSQ"],                          # Беларусь
    "IL": ["TLV"],                          # Израиль
    "JO": ["AMM", "AQJ"],                   # Иордания
    "MA": ["CMN", "RAK", "AGA", "FEZ"],    # Марокко
    "TN": ["TUN", "DJE", "MIR", "TOE"],    # Тунис
    "MX": ["MEX", "CUN", "GDL", "MTY"],    # Мексика
    "BR": ["GRU", "GIG", "BSB", "FOR"],    # Бразилия
    "TJ": ["DYU"],                          # Таджикистан
    "KG": ["FRU", "OSS"],                   # Кыргызстан
    "MN": ["ULN"],                          # Монголия
    "QA": ["DOH"],                          # Катар
    "BH": ["BAH"],                          # Бахрейн
    "KW": ["KWI"],                          # Кувейт
    "OM": ["MCT", "SLL", "DQM"],           # Оман
    "LB": ["BEY"],                          # Ливан
    "RU": ["MOW", "LED", "AER", "SVX"],    # Россия (для поиска внутри)
}

# Русские названия стран → ISO-код
COUNTRY_NAME_TO_ISO: Dict[str, str] = {
    "оаэ": "AE", "эмираты": "AE", "арабские эмираты": "AE", "дубай": "AE",
    "турция": "TR", "туреция": "TR", "турции": "TR",
    "таиланд": "TH", "тайланд": "TH", "тайланде": "TH",
    "китай": "CN",
    "египет": "EG",
    "греция": "GR",
    "испания": "ES",
    "италия": "IT",
    "франция": "FR",
    "германия": "DE", "германии": "DE",
    "чехия": "CZ", "чехии": "CZ",
    "венгрия": "HU",
    "австрия": "AT",
    "нидерланды": "NL", "голландия": "NL",
    "польша": "PL",
    "финляндия": "FI",
    "швеция": "SE",
    "норвегия": "NO",
    "дания": "DK",
    "португалия": "PT",
    "сингапур": "SG",
    "малайзия": "MY",
    "индонезия": "ID", "бали": "ID",
    "вьетнам": "VN",
    "индия": "IN",
    "япония": "JP",
    "корея": "KR", "южная корея": "KR",
    "сша": "US", "америка": "US", "соединенные штаты": "US",
    "канада": "CA",
    "австралия": "AU",
    "мальдивы": "MV", "мальдивские острова": "MV",
    "шри-ланка": "LK", "шри ланка": "LK", "шриланка": "LK", "шри-ланке": "LK", "шриланке": "LK", "шрыланка": "LK",
    "кипр": "CY",
    "мальта": "MT",
    "хорватия": "HR",
    "сербия": "RS",
    "грузия": "GE",
    "армения": "AM",
    "азербайджан": "AZ",
    "казахстан": "KZ",
    "узбекистан": "UZ",
    "беларусь": "BY", "белоруссия": "BY",
    "израиль": "IL",
    "иордания": "JO",
    "марокко": "MA",
    "тунис": "TN",
    "мексика": "MX",
    "бразилия": "BR",
    "таджикистан": "TJ",
    "кыргызстан": "KG", "киргизия": "KG",
    "монголия": "MN",
    "катар": "QA",
    "бахрейн": "BH",
    "кувейт": "KW",
    "оман": "OM",
    "ливан": "LB",
    "россия": "RU",
}


def get_country_cities(country_name: str) -> List[dict]:
    """
    По названию страны возвращает список топ-городов (до 4).
    Каждый элемент: {"iata": "BKK", "name": "Бангкок"}
    Возвращает [] если страна не найдена.
    """
    norm = _normalize_name(country_name)
    iso = COUNTRY_NAME_TO_ISO.get(norm)
    if not iso:
        return []

    iata_list = COUNTRY_TOP_CITIES.get(iso, [])
    result = []
    for iata in iata_list[:4]:
        name = IATA_TO_CITY.get(iata)
        if name:
            result.append({"iata": iata, "name": name})
        else:
            # Фолбэк — статичные имена для ключевых городов
            fallback = {
                "BKK": "Бангкок", "HKT": "Пхукет", "DXB": "Дубай", "IST": "Стамбул",
                "AYT": "Анталья", "CAI": "Каир", "HRG": "Хургада", "SSH": "Шарм-эль-Шейх",
                "ATH": "Афины", "BCN": "Барселона", "FCO": "Рим", "CDG": "Париж",
                "BER": "Берлин", "VIE": "Вена", "AMS": "Амстердам", "PRG": "Прага",
                "MLE": "Мале", "CMB": "Коломбо", "LCA": "Ларнака", "TLV": "Тель-Авив",
                "SIN": "Сингапур", "NRT": "Токио", "ICN": "Сеул", "DEL": "Дели",
                "KUL": "Куала-Лумпур", "DPS": "Денпасар", "HAN": "Ханой", "SGN": "Хошимин",
                "PEK": "Пекин", "PVG": "Шанхай", "CAN": "Гуанчжоу", "CTU": "Чэнду",
                "JFK": "Нью-Йорк", "LAX": "Лос-Анджелес", "YYZ": "Торонто", "SYD": "Сидней",
                "GRU": "Сан-Паулу", "CUN": "Канкун", "TBS": "Тбилиси", "EVN": "Ереван",
                "GYD": "Баку", "ALA": "Алматы", "TAS": "Ташкент", "DOH": "Доха",
            }.get(iata)
            if fallback:
                result.append({"iata": iata, "name": fallback})
    return result


def get_city_info(iata: str) -> Optional[dict]:
    """Возвращает полную информацию о городе по IATA"""
    result = CITIES_DATA.get(iata)
    if result:
        print(f"[CITIES_LOADER] [get_city_info] ✅ '{iata}' → найдено ({len(result)} полей)")
    else:
        print(f"[CITIES_LOADER] [get_city_info] ❌ '{iata}' → не найдено")
    return result

def search_cities(query: str, limit: int = 10) -> List[dict]:
    """Поиск городов по подстроке (для автокомплита)"""
    print(f"[CITIES_LOADER] [search_cities] 🔍 Поиск: '{query}', лимит: {limit}")
    
    if not query:
        print("[CITIES_LOADER] [search_cities] ⚠️ Пустой запрос")
        return []
    
    query_norm = _normalize_name(query)
    results = []
    
    for name, iata in CITY_TO_IATA.items():
        if query_norm in name and iata in IATA_TO_CITY:
            results.append({
                "name": IATA_TO_CITY[iata],
                "iata": iata,
                "country": CITIES_DATA.get(iata, {}).get("country_code")
            })
            if len(results) >= limit:
                break
    
    print(f"[CITIES_LOADER] [search_cities] ✅ Найдено {len(results)} результатов")
    return results

# Тест при прямом запуске
if __name__ == "__main__":
    import asyncio
    
    async def test():
        print("\n" + "=" * 60)
        print("🧪 [ТЕСТ] Запуск тестов cities_loader...")
        print("=" * 60 + "\n")
        
        # Загружаем города
        await load_cities_from_api()
        
        # Тестируем поиск
        print("\n" + "=" * 60)
        print("🧪 [ТЕСТ] Проверка get_iata()...")
        print("=" * 60)
        test_cities = ["москва", "спб", "сочи", "дубай", "бангкок", "несуществующий"]
        for city in test_cities:
            get_iata(city)
        
        print("\n" + "=" * 60)
        print("🧪 [ТЕСТ] Проверка get_city_name()...")
        print("=" * 60)
        test_iatas = ["MOW", "LED", "AER", "DXB", "BKK", "XXX"]
        for iata in test_iatas:
            get_city_name(iata)
        
        print("\n" + "=" * 60)
        print("🧪 [ТЕСТ] Проверка search_cities()...")
        print("=" * 60)
        search_cities("моск", limit=5)
        search_cities("петер", limit=5)
        
        print("\n" + "=" * 60)
        print("✅ [ТЕСТ] Завершено!")
        print("=" * 60 + "\n")
    
    asyncio.run(test())