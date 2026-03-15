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
from typing import Optional
from utils.flight_utils import _format_duration
from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
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

async def build_subs_menu_kb(user_id: int, username: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
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
    plan_data = await get_user_plan(user_id, username)
    plan_key  = plan_data.get("plan", "free")
    cfg       = PLANS.get(plan_key) or PLANS["free"]  # fallback на free если неизвестный plan
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
        [InlineKeyboardButton(text="🕐 История поисков", callback_data="subs_history")],
        [InlineKeyboardButton(text=f"💳 Тарифы  [{cfg.get('emoji', '') or cfg['label']}]", callback_data="billing_menu")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    return text, kb


@router.callback_query(F.data == "subs_menu")
async def cb_subs_menu(callback: CallbackQuery, state: FSMContext):
    """Открыть главный экран подписок (из inline-кнопки)."""
    cancel_inactivity(callback.message.chat.id)
    text, kb = await build_subs_menu_kb(callback.from_user.id, callback.from_user.username)
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
        from handlers.billing import can_add_sub
        text, kb = await hd_my_subs_text_kb(user_id, hot_subs)
        text = "🔥 <b>Горячие предложения</b>\n\n" + text
        new_buttons = list(kb.inline_keyboard)
        # Умная кнопка: лимит → тарифы, есть место → добавить
        ok, _ = await can_add_sub(user_id, "hot", callback.from_user.username)
        if ok:
            new_buttons.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="hd_type_hot")])
        else:
            new_buttons.append([InlineKeyboardButton(text="💳 Увеличить лимит", callback_data="billing_menu")])
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
        from handlers.billing import can_add_sub
        text, kb = await hd_my_subs_text_kb(user_id, digest_subs)
        text = "📰 <b>Дайджест</b>\n\n" + text
        new_buttons = list(kb.inline_keyboard)
        ok, _ = await can_add_sub(user_id, "digest", callback.from_user.username)
        if ok:
            new_buttons.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="hd_type_digest")])
        else:
            new_buttons.append([InlineKeyboardButton(text="💳 Увеличить лимит", callback_data="billing_menu")])
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

# ════════════════════════════════════════════════════════════════
# История поисков
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "subs_history")
async def cb_search_history(callback: CallbackQuery, state: FSMContext):
    """Показываем последние 5 поисков с кнопками повтора."""
    from datetime import datetime
    user_id = callback.from_user.id
    history = await redis_client.get_search_history(user_id)

    if not history:
        try:
            await callback.message.edit_text(
                "🕐 <b>История поисков</b>\n\nПоисков пока нет. "
                "Попробуй найти билеты — они появятся здесь.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")],
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="subs_menu")],
                ])
            )
        except Exception:
            pass
        await callback.answer()
        return

    lines = ["🕐 <b>История поисков</b>\n"]
    buttons = []

    for i, entry in enumerate(history, 1):
        origin = entry.get("origin_name") or entry.get("origin_iata", "?")
        dest   = entry.get("dest_name")   or entry.get("dest_iata", "?")
        date   = entry.get("depart_date", "")
        ret    = entry.get("return_date", "")
        ts     = entry.get("ts", 0)

        date_str = f" · {date}" if date else ""
        ret_str  = f"→{ret}" if ret else ""
        ago_str  = ""
        if ts:
            delta = int(datetime.now().timestamp()) - ts
            if delta < 3600:
                ago_str = f" <i>({delta // 60} мин назад)</i>"
            elif delta < 86400:
                ago_str = f" <i>({delta // 3600} ч назад)</i>"
            else:
                ago_str = f" <i>({delta // 86400} дн назад)</i>"

        lines.append(f"{i}. <b>{origin} → {dest}</b>{date_str}{ago_str}")

        # Кнопка повтора
        btn_text = f"🔁 {origin} → {dest}{date_str}"
        # Передаём данные через callback_data (компактно)
        o_iata = entry.get("origin_iata", "")
        d_iata = entry.get("dest_iata", "")
        d1     = entry.get("depart_date", "")
        d2     = entry.get("return_date", "")
        pax    = entry.get("pax", "1")
        cb_data = f"hist_repeat:{o_iata}:{d_iata}:{d1}:{d2}:{pax}"
        if len(cb_data) <= 64:
            buttons.append([InlineKeyboardButton(text=btn_text[:40], callback_data=cb_data)])

    buttons.append([InlineKeyboardButton(text="↩️ Назад к подпискам", callback_data="subs_menu")])

    try:
        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    except Exception:
        await callback.message.answer(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    await callback.answer()


@router.callback_query(F.data.startswith("hist_repeat:"))
async def cb_history_repeat(callback: CallbackQuery, state: FSMContext):
    """Повтор поиска из истории — восстанавливаем FSM и запускаем поиск."""
    from handlers.flight_fsm import FlightSearch
    from utils.cities_loader import get_city_name

    parts = callback.data.split(":")
    # hist_repeat:origin_iata:dest_iata:depart_date:return_date:pax
    if len(parts) < 6:
        await callback.answer("Не удалось повторить поиск", show_alert=True)
        return

    _, o_iata, d_iata, d1, d2, pax = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]

    o_name = get_city_name(o_iata) or o_iata
    d_name = get_city_name(d_iata) or d_iata

    await state.set_state(FlightSearch.confirm)
    await state.update_data(
        origin=o_name, origin_iata=o_iata, origin_name=o_name,
        dest=d_name,   dest_iata=d_iata,   dest_name=d_name,
        depart_date=d1, return_date=d2 or None,
        passenger_code=pax,
        flight_type="all",
    )

    await callback.answer()
    await callback.message.answer(
        f"🔁 Повторяю поиск: <b>{o_name} → {d_name}</b>\n"
        f"📅 {d1}" + (f" — {d2}" if d2 else ""),
        parse_mode="HTML"
    )

    # Запускаем поиск
    from handlers.search_results import confirm_search
    await confirm_search(callback, state)