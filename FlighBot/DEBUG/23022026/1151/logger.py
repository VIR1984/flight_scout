# utils/logger.py
import logging
import sys
from pathlib import Path

# Создаём папку logs
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# Формат логов
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Создаём logger
logger = logging.getLogger("flight_bot")
logger.setLevel(logging.DEBUG)

# Очищаем старые хендлеры
logger.handlers.clear()

# ─── Консольный хендлер ───
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(console_handler)

# ─── Файловый хендлер (все логи) ───
file_handler = logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(file_handler)

# ─── Файловый хендлер (только ошибки) ───
error_handler = logging.FileHandler(LOG_DIR / "errors.log", encoding="utf-8")
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
logger.addHandler(error_handler)

# Запрещаем propagate
logger.propagate = False