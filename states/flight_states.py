from aiogram.fsm.state import State, StatesGroup

class FlightSearch(StatesGroup):
    """Пошаговый поиск авиабилетов"""
    # Шаг 1: Маршрут
    route = State()           # Город отправления - город прибытия
    
    # Шаг 2: Дата вылета
    depart_date = State()
    
    # Шаг 3: Нужен ли обратный билет
    need_return = State()
    
    # Шаг 4: Дата возврата (если нужен)
    return_date = State()
    
    # Шаг 5: Пассажиры
    adults = State()          # Взрослые
    children = State()        # Дети
    infants = State()         # Младенцы
    
    # Финал: Подтверждение и поиск
    confirm = State()