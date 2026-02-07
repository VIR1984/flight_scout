# utils/validators.py
"""
Валидаторы и утилиты для обработки пользовательского ввода
"""
import re
from datetime import datetime
from typing import Tuple, Optional

# ===== Валидация маршрута =====
def validate_route(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Парсит маршрут из текста: 'Москва - Сочи' или 'Москва Сочи'
    
    Args:
        text: пользовательский ввод
        
    Returns:
        (origin, dest) или (None, None) если не удалось распарсить
    """
    text = text.strip().lower()
    
    # Разделяем по дефису, стрелке или пробелу
    if any(sym in text for sym in ['-', '→', '—', '>']):
        parts = re.split(r'[-→—>]+', text)
    else:
        parts = text.split()
    
    if len(parts) < 2:
        return None, None
    
    origin = parts[0].strip()
    dest = parts[1].strip()
    
    # Если "везде" в начале
    if origin == "везде":
        return "везде", dest
    
    return origin, dest


# ===== Валидация даты =====
def validate_date(date_str: str) -> bool:
    """
    Проверяет формат даты ДД.ММ
    
    Args:
        date_str: строка с датой
        
    Returns:
        True если формат корректен
    """
    try:
        day, month = map(int, date_str.split('.'))
        return 1 <= day <= 31 and 1 <= month <= 12
    except:
        return False


# ===== Код пассажиров =====
def build_passenger_code(adults: int, children: int = 0, infants: int = 0) -> str:
    """
    Формирует код пассажиров с ограничениями Aviasales
    
    Args:
        adults: взрослые (минимум 1)
        children: дети 2-11 лет
        infants: младенцы <2 лет (не больше взрослых)
        
    Returns:
        Код в формате "1", "21", "211" и т.д.
    """
    adults = max(1, adults)  # Минимум 1 взрослый
    total = adults + children + infants
    
    # Максимум 9 человек
    if total > 9:
        remaining = 9 - adults
        if children + infants > remaining:
            children = min(children, remaining)
            infants = max(0, remaining - children)
    
    # Младенцев не больше взрослых
    if infants > adults:
        infants = adults
    
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    
    return code


# ===== Описание пассажиров =====
def build_passenger_desc(code: str) -> str:
    """
    Формирует читаемое описание пассажиров из кода
    
    Args:
        code: код пассажиров (например, "211")
        
    Returns:
        Описание (например, "2 взр., 1 реб., 1 мл.")
    """
    try:
        adults = int(code[0])
        children = int(code[1]) if len(code) > 1 else 0
        infants = int(code[2]) if len(code) > 2 else 0
        
        parts = []
        if adults: parts.append(f"{adults} взр.")
        if children: parts.append(f"{children} реб.")
        if infants: parts.append(f"{infants} мл.")
        
        return ", ".join(parts) if parts else "1 взр."
    except:
        return "1 взр."


# ===== Парсинг пассажиров из текста =====
def parse_passengers(s: str) -> str:
    """
    Парсит пассажиров из текстовой строки
    
    Args:
        s: строка (например, "2 взр, 1 реб, 1 мл" или "3")
        
    Returns:
        Код пассажиров
    """
    if not s:
        return "1"
    
    # Если просто цифра
    if s.isdigit():
        return s
    
    adults = children = infants = 0
    
    for part in s.split(","):
        part = part.strip().lower()
        
        # Ищем число в части
        num_match = re.search(r"\d+", part)
        n = int(num_match.group()) if num_match else 1
        
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
    
    # Если не нашли взрослых — ставим 1 по умолчанию
    if adults == 0:
        adults = 1
    
    return build_passenger_code(adults, children, infants)


# ===== Форматирование даты =====
def format_user_date(date_str: str, base_year: int = 2026) -> str:
    """
    Форматирует дату для отображения пользователю (ДД.ММ.ГГГГ)
    
    Args:
        date_str: дата в формате ДД.ММ
        base_year: базовый год (по умолчанию 2026)
        
    Returns:
        Дата в формате ДД.ММ.ГГГГ
    """
    try:
        d, m = map(int, date_str.split('.'))
        
        # Автоматически определяем год
        # Если дата уже прошла — берём следующий год
        now = datetime.now()
        current_year = now.year
        current_month = now.month
        current_day = now.day
        
        if (m < current_month) or (m == current_month and d < current_day):
            year = current_year + 1
        else:
            year = current_year
        
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str


# ===== Нормализация даты для API =====
def normalize_date(date_str: str) -> str:
    """
    Приводит дату к формату ГГГГ-ММ-ДД для API
    
    Args:
        date_str: дата в формате ДД.ММ
        
    Returns:
        Дата в формате ГГГГ-ММ-ДД
    """
    day, month = map(int, date_str.split('.'))
    
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    current_day = now.day
    
    # Если дата уже прошла в этом году — берём следующий год
    if (month < current_month) or (month == current_month and day < current_day):
        year = current_year + 1
    else:
        year = current_year
    
    return f"{year}-{month:02d}-{day:02d}"


# ===== Форматирование даты для ссылки на Aviasales =====
def format_avia_link_date(date_str: str) -> str:
    """
    Преобразует '10.03' → '1003' для ссылки на Aviasales
    
    Args:
        date_str: дата в формате ДД.ММ
        
    Returns:
        Дата в формате ДДММ
    """
    try:
        d, m = date_str.split('.')
        return f"{int(d):02d}{int(m):02d}"
    except:
        return "0101"