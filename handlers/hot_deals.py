# handlers/hot_deals.py
"""
Хендлер подписки на горячие предложения по авиабилетам.

Типы подписок:
  1. «Горячие предложения» — пользователь задаёт категорию (морские курорты /
     городские / по миру / по России), примерный период, бюджет и количество
     пассажиров.  Бот присылает уведомление, когда находит рейс дешевле порога.

  2. «Дайджест» — раз в день или раз в неделю бот присылает подборку лучших
     предложений из города пользователя.
"""

import json
import time
import logging
from datetime import datetime, date
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from utils.redis_client import redis_client
from utils.cities_loader import get_iata, get_city_name

logger = logging.getLogger(__name__)
router = Router()


# ════════════════════════════════════════════════════════════════
# FSM — состояния для настройки горячей подписки
# ════════════════════════════════════════════════════════════════

class HotDealsSub(StatesGroup):
    choose_sub_type   = State()   # горячие vs дайджест
    choose_category   = State()   # морские / городские / мир / Россия
    choose_origin     = State()   # город вылета
    choose_months     = State()   # когда хотите лететь (месяц/квартал/«любой»)
    choose_budget     = State()   # макс. цена на человека
    choose_passengers = State()   # кол-во пассажиров
    choose_frequency  = State()   # ежедневно / еженедельно  (только для дайджеста)
    confirm           = State()   # подтверждение


# ════════════════════════════════════════════════════════════════
# Справочники
# ════════════════════════════════════════════════════════════════

CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",  ["AYT", "HRG", "SSH", "RHO", "DLM", "LCA", "TFS", "PMI", "CFU", "HER", "PFO", "AER", "SIP", "BUS"]),
    "city":   ("🏙️ Городские поездки", ["IST", "BCN", "CDG", "FCO", "AMS", "BER", "PRG", "BUD", "WAW", "VIE", "ATH", "HEL", "ARN", "OSL", "CPH"]),
    "world":  ("🌍 Путешествия по миру", ["DXB", "BKK", "SIN", "KUL", "HKT", "CMB", "NBO", "GRU", "JFK", "LAX", "YYZ", "ICN", "TYO", "PEK", "DEL"]),
    "russia": ("🇷🇺 По России",         ["AER", "LED", "KZN", "OVB", "SVX", "ROV", "UFA", "CEK", "KRR", "VOG", "MCX", "GRV", "KUF", "IKT", "VVO"]),
}

MONTHS_LABELS = {
    "1":  "Январь",  "2":  "Февраль", "3":  "Март",
    "4":  "Апрель",  "5":  "Май",     "6":  "Июнь",
    "7":  "Июль",    "8":  "Август",  "9":  "Сентябрь",
    "10": "Октябрь", "11": "Ноябрь",  "12": "Декабрь",
    "any": "Любой месяц",
}


# ════════════════════════════════════════════════════════════════
# Входная точка — кнопка «🔥 Горячие предложения»
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hot_deals_menu")
async def hot_deals_menu(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    logger.debug(f"[HotDeals] hot_deals_menu вызван, текущий FSM-стейт: {current_state}")

    # Если пользователь в середине поиска билетов — не сбрасываем его прогресс
    if current_state and not current_state.startswith("HotDealsSub"):
        await callback.answer(
            "⚠️ Сначала завершите или отмените текущий поиск билетов",
            show_alert=True
        )
        return

    await state.clear()
    user_id = callback.from_user.id
    logger.info(f"[HotDeals] Открыто меню горячих предложений user_id={user_id}")

    subs = await redis_client.get_hot_subs(user_id)
    logger.debug(f"[HotDeals] Найдено подписок для {user_id}: {len(subs) if subs else 0}")

    text = "🔥 <b>Горячие предложения</b>\n\nВыберите действие:"
    buttons = [
        [InlineKeyboardButton(text="➕ Новая подписка", callback_data="hd_new_sub")],
    ]
    if subs:
        buttons.append([InlineKeyboardButton(text=f"📋 Мои подписки ({len(subs)})", callback_data="hd_my_subs")])
    buttons.append([InlineKeyboardButton(text="↩️ Главное меню", callback_data="main_menu")])

    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 1 — тип подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_new_sub")
async def hd_step1_sub_type(callback: CallbackQuery, state: FSMContext):
    logger.info(f"[HotDeals] Шаг 1 — выбор типа подписки user_id={callback.from_user.id}")
    await state.set_state(HotDealsSub.choose_sub_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Горячие предложения",
                              callback_data="hd_type_hot")],
        [InlineKeyboardButton(text="📰 Дайджест (раз в день / неделю)",
                              callback_data="hd_type_digest")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="hot_deals_menu")],
    ])
    await callback.message.edit_text(
        "Выберите тип подписки:\n\n"
        "🔥 <b>Горячие предложения</b> — уведомление, как только появится рейс "
        "дешевле вашего бюджета по нужному направлению.\n\n"
        "📰 <b>Дайджест</b> — раз в день или раз в неделю получайте подборку "
        "лучших предложений из вашего города.",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 2а — категория направления (для горячих)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.in_({"hd_type_hot", "hd_type_digest"}))
async def hd_step2_category(callback: CallbackQuery, state: FSMContext):
    sub_type = "hot" if callback.data == "hd_type_hot" else "digest"
    logger.info(f"[HotDeals] Шаг 2 — тип={sub_type} user_id={callback.from_user.id}")
    await state.update_data(sub_type=sub_type)
    await state.set_state(HotDealsSub.choose_category)

    buttons = [[InlineKeyboardButton(text=label, callback_data=f"hd_cat_{key}")]
               for key, (label, _) in CATEGORIES.items()]
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="hd_new_sub")])

    await callback.message.edit_text(
        "Выберите <b>направление</b> (тематику путешествия):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 3 — город вылета
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_cat_"))
async def hd_step3_origin(callback: CallbackQuery, state: FSMContext):
    cat = callback.data.replace("hd_cat_", "")
    if cat not in CATEGORIES:
        await callback.answer("Неверная категория", show_alert=True)
        return
    logger.info(f"[HotDeals] Шаг 3 — категория={cat} user_id={callback.from_user.id}")
    await state.update_data(category=cat)
    await state.set_state(HotDealsSub.choose_origin)

    await callback.message.edit_text(
        "🛫 Введите <b>город вылета</b> (например: Москва, Санкт-Петербург, Казань):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="hd_new_sub")]
        ])
    )
    await callback.answer()


@router.message(HotDealsSub.choose_origin)
async def hd_step3_origin_text(message: Message, state: FSMContext):
    city = message.text.strip()
    logger.info(f"[HotDeals] Шаг 3 — ввод города='{city}' user_id={message.from_user.id}")
    iata = get_iata(city)
    if not iata:
        logger.warning(f"[HotDeals] Город не найден: '{city}'")
        await message.answer(
            f"❌ Город «{city}» не найден. Попробуйте ещё раз "
            f"(например: <b>Москва</b>, <b>Новосибирск</b>).",
            parse_mode="HTML"
        )
        return
    logger.info(f"[HotDeals] Город '{city}' → IATA={iata}")
    await state.update_data(origin_iata=iata, origin_name=get_city_name(iata) or city)
    await state.set_state(HotDealsSub.choose_months)
    await _ask_months(message)


async def _ask_months(target):
    """Отправить клавиатуру выбора месяца(ев)."""
    cur_month = date.today().month
    cur_year = date.today().year
    buttons = []
    row = []
    for i in range(12):
        m = (cur_month - 1 + i) % 12 + 1
        y = cur_year + ((cur_month - 1 + i) // 12)
        label = f"{MONTHS_LABELS[str(m)]} {y}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"hd_month_{m}_{y}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🗓️ Любой месяц", callback_data="hd_month_any_any")])

    send = target.answer if isinstance(target, Message) else target.message.edit_text
    await send(
        "📅 Выберите <b>примерный месяц</b> вылета:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# ════════════════════════════════════════════════════════════════
# ШАГ 4 — месяц вылета
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_month_"))
async def hd_step4_month(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")  # hd_month_<m>_<y>
    month_val = parts[2]
    year_val = parts[3]
    if month_val == "any":
        await state.update_data(travel_month=None, travel_year=None)
    else:
        await state.update_data(travel_month=int(month_val), travel_year=int(year_val))
    await state.set_state(HotDealsSub.choose_budget)

    await callback.message.edit_text(
        "💰 Укажите <b>максимальную цену на человека</b> (в рублях).\n\n"
        "Например: <code>15000</code> — буду присылать только предложения дешевле этой суммы.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="5 000 ₽",  callback_data="hd_budget_5000")],
            [InlineKeyboardButton(text="10 000 ₽", callback_data="hd_budget_10000")],
            [InlineKeyboardButton(text="15 000 ₽", callback_data="hd_budget_15000")],
            [InlineKeyboardButton(text="20 000 ₽", callback_data="hd_budget_20000")],
            [InlineKeyboardButton(text="30 000 ₽", callback_data="hd_budget_30000")],
            [InlineKeyboardButton(text="Без ограничений", callback_data="hd_budget_0")],
        ])
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 5 — бюджет (кнопка или текст)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_budget_"))
async def hd_step5_budget_btn(callback: CallbackQuery, state: FSMContext):
    budget = int(callback.data.replace("hd_budget_", ""))
    await state.update_data(max_price=budget)
    await state.set_state(HotDealsSub.choose_passengers)
    await _ask_passengers(callback)
    await callback.answer()


@router.message(HotDealsSub.choose_budget)
async def hd_step5_budget_text(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ", "").replace("₽", "").replace(",", "")
    if not raw.isdigit():
        await message.answer("Введите сумму числом, например: <code>12000</code>", parse_mode="HTML")
        return
    await state.update_data(max_price=int(raw))
    await state.set_state(HotDealsSub.choose_passengers)
    await _ask_passengers(message)


async def _ask_passengers(target):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 пассажир",  callback_data="hd_pax_1"),
         InlineKeyboardButton(text="2 пассажира", callback_data="hd_pax_2")],
        [InlineKeyboardButton(text="3 пассажира", callback_data="hd_pax_3"),
         InlineKeyboardButton(text="4 пассажира", callback_data="hd_pax_4")],
    ])
    send = target.answer if isinstance(target, Message) else target.message.edit_text
    await send("👥 Сколько <b>пассажиров</b>?", parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════════════
# ШАГ 6 — пассажиры
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_pax_"))
async def hd_step6_pax(callback: CallbackQuery, state: FSMContext):
    pax = int(callback.data.replace("hd_pax_", ""))
    await state.update_data(passengers=pax)

    data = await state.get_data()
    if data.get("sub_type") == "digest":
        await state.set_state(HotDealsSub.choose_frequency)
        await _ask_frequency(callback)
    else:
        await state.set_state(HotDealsSub.confirm)
        await _show_confirm(callback, data | {"passengers": pax})
    await callback.answer()


async def _ask_frequency(target):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Раз в день",   callback_data="hd_freq_daily")],
        [InlineKeyboardButton(text="📆 Раз в неделю", callback_data="hd_freq_weekly")],
    ])
    send = target.message.edit_text if isinstance(target, CallbackQuery) else target.answer
    await send("⏰ Как часто присылать <b>дайджест</b>?", parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════════════
# ШАГ 7 — частота (только для дайджеста)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_freq_"))
async def hd_step7_freq(callback: CallbackQuery, state: FSMContext):
    freq = callback.data.replace("hd_freq_", "")  # "daily" / "weekly"
    await state.update_data(frequency=freq)
    data = await state.get_data()
    await state.set_state(HotDealsSub.confirm)
    await _show_confirm(callback, data)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Подтверждение подписки
# ════════════════════════════════════════════════════════════════

async def _show_confirm(target, data: dict):
    cat_label, _ = CATEGORIES.get(data.get("category", ""), ("—", []))
    origin_name = data.get("origin_name", data.get("origin_iata", "—"))
    sub_type = data.get("sub_type", "hot")
    max_price = data.get("max_price", 0)
    passengers = data.get("passengers", 1)

    if data.get("travel_month"):
        month_str = f"{MONTHS_LABELS.get(str(data['travel_month']), '—')} {data.get('travel_year', '')}"
    else:
        month_str = "Любой"

    price_str = f"до {max_price:,} ₽".replace(",", " ") if max_price else "Без ограничений"

    if sub_type == "digest":
        freq_map = {"daily": "раз в день", "weekly": "раз в неделю"}
        freq_str = freq_map.get(data.get("frequency", "daily"), "раз в день")
        type_str = f"📰 Дайджест ({freq_str})"
    else:
        type_str = "🔥 Горячие предложения"

    text = (
        f"✅ <b>Проверьте настройки подписки:</b>\n\n"
        f"Тип: {type_str}\n"
        f"Направление: {cat_label}\n"
        f"Откуда: {origin_name}\n"
        f"Период: {month_str}\n"
        f"Бюджет: {price_str} / чел.\n"
        f"Пассажиры: {passengers} чел."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подписаться", callback_data="hd_save")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="hd_new_sub")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="hot_deals_menu")],
    ])
    send = target.message.edit_text if isinstance(target, CallbackQuery) else target.answer
    await send(text, parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════════════
# Сохранение подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_save")
async def hd_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    logger.info(f"[HotDeals] Сохранение подписки user_id={user_id}, данные={data}")

    sub = {
        "user_id": user_id,
        "sub_type": data.get("sub_type", "hot"),       # hot | digest
        "category": data.get("category", "world"),
        "origin_iata": data.get("origin_iata", ""),
        "origin_name": data.get("origin_name", ""),
        "travel_month": data.get("travel_month"),
        "travel_year": data.get("travel_year"),
        "max_price": data.get("max_price", 0),
        "passengers": data.get("passengers", 1),
        "frequency": data.get("frequency", "daily"),    # только для digest
        "created_at": int(time.time()),
        "last_notified": 0,
    }

    sub_id = await redis_client.save_hot_sub(user_id, sub)
    await state.clear()

    await callback.message.edit_text(
        "🎉 <b>Подписка оформлена!</b>\n\n"
        "Как только найду подходящий рейс — сразу пришлю уведомление.\n\n"
        "Управлять подписками: кнопка «📋 Мои подписки» в разделе "
        "«🔥 Горячие предложения».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои подписки", callback_data="hd_my_subs")],
            [InlineKeyboardButton(text="↩️ Главное меню", callback_data="main_menu")],
        ])
    )
    await callback.answer("✅ Подписка сохранена!")


# ════════════════════════════════════════════════════════════════
# Список подписок пользователя
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_my_subs")
async def hd_my_subs(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    subs = await redis_client.get_hot_subs(user_id)

    if not subs:
        await callback.message.edit_text(
            "У вас нет активных подписок на горячие предложения.\n"
            "Нажмите «➕ Новая подписка», чтобы создать первую!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Новая подписка", callback_data="hd_new_sub")],
                [InlineKeyboardButton(text="↩️ Назад", callback_data="hot_deals_menu")],
            ])
        )
        await callback.answer()
        return

    text = "📋 <b>Ваши подписки на горячие предложения:</b>\n"
    buttons = []
    for idx, (sub_id, sub) in enumerate(subs.items(), 1):
        cat_label, _ = CATEGORIES.get(sub.get("category", ""), ("—", []))
        sub_type_str = "🔥" if sub.get("sub_type") == "hot" else "📰"
        price_str = f"≤{sub['max_price']:,} ₽".replace(",", " ") if sub.get("max_price") else "любая"

        if sub.get("travel_month"):
            month_str = f"{MONTHS_LABELS.get(str(sub['travel_month']), '?')} {sub.get('travel_year', '')}"
        else:
            month_str = "любой период"

        text += (
            f"\n{idx}. {sub_type_str} {cat_label}\n"
            f"   🛫 {sub.get('origin_name', sub.get('origin_iata', '?'))} · {month_str} · {price_str} · "
            f"{sub.get('passengers', 1)} чел.\n"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Удалить #{idx}",
                callback_data=f"hd_del_{sub_id}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Новая подписка", callback_data="hd_new_sub")])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="hot_deals_menu")])

    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Удаление подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_del_"))
async def hd_delete_sub(callback: CallbackQuery):
    sub_id = callback.data.replace("hd_del_", "")
    user_id = callback.from_user.id
    await redis_client.delete_hot_sub(user_id, sub_id)
    await callback.answer("✅ Подписка удалена")
    # Обновляем список
    await hd_my_subs(callback)