from aiogram.fsm.state import State, StatesGroup

class FlightSearch(StatesGroup):
    """Пошаговый поиск авиабилетов"""
    # Шаг 1а: Города вылета (один или несколько через запятую)
    origin_cities = State()
    # Шаг 1б: Редактирование списка городов вылета (добавление)
    edit_origin_add = State()
    # Шаг 1в: Город прилёта
    dest_city = State()
    # Шаг 1: Маршрут одной строкой (legacy и start.py)
    route = State()
    # Выбор аэропорта при мультиаэропортных городах (start.py)
    choose_airport = State()
    # Шаг 2: Дата вылета
    depart_date = State()
    # Шаг 3: Нужен ли обратный билет
    need_return = State()
    # Шаг 4: Дата возврата (если нужен)
    return_date = State()
    # Шаг 5: Тип рейса
    flight_type = State()
    # Шаг 6: Пассажиры
    adults = State()
    has_children = State()   # используется в start.py (Yes/No вопрос)
    children = State()
    infants = State()
    # Финал: Подтверждение и поиск
    confirm = State()