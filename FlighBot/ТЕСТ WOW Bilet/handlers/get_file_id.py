# handlers/get_file_id.py
"""
Вспомогательный хендлер для загрузки PDF-документов.

Как использовать:
  1. Открой бота
  2. Отправь PDF файл (просто перетащи или прикрепи как документ)
  3. Бот ответит file_id — скопируй его в .env:
       DOC_PRIVACY_POLICY_ID=BQACAgIAAxkBAAI...
       DOC_USER_AGREEMENT_ID=BQACAgIAAxkBAAI...
       DOC_PUBLIC_OFFER_ID=BQACAgIAAxkBAAI...
  4. Перезапусти бота — документы появятся в /help → Документы

Доступно ТОЛЬКО для ADMIN_USER_ID.
"""
import os
from utils.admin import is_admin
import logging
from aiogram import Router, F
from aiogram.types import Message

logger = logging.getLogger(__name__)
router = Router()

DOC_ENV_HINTS = {
    "Политика конфиденциальности": "DOC_PRIVACY_POLICY_ID",
    "политик":                     "DOC_PRIVACY_POLICY_ID",
    "privacy":                     "DOC_PRIVACY_POLICY_ID",
    "соглашение":                  "DOC_USER_AGREEMENT_ID",
    "agreement":                   "DOC_USER_AGREEMENT_ID",
    "оферт":                       "DOC_PUBLIC_OFFER_ID",
    "offer":                       "DOC_PUBLIC_OFFER_ID",
}


@router.message(F.document)
async def handle_document_upload(message: Message):
    """Принимает любой документ от ADMIN и возвращает file_id."""
    if not is_admin(message.from_user.id):
        return  # не мешаем другим хендлерам

    doc = message.document
    if not doc:
        return

    file_id   = doc.file_id
    file_name = doc.file_name or "файл"
    mime      = doc.mime_type or ""

    # Определяем подсказку по имени файла
    hint_key = ""
    fname_lower = file_name.lower()
    for keyword, env_var in DOC_ENV_HINTS.items():
        if keyword in fname_lower:
            hint_key = env_var
            break

    is_pdf = mime == "application/pdf" or fname_lower.endswith(".pdf")
    pdf_warning = "" if is_pdf else "\n⚠️ Файл не является PDF — убедись что отправляешь правильный формат."

    hint_line = f"\n\n💡 Похоже на: <code>{hint_key}</code>" if hint_key else (
        "\n\n💡 Добавь в .env нужную переменную:\n"
        "  <code>DOC_PRIVACY_POLICY_ID</code>  — политика конфиденциальности\n"
        "  <code>DOC_USER_AGREEMENT_ID</code>  — пользовательское соглашение\n"
        "  <code>DOC_PUBLIC_OFFER_ID</code>    — публичная оферта"
    )

    text = (
        f"📎 <b>Файл получен:</b> {file_name}{pdf_warning}\n\n"
        f"<b>file_id:</b>\n<code>{file_id}</code>"
        f"{hint_line}\n\n"
        f"Скопируй <code>file_id</code> и добавь в Railway → Variables, затем перезапусти бота."
    )

    await message.answer(text, parse_mode="HTML")
    logger.info(f"[GetFileId] admin={message.from_user.id} file={file_name} id={file_id}")