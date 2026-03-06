# handlers/subscriptions.py
"""
Единый центр управления подписками.

Три типа:
  1. 🔥 Горячие предложения — уведомление когда появится рейс дешевле бюджета
  2. 📰 Дайджест           — ежедневная / еженедельная подборка
  3. 📉 Слежение за ценой  — уведомление когда цена на конкретный рейс упала

Архитектура:
  nav_subs (start.py) → subs_menu (этот файл) → три раздела
  Горячие/Дайджест → делегируем в hot_deals.py (там уже есть весь FSM)
  Слежение за ценой → новые хендлеры здесь (просмотр + удаление)
"""

import logging
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery, Message,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext

from utils.redis_client import redis_client
from utils.cities import IATA_TO_CITY
from utils.smart_reminder import cancel_inactivity
from handlers.flight_constants import AIRLINE_NAMES

logger = logging.getLogger(__name__)
router = Router()

# ════════════════════════════════════════════════════════════════
# Вспомогательные функции
# ════════════════════════════════════════════════════════════════

def _threshold_label(threshold: int) -> str:
    return {
        0:    "любом изменении",
        100:  "изменении на сотни ₽",
        1000: "изменении на тысячи ₽",
    }.get(threshold, f"изменении ≥ {threshold} ₽")


def _format_duration(minutes: int) -> str:
    if not minutes:
        return ""
    h, m = divmod(minutes, 60)
    return f"{h}ч {m}м" if h else f"{m}м"


def _watch_key_from_data(w: dict) -> str:
    """Восстанавливаем ключ Redis из полей (для старых записей без watch_key)."""
    if w.get("watch_key"):
        return w["watch_key"]
    from utils.redis_client import redis_client as rc
    origin = w.get("origin") or "None"
    dest   = w.get("dest")   or "None"
    key = f"{rc.prefix}watch:{w['user_id']}:{origin}:{dest}:{w['depart_date']}"
    if w.get("return_date"):
        key += f":{w['return_date']}"
    return key


# ════════════════════════════════════════════════════════════════
# Главный экран Подписок
# ════════════════════════════════════════════════════════════════

async def build_subs_menu_kb(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """
    Строит главный экран «📋 Подписки».
    Показывает счётчики по каждому типу и три кнопки входа.
    """
    from handlers.billing import get_user_plan, PLANS

    hot_subs      = await redis_client.get_hot_subs(user_id)
    price_watches = await redis_client.get_user_watches(user_id)

    hot_count    = len(hot_subs)
    digest_count = sum(1 for s in hot_subs.values() if s.get("sub_type") == "digest")
    hot_only     = hot_count - digest_count
    watch_count  = len(price_watches)

    # Тариф пользователя для строки-подсказки
    plan_data = await get_user_plan(user_id)
    plan_key  = plan_data.get("plan", "free")
    cfg       = PLANS[plan_key]
    hot_lim   = "∞" if cfg["hot_limit"]    == 0 else str(cfg["hot_limit"])
    dig_lim   = "∞" if cfg["digest_limit"] == 0 else str(cfg["digest_limit"])
    plan_line = f"\n<i>Тариф {cfg['label']}: горячие {hot_only}/{hot_lim} · дайджест {digest_count}/{dig_lim}</i>"

    total   = hot_count + watch_count
    summary = f"Всего активных: {total}" if total else "Активных подписок нет"
    text = (
        f"<b>Подписки</b>\n<i>{summary}</i>{plan_line}\n\n"
        "<b>Горячие предложения</b> — уведомлю, когда появится рейс дешевле бюджета.\n\n"
        "<b>Дайджест</b> — ежедневная или еженедельная подборка лучших цен.\n\n"
        "<b>Слежение за ценой</b> — слежу за конкретным рейсом и сообщу при изменении цены."
    )

    def _cnt(n): return f" ({n})" if n else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🔥 Горячие предложения{_cnt(hot_only)}",
            callback_data="subs_section_hot"
        )],
        [InlineKeyboardButton(
            text=f"📰 Дайджест{_cnt(digest_count)}",
            callback_data="subs_section_digest"
        )],
        [InlineKeyboardButton(
            text=f"📉 Слежение за ценой{_cnt(watch_count)}",
            callback_data="subs_section_watches"
        )],
        [InlineKeyboardButton(text=f"💳 Тарифы  [{cfg['badge'] or cfg['label']}]", callback_data="billing_menu")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    return text, kb


@router.callback_query(F.data == "subs_menu")
async def cb_subs_menu(callback: CallbackQuery, state: FSMContext):
    """Открыть главный экран подписок (из inline-кнопки)."""
    cancel_inactivity(callback.message.chat.id)
    text, kb = await build_subs_menu_kb(callback.from_user.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Раздел: Горячие предложения
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "subs_section_hot")
async def cb_section_hot(callback: CallbackQuery, state: FSMContext):
    """Показывает горячие подписки (sub_type == 'hot')."""
    cancel_inactivity(callback.message.chat.id)
    user_id = callback.from_user.id
    all_subs = await redis_client.get_hot_subs(user_id)
    hot_subs = {k: v for k, v in all_subs.items() if v.get("sub_type") == "hot"}

    if not hot_subs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать подписку", callback_data="hd_type_hot")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="subs_menu")],
        ])
        await callback.message.edit_text(
            "<b>Горячие предложения</b>\n\n"
            "Горячих подписок пока нет.\n\n"
            "<i>Укажи направление и бюджет — напишу, как только появится выгодный рейс.</i>",
            parse_mode="HTML", reply_markup=kb
        )
    else:
        from handlers.hot_deals import hd_my_subs_text_kb
        text, kb = await hd_my_subs_text_kb(user_id, hot_subs)
        # Добавляем заголовок типа подписки
        text = "🔥 <b>Горячие предложения</b>\n\n" + text
        new_buttons = list(kb.inline_keyboard)
        # Одна кнопка "Добавить ещё" (без дубля "Добавить подписку")
        new_buttons.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="hd_type_hot")])
        # Порядок: сначала "К подпискам", потом "В начало"
        new_buttons.append([InlineKeyboardButton(text="↩️ К подпискам", callback_data="subs_menu")])
        new_buttons.append([InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")])
        new_kb = InlineKeyboardMarkup(inline_keyboard=new_buttons)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=new_kb)

    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Раздел: Дайджест
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "subs_section_digest")
async def cb_section_digest(callback: CallbackQuery, state: FSMContext):
    """Показывает дайджест-подписки (sub_type == 'digest')."""
    cancel_inactivity(callback.message.chat.id)
    user_id = callback.from_user.id
    all_subs = await redis_client.get_hot_subs(user_id)
    digest_subs = {k: v for k, v in all_subs.items() if v.get("sub_type") == "digest"}

    if not digest_subs:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать дайджест", callback_data="hd_type_digest")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="subs_menu")],
        ])
        await callback.message.edit_text(
            "<b>Дайджест</b>\n\n"
            "Дайджест-подписок пока нет.\n\n"
            "<i>Настрой направление — буду присылать лучшие предложения каждый день или неделю.</i>",
            parse_mode="HTML", reply_markup=kb
        )
    else:
        from handlers.hot_deals import hd_my_subs_text_kb
        text, kb = await hd_my_subs_text_kb(user_id, digest_subs)
        text = "📰 <b>Дайджест</b>\n\n" + text
        new_buttons = list(kb.inline_keyboard)
        new_buttons.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="hd_type_digest")])
        new_buttons.append([InlineKeyboardButton(text="↩️ К подпискам", callback_data="subs_menu")])
        new_buttons.append([InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")])
        new_kb = InlineKeyboardMarkup(inline_keyboard=new_buttons)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=new_kb)

    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Раздел: Слежение за ценой
# ════════════════════════════════════════════════════════════════

def _build_watch_card(w: dict, idx: int) -> str:
    """Строит текстовую карточку одного price watch."""
    origin = w.get("origin") or ""
    dest   = w.get("dest")   or ""
    orig_name = "Везде" if not origin else IATA_TO_CITY.get(origin, origin)
    dest_name = "Везде" if not dest   else IATA_TO_CITY.get(dest, dest)

    depart  = w.get("display_depart") or w.get("depart_date", "—")
    ret     = w.get("display_return") or w.get("return_date")
    dur     = _format_duration(w.get("duration", 0))
    trf     = w.get("transfers")
    airline = w.get("airline", "")
    fnum    = w.get("flight_number", "")
    price   = w.get("current_price", "?")
    thr     = w.get("threshold", 0)

    if trf == 0:
        trf_str = "Прямой рейс"
    elif trf == 1:
        trf_str = "1 пересадка"
    elif trf:
        trf_str = f"{trf} пересадки"
    else:
        trf_str = ""

    airline_str = ""
    if airline or fnum:
        a_disp = AIRLINE_NAMES.get(airline, airline)
        airline_str = f"{a_disp} {fnum}".strip() if fnum else a_disp

    lines = [f"<b>{idx}. {orig_name} → {dest_name}</b>"]
    lines.append(f"Вылет туда: <b>{depart}</b>")
    if ret:
        lines.append(f"Вылет обратно: <b>{ret}</b>")
    if dur:
        lines.append(f"Продолжительность: {dur}")
    if trf_str:
        lines.append(trf_str)
    if airline_str:
        lines.append(f"Авиакомпания: {airline_str}")
    lines.append(f"Цена сейчас: <b>{price} ₽</b>")
    lines.append(f"<i>Уведомлять: {_threshold_label(thr)}</i>")

    return "\n".join(lines)


@router.callback_query(F.data == "subs_section_watches")
async def cb_section_watches(callback: CallbackQuery, state: FSMContext):
    """Показывает все price-watch подписки пользователя."""
    cancel_inactivity(callback.message.chat.id)
    user_id = callback.from_user.id
    watches = await redis_client.get_user_watches(user_id)

    if not watches:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ К подпискам", callback_data="subs_menu")],
        ])
        await callback.message.edit_text(
            "<b>Слежение за ценой</b>\n\n"
            "Активных отслеживаний нет.\n\n"
            "<i>Найди рейс через поиск и нажми <b>«Следить за ценой»</b> — добавлю сюда.</i>",
            parse_mode="HTML", reply_markup=kb
        )
        await callback.answer()
        return

    # Нормализуем ключи (для старых записей без watch_key)
    for w in watches:
        if not w.get("watch_key"):
            w["watch_key"] = _watch_key_from_data(w)

    blocks = []
    del_buttons = []
    for i, w in enumerate(watches, 1):
        blocks.append(_build_watch_card(w, i))
        orig = w.get("origin") or ""
        dest = w.get("dest")   or ""
        orig_name = "Везде" if not orig else IATA_TO_CITY.get(orig, orig)
        dest_name = "Везде" if not dest  else IATA_TO_CITY.get(dest, dest)
        del_buttons.append([InlineKeyboardButton(
            text=f"Удалить: {orig_name} → {dest_name}",
            callback_data=f"unwatch_{w['watch_key']}"
        )])

    divider = "\n\n" + "─" * 20 + "\n\n"
    count_str = f"1 рейс" if len(watches) == 1 else f"{len(watches)} рейса" if len(watches) < 5 else f"{len(watches)} рейсов"
    text = (
        f"<b>Слежение за ценой</b> · <i>{count_str}</i>\n\n"
        + divider.join(blocks)
    )

    del_buttons.append([InlineKeyboardButton(text="↩️ К подпискам", callback_data="subs_menu")])
    del_buttons.append([InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=del_buttons)

    # Telegram ограничивает длину сообщения — если слишком длинное, отправляем новым
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)

    await callback.answer()