# utils/cities_loader.py
import json
import os
import aiohttp
from pathlib import Path
from typing import Dict, Optional, List
from utils.logger import logger

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
    
    try:
        # Проверяем локальный кэш
        if CACHE_FILE.exists():
            import time
            age = time.time() - CACHE_FILE.stat().st_mtime
            if age < CACHE_TTL_SECONDS:
                logger.info("📦 Загружаем города из локального кэша...")
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _build_dictionaries(data)
                return True
            else:
                logger.info("🔄 Кэш устарел, обновляем...")
        
        # Скачиваем с API
        logger.info("🌐 Загружаем города из Travelpayouts API...")
        async with aiohttp.ClientSession() as session:
            async with session.get(CITIES_API_URL, timeout=30) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                data = await resp.json()
        
        # Сохраняем в кэш
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        
        _build_dictionaries(data)
        logger.info(f"✅ Загружено {len(CITY_TO_IATA)} городов")
        return True
        
    except Exception as e:
        logger.error(f"❌ Ошибка загрузки городов: {e}")
        # Пробуем загрузить fallback из cities.py
        return _load_fallback()

def _load_fallback() -> bool:
    """Загружает города из старого cities.py как fallback"""
    global CITY_TO_IATA, IATA_TO_CITY, CITIES_DATA
    try:
        from utils.cities import CITY_TO_IATA as FALLBACK_C2I, IATA_TO_CITY as FALLBACK_I2C
        CITY_TO_IATA.update(FALLBACK_C2I)
        IATA_TO_CITY.update(FALLBACK_I2C)
        logger.warning("⚠️ Используется fallback из cities.py")
        return True
    except ImportError:
        logger.error("❌ Не удалось загрузить fallback")
        return False

def _build_dictionaries(data: List[dict]):
    """Строит словари из данных API"""
    global CITY_TO_IATA, IATA_TO_CITY, CITIES_DATA
    
    CITY_TO_IATA.clear()
    IATA_TO_CITY.clear()
    CITIES_DATA.clear()
    
    for city in data:
        # Пропускаем города без летного аэропорта
        if not city.get("has_flightable_airport", False):
            continue
        
        iata = city.get("code")
        if not iata or len(iata) != 3:
            continue
        
        # Берем название: cases['su'] (именительный) или name
        name = city.get("cases", {}).get("su") or city.get("name")
        if not name:
            continue
        
        # Основной маппинг
        norm_name = _normalize_name(name)
        CITY_TO_IATA[norm_name] = iata
        IATA_TO_CITY[iata] = name
        CITIES_DATA[iata] = city
        
        # Добавляем английское название как алиас
        en_name = city.get("name_translations", {}).get("en")
        if en_name:
            CITY_TO_IATA[_normalize_name(en_name)] = iata
    
    # Добавляем ручные алиасы (перезаписывают API, если есть конфликт)
    for alias, iata in MANUAL_ALIASES.items():
        CITY_TO_IATA[_normalize_name(alias)] = iata
        if iata not in IATA_TO_CITY:
            IATA_TO_CITY[iata] = alias.capitalize()

def get_iata(city_name: str) -> Optional[str]:
    """Возвращает IATA-код по названию города"""
    if not city_name:
        return None
    norm = _normalize_name(city_name)
    return CITY_TO_IATA.get(norm)

def get_city_name(iata: str) -> Optional[str]:
    """Возвращает название города по IATA-коду"""
    return IATA_TO_CITY.get(iata)

def get_city_info(iata: str) -> Optional[dict]:
    """Возвращает полную информацию о городе по IATA"""
    return CITIES_DATA.get(iata)

def search_cities(query: str, limit: int = 10) -> List[dict]:
    """Поиск городов по подстроке (для автокомплита)"""
    if not query:
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
    return results