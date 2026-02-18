# utils/date_validator.py
from datetime import datetime
from typing import Tuple, Optional

def parse_date(date_str: str) -> datetime:
    """
    Преобразует строку даты в объект datetime.
    Поддерживает форматы: ДД.ММ и ГГГГ-ММ-ДД
    """
    try:
        # Проверяем формат ГГГГ-ММ-ДД
        if '-' in date_str and len(date_str) == 10:
            date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        # Проверяем формат ДД.ММ
        elif '.' in date_str:
            day, month = map(int, date_str.split('.'))
            today = datetime.now()
            
            # Проверяем корректность даты
            if day < 1 or day > 31 or month < 1 or month > 12:
                raise ValueError("Некорректная дата")
            
            # Создаем дату с текущим годом
            try:
                date_obj = datetime(today.year, month, day)
            except ValueError:
                # Если дата недействительна, попробуем следующий год
                date_obj = datetime(today.year + 1, month, day)
            
            # Если дата уже прошла, используем следующий год
            if date_obj < today.replace(hour=0, minute=0, second=0, microsecond=0):
                date_obj = datetime(today.year + 1, month, day)
        else:
            raise ValueError(f"Неподдерживаемый формат даты: {date_str}")
        
        return date_obj
    except Exception as e:
        raise ValueError(f"Некорректный формат даты: {str(e)}")

def is_valid_departure_date(date_str: str) -> Tuple[bool, str]:
    """Проверяет, что дата вылета не в прошлом"""
    try:
        date_obj = parse_date(date_str)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        if date_obj < today:
            return False, "❌ Дата вылета не может быть в прошлом.\n\nПожалуйста, укажите дату в будущем."
        
        return True, ""
    except Exception as e:
        return False, f"❌ Ошибка при проверке даты: {str(e)}"

def is_valid_return_date(depart_date_str: str, return_date_str: str) -> Tuple[bool, str]:
    """Проверяет, что дата возврата не раньше даты вылета"""
    try:
        depart_date = parse_date(depart_date_str)
        return_date = parse_date(return_date_str)
        
        if return_date < depart_date:
            return False, "❌ Дата возврата не может быть раньше даты вылета.\n\nПожалуйста, укажите корректную дату возврата."
        
        return True, ""
    except Exception as e:
        return False, f"❌ Ошибка при проверке дат: {str(e)}"

def validate_flight_dates(depart_date: str, return_date: Optional[str] = None) -> Tuple[bool, str]:
    """
    Проверяет корректность дат для поиска рейсов.
    Возвращает (is_valid, error_message)
    Поддерживает форматы: ДД.ММ и ГГГГ-ММ-ДД
    """
    # Проверяем дату вылета
    is_valid, error_msg = is_valid_departure_date(depart_date)
    if not is_valid:
        return False, error_msg
    
    # Проверяем дату возврата, если она указана
    if return_date:
        is_valid, error_msg = is_valid_return_date(depart_date, return_date)
        if not is_valid:
            return False, error_msg
    
    return True, ""