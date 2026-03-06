# handlers/help.py
"""
Раздел справки с документами (PDF-файлы) и описанием бота.

Документы хранятся как file_id в переменных окружения — загружаешь
PDF один раз через @документ → боту, получаешь file_id, кладёшь в .env.

Переменные окружения:
  DOC_PRIVACY_POLICY_ID   — file_id PDF «Политика конфиденциальности»
  DOC_USER_AGREEMENT_ID   — file_id PDF «Пользовательское соглашение»
  DOC_PUBLIC_OFFER_ID     — file_id PDF «Публичная оферта»

Если переменная не задана — кнопка показывается серой с пометкой «скоро»....
"""
import os
import logging
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext

logger = logging.getLogger(__name__)
router = Router()

# ── Конфигурация документов ───────────────────────────────────────────────────
DOCS = {
    "privacy":   ("🔒 Политика конфиденциальности", "DOC_PRIVACY_POLICY_ID"),
    "agreement": ("📋 Пользовательское соглашение",  "DOC_USER_AGREEMENT_ID"),
    "offer":     ("📄 Публичная оферта",              "DOC_PUBLIC_OFFER_ID"),
}


def _docs_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру документов. Кнопка неактивна если file_id не задан."""
    buttons = []
    for key, (label, env_var) in DOCS.items():
        file_id = os.getenv(env_var, "").strip()
        if file_id:
            buttons.append([InlineKeyboardButton(
                text=label,
                callback_data=f"doc_send:{key}",
            )])
        else:
            buttons.append([InlineKeyboardButton(
                text=f"{label}  (скоро)",
                callback_data="doc_not_ready",
            )])
    buttons.append([InlineKeyboardButton(text="↩️ Назад к справке", callback_data="help_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 Документы",    callback_data="help_docs")],
        [InlineKeyboardButton(text="✈️ Начать поиск", callback_data="start_search")],
    ])


HELP_TEXT = (
    "<b>Справка</b>\n\n"
    "<b>Поиск</b> — найти билеты по маршруту, датам и числу пассажиров.\n"
    "<b>Маршрут</b> — составной поиск с несколькими перелётами.\n"
    "<b>Горячие</b> — уведомление, когда появится выгодный рейс.\n"
    "<b>Подписки</b> — просмотр и управление всеми уведомлениями.\n"
    "<b>Обратная связь</b> — сообщить об ошибке или предложить улучшение.\n\n"
    "——————————————\n\n"
    "<b>Конфиденциальность</b>\n\n"
    "Бот не хранит персональные данные. При поиске используются только маршрут, "
    "даты и число пассажиров — исключительно для запроса к Aviasales.\n\n"
    "Параметры подписок хранятся в зашифрованном виде и автоматически удаляются через 30 дней.\n\n"
    "Данные банковских карт боту не передаются. Оплата проходит напрямую на сайте партнёра.\n\n"
    "<i>📁 Юридические документы — кнопка ниже.</i>"
)


# ── Хендлеры ─────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=_help_keyboard())


async def show_help(target: Message | CallbackQuery, edit: bool = False):
    """Универсальный показ справки — из сообщения или из callback."""
    kb = _help_keyboard()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(HELP_TEXT, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await target.message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=kb)
        await target.answer()
    else:
        await target.answer(HELP_TEXT, parse_mode="HTML", reply_markup=kb)


@router.callback_query(lambda c: c.data == "help_main")
async def cb_help_main(callback: CallbackQuery):
    await show_help(callback)


@router.callback_query(lambda c: c.data == "help_info")
async def cb_help_info(callback: CallbackQuery):
    """Совместимость со старым callback_data из start.py."""
    await show_help(callback)


@router.callback_query(lambda c: c.data == "help_docs")
async def cb_help_docs(callback: CallbackQuery):
    """Показываем список документов."""
    text = (
        "<b>📁 Документы</b>\n\n"
        "Выбери документ чтобы получить его в формате PDF:\n\n"
    )
    for key, (label, env_var) in DOCS.items():
        status = "✅" if os.getenv(env_var, "").strip() else "🕐 скоро"
        text += f"  {label} — {status}\n"

    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_docs_keyboard())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=_docs_keyboard())
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("doc_send:"))
async def cb_doc_send(callback: CallbackQuery):
    """Отправляем PDF документ по его file_id."""
    key = callback.data.split(":", 1)[1]
    if key not in DOCS:
        await callback.answer("Документ не найден", show_alert=True)
        return

    label, env_var = DOCS[key]
    file_id = os.getenv(env_var, "").strip()

    if not file_id:
        await callback.answer("Документ пока не загружен. Скоро появится!", show_alert=True)
        return

    try:
        await callback.message.answer_document(
            document=file_id,
            caption=f"<b>{label}</b>",
            parse_mode="HTML",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"[Docs] Ошибка отправки {key}: {e}")
        await callback.answer("Ошибка при отправке файла. Попробуй позже.", show_alert=True)


@router.callback_query(lambda c: c.data == "doc_not_ready")
async def cb_doc_not_ready(callback: CallbackQuery):
    await callback.answer("Этот документ пока не загружен. Скоро появится!", show_alert=True)