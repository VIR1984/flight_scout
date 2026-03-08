# utils/flight_utils.py
"""
Общие утилиты форматирования — вынесены из дублей в нескольких модулях.
Использовать:  from utils.flight_utils import _format_datetime, _format_duration, parse_passengers
"""
from datetime import datetime
import re


def _format_datetime(dt_str: str) -> str:
    """'2026-04-15T07:30:00+03:00' → '07:30'"""
    if not dt_str:
        return "??:??"
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return dt_str.split("T")[1][:5] if "T" in dt_str else "??:??"


def _format_duration(minutes: int) -> str:
    """125 → '2ч 5м'"""
    if not minutes:
        return "—"
    hours = minutes // 60
    mins  = minutes % 60
    parts = []
    if hours: parts.append(f"{hours}ч")
    if mins:  parts.append(f"{mins}м")
    return " ".join(parts) if parts else "—"


def parse_passengers(s: str) -> str:
    """
    Парсит строку пассажиров в код для API.

    Поддерживаемые форматы:
      ''           → '1'
      '3'          → '3'
      '211'        → '211'
      '2 взр'      → '2'
      '2 взр, 1 реб, 1 млад' → '211'
    """
    if not s:
        return "1"
    s = s.strip()
    if re.fullmatch(r"\d+", s):
        return s
    adults = children = infants = 0
    for part in s.split(","):
        part = part.strip().lower()
        m = re.search(r"\d+", part)
        n = int(m.group()) if m else 1
        if "взр" in part or "взросл" in part:
            adults = n
        elif "реб" in part or "дет" in part:
            children = n
        elif "мл" in part or "млад" in part:
            infants = n
        elif not adults:
            adults = n
    code = str(max(adults, 1))
    if children > 0: code += str(children)
    if infants  > 0: code += str(infants)
    return code
