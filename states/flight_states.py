from aiogram.fsm.state import State, StatesGroup

class FlightSearch(StatesGroup):
    """Пошаговый поиск авиабилетов"""
    # Шаг 1а: Города вылета (один или несколько через запятую)
    origin_cities = State()
    # Шаг 1б: Редактирование списка городов вылета (добавление)
    edit_origin_add = State()
    # Шаг 1в: Город прилёта
    dest_city = State()
    # (устаревший) Шаг 1: Маршрут одной строкой — оставлен для совместимости
    route = State()
    # Шаг 2: Дата вылета
    depart_date = State()
    # Шаг 3: Нужен ли обратный билет
    need_return = State()
    # Шаг 4: Дата возврата (если нужен)
    return_date = State()
    # Шаг 5: Тип рейса
    flight_type = State()     # Прямой / С пересадкой / Все
    # Шаг 6: Пассажиры
    adults = State()          # Взрослые
    children = State()        # Дети
    infants = State()         # Младенцы
    # Финал: Подтверждение и поиск
    confirm = State()