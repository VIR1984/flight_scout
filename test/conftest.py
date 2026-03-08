# tests/conftest.py
"""
Конфигурация pytest для папки tests/.

Добавляет корень проекта (папку выше tests/) в sys.path,
чтобы импорты вида "from handlers.xxx import yyy" работали
при запуске pytest из любой папки:

    # из корня проекта:
    pytest tests/ -v

    # из самой папки tests/:
    cd tests && pytest -v
"""

import sys
from pathlib import Path

# Корень проекта = папка на уровень выше tests/
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))