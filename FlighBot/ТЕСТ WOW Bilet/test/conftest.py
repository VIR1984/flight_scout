# test/conftest.py
"""
Конфигурация pytest для папки test/.

Добавляет корень проекта (папку выше test/) в sys.path,
чтобы импорты вида "from handlers.xxx import yyy" работали
при запуске pytest из любой папки.
"""

import sys
from pathlib import Path

# Корень проекта = папка на уровень выше test/
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))