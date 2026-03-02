# utils/cities.py
import json
import os
import csv
from pathlib import Path

# Путь к данным
DATA_DIR = Path(__file__).parent.parent / "data"

# Глобальные хабы для режима "везде"
GLOBAL_HUBS = [
    "MOW", "LED", "IST", "LON", "PAR", "FRA", "AMS",
    "DXB", "AUH", "BKK", "SIN", "TYO", "NYC", "LAX"
]

def _load_manual_mapping() -> dict:
    """Загружает ручной маппинг город → IATA"""
    mapping_path = DATA_DIR / "iata_manual.json"
    if not mapping_path.exists():
        print(f"⚠️ Файл маппинга не найден: {mapping_path}")
        return {}
    
    try:
        with open(mapping_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Ошибка загрузки маппинга: {e}")
        return {}

def generate_cities_json():
    """Генерирует cities.json из CSV + ручного маппинга"""
    mapping = _load_manual_mapping()
    if not mapping:
        print("❌ Нет данных для маппинга IATA-кодов")
        return False
    
    csv_path = DATA_DIR / "cities.csv"
    if not csv_path.exists():
        print(f"⚠️ Файл не найден: {csv_path}")
        return False
    
    cities_list = []
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                city_name = row.get("city_rus", "").strip()
                if not city_name:
                    continue
                
                # Поиск по нижнему регистру (игнорируем регистр)
                iata = mapping.get(city_name.lower())
                if iata:
                    cities_list.append({
                        "city_rus": city_name,
                        "iata_code": iata
                    })
    except Exception as e:
        print(f"⚠️ Ошибка чтения CSV: {e}")
        return False
    
    # Сохраняем результат
    output_path = DATA_DIR / "cities.json"
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(cities_list, f, ensure_ascii=False, indent=2)
        print(f"✅ Создано {len(cities_list)} записей в {output_path.name}")
        return True
    except Exception as e:
        print(f"⚠️ Ошибка записи JSON: {e}")
        return False

# Загружаем данные при импорте модуля
CITY_TO_IATA = {}
try:
    cities_path = DATA_DIR / "cities.json"
    if cities_path.exists():
        with open(cities_path, "r", encoding="utf-8") as f:
            cities_data = json.load(f)
            for city in cities_
                CITY_TO_IATA[city["city_rus"].lower()] = city["iata_code"]
    else:
        # Автогенерация при первом запуске
        if generate_cities_json():
            # Повторная загрузка после генерации
            with open(cities_path, "r", encoding="utf-8") as f:
                cities_data = json.load(f)
                for city in cities_data:
                    CITY_TO_IATA[city["city_rus"].lower()] = city["iata_code"]
except Exception as e:
    print(f"⚠️ Ошибка инициализации CITY_TO_IATA: {e}")