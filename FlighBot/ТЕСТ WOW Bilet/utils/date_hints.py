# utils/date_hints.py
"""
Динамические примеры дат для подсказок пользователю.
Примеры всегда «в будущем» и не уходят в прошлое.
"""
from datetime import date, timedelta


def _next_weekday(d: date, weekday: int) -> date:
    """Ближайший день недели (0=пн … 6=вс), не раньше d + 1."""
    days = (weekday - d.weekday()) % 7 or 7
    return d + timedelta(days=days)


def hint_depart(gap_days: int = 7) -> str:
    """
    Пример даты вылета — через gap_days дней от сегодня,
    округлённый до ближайшей пятницы (удобная дата вылета).
    Формат: ДД.ММ
    """
    base = date.today() + timedelta(days=gap_days)
    friday = _next_weekday(base, 4)   # 4 = пятница
    return friday.strftime("%d.%m")


def hint_return(depart_hint: str, gap_days: int = 7) -> str:
    """
    Пример даты возврата — через gap_days дней после даты вылета.
    depart_hint — строка ДД.ММ, возвращённая hint_depart().
    Формат: ДД.ММ
    """
    day, month = map(int, depart_hint.split("."))
    year = date.today().year
    try:
        dep = date(year, month, day)
    except ValueError:
        dep = date.today() + timedelta(days=7)
    # Если дата уже в прошлом — берём следующий год
    if dep < date.today():
        dep = dep.replace(year=year + 1)
    return (dep + timedelta(days=gap_days)).strftime("%d.%m")
