# utils/admin.py
"""
Единая проверка прав администратора.
Использовать:  from utils.admin import is_admin
"""
import os


def is_admin(user_id: int) -> bool:
    """True если user_id совпадает с ADMIN_USER_ID из окружения."""
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    return bool(admin_id) and str(user_id) == admin_id
