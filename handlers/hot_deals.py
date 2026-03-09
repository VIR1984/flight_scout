# handlers/hot_deals.py
"""
Хендлер подписки на горячие предложения.

Типы подписок:
  1. «Горячие предложения» — уведомление, когда появится рейс дешевле бюджета.
  2. «Дайджест» — ежедневная / еженедельная подборка лучших предложений.

Флоу:
  Тип → Категория → [Подкатегория назначения] → Города вылета (несколько)
       → Месяцы (несколько) → Бюджет → Пассажиры → [Частота] → Подтверждение
"""

import time
import logging
from datetime import date
from typing import Optional

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from utils.redis_client import redis_client
from utils.cities_loader import get_iata, get_city_name
from handlers.billing import can_add_sub, show_paywall, get_user_plan, PLANS

logger = logging.getLogger(__name__)
router = Router()

BACK_TO_MAIN = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
])


# ════════════════════════════════════════════════════════════════
# FSM
# ════════════════════════════════════════════════════════════════

class HotDealsSub(StatesGroup):
    choose_sub_type    = State()
    choose_category    = State()
    choose_dest_preset = State()   # подкатегория назначения (Турция / Египет / ...)
    choose_dest_custom = State()   # ввод своего направления текстом
    choose_origins     = State()   # мультивыбор городов вылета
    choose_months      = State()   # мультивыбор месяцев
    choose_budget      = State()   # ввод бюджета числом
    choose_passengers  = State()
    choose_frequency   = State()   # только для дайджеста
    confirm            = State()


# ════════════════════════════════════════════════════════════════
# Справочники
# ════════════════════════════════════════════════════════════════

CATEGORIES = {
    "sea":    ("🏖️ Морские курорты",    ["AYT","HRG","SSH","RHO","DLM","LCA","TFS","PMI","CFU","HER","PFO","AER","SIP","BUS"]),
    "world":  ("🌍 Путешествия по миру", ["DXB","BKK","SIN","KUL","HKT","CMB","NBO","GRU","JFK","LAX","YYZ","ICN","TYO","PEK","DEL"]),
    "russia": ("🇷🇺 По России",          [
        # Юг и курорты
        "AER","KRR","MRV","NAL","MCX","GRV","ESL","ASF",
        # Центр и Поволжье
        "LED","KZN","KUF","VOG","PEE","ULY","GOJ","RTW",
        # Урал и Сибирь
        "SVX","UFA","CEK","OVB","KJA","OMS","TJM","TOF",
        # Дальний Восток
        "VVO","IKT","UUS","PKC","GDX","UUD",
        # Северо-Запад
        "KGD","MMK","ARH","PES",
        # Уже были
        "ROV",
    ]),
    "custom": ("🔍 Свой маршрут",   []),  # пользователь вводит сам
}

# Подкатегории направлений — быстрые варианты
# None в iata_list → «Свой вариант» (пользователь вводит текстом)
CATEGORY_PRESETS = {
    "sea": [
        ("🌊 Сочи",          ["AER"]),
        ("🇹🇷 Турция",       ["AYT","DLM","ADB","BJV"]),
        ("🇪🇬 Египет",       ["HRG","SSH"]),
        ("🇹🇭 Таиланд",      ["BKK","HKT","USM"]),
        ("✏️ Свой вариант",  None),
    ],
    "world": [
        ("🇨🇳 Китай",        ["PEK","PVG","CAN"]),
        ("🇯🇵 Япония",       ["TYO","OSA","CTS"]),
        ("🌍 Европа",        ["IST","BCN","CDG","FCO","AMS","BER","PRG"]),
        ("🇺🇸 США",          ["JFK","LAX","ORD","MIA"]),
        ("✏️ Свой вариант",  None),
    ],
    "russia": [
        ("🏙️ Москва",               ["SVO","DME","VKO"]),
        ("🌊 Сочи / Юг",            ["AER","KRR","MRV","NAL","ASF"]),
        ("🏛️ Санкт-Петербург",      ["LED"]),
        ("🕌 Казань / Поволжье",    ["KZN","KUF","GOJ","ULY"]),
        ("🌊 Калининград",           ["KGD"]),
        ("🏔️ Урал / Сибирь",        ["SVX","UFA","CEK","OVB","KJA","OMS"]),
        ("🌏 Дальний Восток",        ["VVO","IKT","UUS","PKC"]),
        ("✏️ Свой вариант",         None),
    ],
}

MONTHS_LABELS = {
    "1":"Январь",  "2":"Февраль", "3":"Март",    "4":"Апрель",
    "5":"Май",     "6":"Июнь",    "7":"Июль",    "8":"Август",
    "9":"Сентябрь","10":"Октябрь","11":"Ноябрь", "12":"Декабрь",
}


# ════════════════════════════════════════════════════════════════
# Вспомогательные рендеры
# ════════════════════════════════════════════════════════════════

async def _ask_origins(target, state: FSMContext, edit: bool = False):
    """
    Шаг «Город(а) вылета».
    Free-тариф: только 1 город. Plus/Premium: несколько.
    """
    data    = await state.get_data()
    origins = data.get("origins", [])
    user_id = data.get("user_id_fsm")

    # Определяем разрешён ли мультигород для этого пользователя
    multi_allowed = True
    if user_id:
        plan_data     = await get_user_plan(user_id)
        plan_cfg      = PLANS.get(plan_data.get("plan", "free")) or PLANS["free"]
        multi_allowed = plan_cfg.get("multi_origin", False)

    if origins:
        names = ", ".join(o["name"] for o in origins)
        if multi_allowed:
            text = (
                f"🛫 <b>Города вылета</b>\n\n"
                f"Добавлено: <b>{names}</b>\n\n"
                f"Допиши ещё города через запятую или нажми «Готово».\n"
                f"Чтобы убрать город — нажми ❌ рядом с ним."
            )
        else:
            text = (
                f"🛫 <b>Город вылета</b>\n\n"
                f"Выбран: <b>{names}</b>\n\n"
                
            )
    else:
        if multi_allowed:
            text = (
                "Введи <b>город(а) вылета</b>.\n\n"
                "Можно сразу несколько — через запятую или пробел:\n"
                "<i>Москва, Казань, Екатеринбург</i>\n\n"
                "Бот будет следить за ценами из каждого города."
            )
        else:
            text = (
                "Введи <b>город вылета</b>.\n\n"
                "<i>На бесплатном тарифе доступен только 1 город.\n"
                "⚡️ Плюс и 💎 Премиум открывают мультигород.</i>"
            )

    buttons = []
    if origins:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Готово ({len(origins)} {_city_word(len(origins))})",
            callback_data="hd_origins_done"
        )])
        for o in origins:
            buttons.append([InlineKeyboardButton(
                text=f"❌ {o['name']}",
                callback_data=f"hd_origin_del_{o['iata']}"
            )])
    buttons.append([InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_origins_back")])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit and hasattr(target, "edit_text"):
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        msg = target if isinstance(target, Message) else target.message
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


def _city_word(n: int) -> str:
    """Склонение слова 'город': 1 город, 2 города, 5 городов."""
    if 11 <= n % 100 <= 19:
        return "городов"
    r = n % 10
    if r == 1: return "город"
    if 2 <= r <= 4: return "города"
    return "городов"


async def _ask_months(target, selected: list, multi_allowed: bool = True):
    """Выбор месяца вылета. Free: только один месяц. Plus/Premium: мультиселект."""
    cur_month = date.today().month
    cur_year  = date.today().year
    buttons   = []
    row       = []

    for i in range(12):
        m   = (cur_month - 1 + i) % 12 + 1
        y   = cur_year + ((cur_month - 1 + i) // 12)
        key = f"{m}_{y}"
        lbl = f"{MONTHS_LABELS[str(m)]} {y}"
        if key in selected:
            lbl = "✅ " + lbl
        row.append(InlineKeyboardButton(text=lbl, callback_data=f"hd_month_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="🗓️ Любой месяц", callback_data="hd_month_any_any")])
    if multi_allowed and selected:
        buttons.append([InlineKeyboardButton(text="✅ Готово", callback_data="hd_months_done")])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

    if multi_allowed:
        text = "Выбери <b>месяц вылета</b>. Можно выбрать несколько."
        if selected:
            labels = [MONTHS_LABELS.get(k.split("_")[0], k) for k in selected]
            text += f"\n\n<i>Выбрано: {', '.join(labels)}</i>"
    else:
        text = (
            "Выбери <b>месяц вылета</b>.\n\n"
            "<i>На бесплатном тарифе доступен только 1 месяц.\n"
            "⚡️ Плюс и 💎 Премиум открывают мультивыбор месяцев.</i>"
        )

    send = target.answer if isinstance(target, Message) else target.message.edit_text
    await send(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


async def _ask_budget(target):
    """Ввод бюджета: только ручной ввод числом."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    text = (
        "Укажи <b>максимальную цену на человека</b> (в рублях).\n\n"
        "Напиши сумму числом — или <b>0</b> для поиска без ограничений.\n"
        "<i>Пример: 12000</i>"
    )
    send = target.answer if isinstance(target, Message) else target.message.edit_text
    await send(text, parse_mode="HTML", reply_markup=kb)


async def _ask_passengers(target):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1", callback_data="hd_pax_1"),
         InlineKeyboardButton(text="2", callback_data="hd_pax_2"),
         InlineKeyboardButton(text="3", callback_data="hd_pax_3"),
         InlineKeyboardButton(text="4", callback_data="hd_pax_4")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    send = target.answer if isinstance(target, Message) else target.message.edit_text
    await send("Сколько <b>пассажиров</b>?", parse_mode="HTML", reply_markup=kb)


async def _ask_frequency(target):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Раз в день",   callback_data="hd_freq_daily")],
        [InlineKeyboardButton(text="📆 Раз в неделю", callback_data="hd_freq_weekly")],
        [InlineKeyboardButton(text="↩️ В начало",     callback_data="main_menu")],
    ])
    send = target.message.edit_text if isinstance(target, CallbackQuery) else target.answer
    await send("Как часто присылать <b>дайджест</b>?", parse_mode="HTML", reply_markup=kb)


async def _show_confirm(target, data: dict):
    cat_label, _ = CATEGORIES.get(data.get("category", ""), ("—", []))
    sub_type      = data.get("sub_type", "hot")
    max_price     = data.get("max_price", 0)
    passengers    = data.get("passengers", 1)
    preset_name   = data.get("dest_preset_name", "")

    # Города вылета
    origins = data.get("origins", [])
    if origins:
        origins_str = ", ".join(o["name"] for o in origins)
    else:
        origins_str = data.get("origin_name", data.get("origin_iata", "—"))

    # Направление
    dest_str = preset_name if preset_name and preset_name != "свой вариант" else cat_label

    # Месяцы
    travel_months = data.get("travel_months", [])
    if travel_months:
        labels = []
        for mk in travel_months:
            m_str, y_str = mk.split("_")
            labels.append(f"{MONTHS_LABELS.get(m_str, m_str)} {y_str}")
        month_str = ", ".join(labels)
    elif data.get("travel_month"):
        month_str = f"{MONTHS_LABELS.get(str(data['travel_month']), '—')} {data.get('travel_year', '')}"
    else:
        month_str = "Любой"

    price_str = f"до {max_price:,} ₽".replace(",", " ") if max_price else "Без ограничений"

    if sub_type == "digest":
        freq_map = {"daily": "раз в день", "weekly": "раз в неделю"}
        type_str = f"📰 Дайджест ({freq_map.get(data.get('frequency', 'daily'), 'раз в день')})"
    else:
        type_str = "🔥 Горячие предложения"

    text = (
        f"✅ <b>Проверьте настройки подписки:</b>\n\n"
        f"Тип: {type_str}\n"
        f"Куда: {dest_str}\n"
        f"Откуда: {origins_str}\n"
        f"Период: {month_str}\n"
        f"Бюджет: {price_str} / чел.\n"
        f"Пассажиры: {passengers} чел."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подписаться",  callback_data="hd_save")],
        [InlineKeyboardButton(text="✏️ Изменить",     callback_data="hd_new_sub")],
        [InlineKeyboardButton(text="❌ Отмена",        callback_data="hot_deals_menu")],
        [InlineKeyboardButton(text="↩️ В начало",     callback_data="main_menu")],
    ])
    send = target.message.edit_text if isinstance(target, CallbackQuery) else target.answer
    await send(text, parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════════════
# Входная точка
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hot_deals_menu")
async def hot_deals_menu(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state and not current_state.startswith("HotDealsSub"):
        await callback.answer("⚠️ Сначала заверши или отмени текущий поиск билетов", show_alert=True)
        return

    await state.clear()
    user_id = callback.from_user.id
    subs    = await redis_client.get_hot_subs(user_id)

    text = (
        "🔥 <b>Горячие предложения</b>\n\n"
        "Укажи направление, период и бюджет — напишу, как только появится выгодный рейс."
    )
    buttons = [[InlineKeyboardButton(text="⚙️ Настроить", callback_data="hd_new_sub")]]
    if subs:
        buttons.append([InlineKeyboardButton(text=f"Мои подписки ({len(subs)})", callback_data="hd_my_subs")])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

    await callback.message.edit_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 1 — тип подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_new_sub")
async def hd_step1_sub_type(callback: CallbackQuery, state: FSMContext):
    await state.set_state(HotDealsSub.choose_sub_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Горячие предложения",           callback_data="hd_type_hot")],
        [InlineKeyboardButton(text="📰 Дайджест (раз в день / неделю)", callback_data="hd_type_digest")],
        [InlineKeyboardButton(text="↩️ Назад",    callback_data="hot_deals_menu")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.edit_text(
        "Выбери тип подписки:\n\n"
        "<b>Горячие предложения</b> — уведомление, как только появится рейс "
        "дешевле твоего бюджета по нужному направлению.\n\n"
        "<b>Дайджест</b> — раз в день или раз в неделю получай подборку "
        "лучших предложений из твоего города.",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 2 — категория направления
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.in_({"hd_type_hot", "hd_type_digest"}))
async def hd_step2_category(callback: CallbackQuery, state: FSMContext):
    sub_type = "hot" if callback.data == "hd_type_hot" else "digest"
    await state.update_data(sub_type=sub_type, user_id_fsm=callback.from_user.id)
    await state.set_state(HotDealsSub.choose_category)

    # Показываем все категории кроме "custom" — она идёт отдельной кнопкой внизу
    buttons = [[InlineKeyboardButton(text=label, callback_data=f"hd_cat_{key}")]
               for key, (label, _) in CATEGORIES.items() if key != "custom"]
    buttons.append([InlineKeyboardButton(
        text="🔍 Свой маршрут",
        callback_data="hd_cat_custom"
    )])
    buttons.append([InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_new_sub")])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

    await callback.message.edit_text(
        "Выбери <b>направление</b> (тематику путешествия):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 3a — подкатегория назначения
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# ШАГ 3 (custom) — ввод своего направления
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_cat_custom")
async def hd_step3_custom_dest(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал Свой маршрут — просим ввести город или страну."""
    await state.update_data(category="custom", origins=[], dest_iata_list=[], dest_preset_name="")
    await state.set_state(HotDealsSub.choose_dest_custom)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_new_sub")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.edit_text(
        "✈️ <b>Свой маршрут</b>\n\n"
        "Введи <b>город или страну прилёта</b>:\n\n"
        "<i>Примеры:\n"
        "• Вьетнам\n"
        "• Бали\n"
        "• Бангкок\n"
        "• Барселона</i>",
        parse_mode="HTML", reply_markup=kb
    )
    await callback.answer()


@router.message(HotDealsSub.choose_dest_custom)
async def hd_custom_dest_text(message: Message, state: FSMContext):
    """Обрабатываем введённый город/страну назначения."""
    from utils.cities_loader import (
        get_iata, get_city_name, fuzzy_get_iata,
        COUNTRY_NAME_TO_ISO, COUNTRY_TOP_CITIES, IATA_TO_CITY,
    )
    raw = message.text.strip()
    norm = raw.lower().replace("ё", "е")

    dest_iata_list = []
    dest_name      = ""

    # 1. Пробуем как страну
    iso = COUNTRY_NAME_TO_ISO.get(norm)
    if iso:
        dest_iata_list = COUNTRY_TOP_CITIES.get(iso, [])[:6]
        # Красивое название страны — берём из исходного ввода с заглавной
        dest_name = raw.capitalize()

    # 2. Пробуем как город (точное совпадение)
    if not dest_iata_list:
        iata = get_iata(norm) or get_iata(raw)
        if iata:
            dest_iata_list = [iata]
            dest_name = get_city_name(iata) or raw.capitalize()

    # 3. Нечёткий поиск
    if not dest_iata_list:
        iata, corrected = fuzzy_get_iata(raw)
        if iata:
            dest_iata_list = [iata]
            dest_name = corrected or raw.capitalize()

    if not dest_iata_list:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_cat_custom")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
        await message.answer(
            f"❌ Не нашёл направление <b>{raw}</b>.\n\n"
            "Попробуй написать иначе — например:\n"
            "<i>Вьетнам, Бангкок, Барселона, Дубай</i>",
            parse_mode="HTML", reply_markup=kb
        )
        return

    await state.update_data(
        dest_iata_list=dest_iata_list,
        dest_preset_name=dest_name,
        category="custom",
    )
    await message.answer(
        f"✅ Направление: <b>{dest_name}</b>\n"
        f"<i>Аэропорты: {", ".join(dest_iata_list)}</i>",
        parse_mode="HTML"
    )
    await state.set_state(HotDealsSub.choose_origins)
    await _ask_origins(message, state, edit=False)


@router.callback_query(HotDealsSub.choose_dest_custom, F.data == "hd_new_sub")
async def hd_custom_back(callback: CallbackQuery, state: FSMContext):
    """Назад из ввода направления — к выбору типа подписки."""
    data = await state.get_data()
    sub_type = data.get("sub_type", "hot")
    callback.data = f"hd_type_{sub_type}"
    await hd_step2_category(callback, state)


@router.callback_query(F.data.startswith("hd_cat_"))
async def hd_step3_category_chosen(callback: CallbackQuery, state: FSMContext):
    cat = callback.data.replace("hd_cat_", "")
    if cat not in CATEGORIES:
        await callback.answer("Неверная категория", show_alert=True)
        return

    await state.update_data(category=cat, origins=[], dest_iata_list=[], dest_preset_name="")

    presets = CATEGORY_PRESETS.get(cat)
    if presets:
        await state.set_state(HotDealsSub.choose_dest_preset)
        cat_label, _ = CATEGORIES[cat]
        buttons = []
        for label, iata_list in presets:
            cb = "hd_preset_custom" if iata_list is None else f"hd_preset_{'|'.join(iata_list)}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])
        buttons.append([InlineKeyboardButton(text="↩️ Назад",    callback_data=f"hd_type_{await _get_sub_type(state)}")])
        buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
        await callback.message.edit_text(
            f"<b>{cat_label}</b> — куда именно хочешь лететь?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    else:
        # Нет подкатегорий — сразу к вводу городов вылета
        await state.set_state(HotDealsSub.choose_origins)
        await _ask_origins(callback.message, state, edit=True)

    await callback.answer()


async def _get_sub_type(state: FSMContext) -> str:
    data = await state.get_data()
    return data.get("sub_type", "hot")


@router.callback_query(HotDealsSub.choose_dest_preset, F.data.startswith("hd_preset_"))
async def hd_step3b_preset_chosen(callback: CallbackQuery, state: FSMContext):
    preset_key = callback.data.replace("hd_preset_", "")

    if preset_key == "custom":
        await state.update_data(dest_iata_list=[], dest_preset_name="свой вариант")
    else:
        iata_list = preset_key.split("|")
        # Ищем название пресета
        data = await state.get_data()
        cat  = data.get("category", "")
        preset_name = preset_key
        for lbl, lst in (CATEGORY_PRESETS.get(cat) or []):
            if lst and lst == iata_list:
                preset_name = lbl
                break
        await state.update_data(dest_iata_list=iata_list, dest_preset_name=preset_name)

    await state.set_state(HotDealsSub.choose_origins)
    await _ask_origins(callback.message, state, edit=True)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 3b — мультивыбор городов вылета
# ════════════════════════════════════════════════════════════════

@router.message(HotDealsSub.choose_origins)
async def hd_origins_text(message: Message, state: FSMContext):
    """
    Пользователь вводит город(а) вылета — через запятую или пробел.
    Free-тариф: принимаем только первый город, игнорируем остальные.
    """
    raw = message.text.strip()

    data    = await state.get_data()
    origins = list(data.get("origins", []))

    # Проверяем разрешён ли мультигород
    plan_data     = await get_user_plan(message.from_user.id)
    plan_cfg      = PLANS.get(plan_data.get("plan", "free")) or PLANS["free"]
    multi_allowed = plan_cfg.get("multi_origin", False)

    # Если уже есть город и мультигород запрещён — мягко отказываем
    if origins and not multi_allowed:
        await message.answer(
            "На бесплатном тарифе доступен только <b>1 город вылета</b>.\n\n"
            "⚡️ <b>Плюс</b> и 💎 <b>Премиум</b> открывают поиск из нескольких городов.",
            parse_mode="HTML"
        )
        await _ask_origins(message, state, edit=False)
        return

    # Разбиваем по запятым; если запятых нет — по пробелам
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = raw.split()

    # Free: берём только первый токен
    if not multi_allowed:
        parts = parts[:1]

    added     = []
    not_found = []
    dupes     = []

    for part in parts:
        iata = get_iata(part)
        if not iata:
            not_found.append(part)
            continue
        name = get_city_name(iata) or part.capitalize()
        if any(o["iata"] == iata for o in origins):
            dupes.append(name)
            continue
        origins.append({"iata": iata, "name": name})
        added.append(name)
        # Free: останавливаемся после первого добавленного города
        if not multi_allowed:
            break

    # Сохраняем user_id для последующих проверок плана в _ask_origins
    await state.update_data(origins=origins, user_id_fsm=message.from_user.id)

    feedback = []
    if added:
        feedback.append(f"✅ Добавлен: <b>{', '.join(added)}</b>")
    if dupes:
        feedback.append(f"Уже есть: {', '.join(dupes)}")
    if not_found:
        feedback.append(f"❌ Не найдены: {', '.join(not_found)}")
    if feedback:
        await message.answer("\n".join(feedback), parse_mode="HTML")

    await _ask_origins(message, state, edit=False)


@router.callback_query(HotDealsSub.choose_origins, F.data == "hd_origins_done")
async def hd_origins_done(callback: CallbackQuery, state: FSMContext):
    data    = await state.get_data()
    origins = data.get("origins", [])
    if not origins:
        await callback.answer("Добавь хотя бы один город", show_alert=True)
        return
    plan_data     = await get_user_plan(callback.from_user.id)
    plan_cfg      = PLANS.get(plan_data.get("plan", "free")) or PLANS["free"]
    multi_allowed = plan_cfg.get("multi_month", False)
    await state.set_state(HotDealsSub.choose_months)
    await _ask_months(callback, selected=[], multi_allowed=multi_allowed)
    await callback.answer()


@router.callback_query(HotDealsSub.choose_origins, F.data.startswith("hd_origin_del_"))
async def hd_origin_delete(callback: CallbackQuery, state: FSMContext):
    """Удаляем город из списка вылета."""
    iata    = callback.data.replace("hd_origin_del_", "")
    data    = await state.get_data()
    origins = [o for o in data.get("origins", []) if o["iata"] != iata]
    await state.update_data(origins=origins)
    await _ask_origins(callback.message, state, edit=True)
    await callback.answer("Город удалён")


@router.callback_query(HotDealsSub.choose_origins, F.data == "hd_origins_back")
async def hd_origins_back(callback: CallbackQuery, state: FSMContext):
    """Назад — к подкатегориям или к выбору категории."""
    data = await state.get_data()
    cat  = data.get("category", "")
    if CATEGORY_PRESETS.get(cat):
        await state.set_state(HotDealsSub.choose_dest_preset)
        # Показываем пресеты заново
        cat_label, _ = CATEGORIES[cat]
        buttons = []
        for label, iata_list in CATEGORY_PRESETS[cat]:
            cb = "hd_preset_custom" if iata_list is None else f"hd_preset_{'|'.join(iata_list)}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])
        buttons.append([InlineKeyboardButton(text="↩️ Назад",    callback_data=f"hd_cat_{cat}")])
        buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
        await callback.message.edit_text(
            f"<b>{cat_label}</b> — куда именно хотите лететь?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    elif cat == "custom":
        # Назад к вводу своего направления
        await state.set_state(HotDealsSub.choose_dest_custom)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_new_sub")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ])
        await callback.message.edit_text(
            "✈️ <b>Свой маршрут</b>\n\n"
            "Введи <b>город или страну прилёта</b>:\n\n"
            "<i>Примеры: Вьетнам, Бали, Бангкок, Барселона</i>",
            parse_mode="HTML", reply_markup=kb
        )
    else:
        # Назад к выбору категории
        sub_type = data.get("sub_type", "hot")
        await state.set_state(HotDealsSub.choose_category)
        buttons = [[InlineKeyboardButton(text=label, callback_data=f"hd_cat_{key}")]
                   for key, (label, _) in CATEGORIES.items() if key != "custom"]
        buttons.append([InlineKeyboardButton(
            text="🔍 Свой маршрут", callback_data="hd_cat_custom"
        )])
        buttons.append([InlineKeyboardButton(text="↩️ Назад",    callback_data="hd_new_sub")])
        buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
        await callback.message.edit_text(
            "Выбери <b>направление</b> (тематику путешествия):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 4 — месяцы вылета
# ════════════════════════════════════════════════════════════════

@router.callback_query(HotDealsSub.choose_months, F.data.startswith("hd_month_"))
async def hd_step4_month(callback: CallbackQuery, state: FSMContext):
    data      = await state.get_data()
    parts     = callback.data.split("_")  # hd_month_<m>_<y>
    month_val = parts[2]
    year_val  = parts[3]

    # Проверяем разрешён ли мультивыбор
    plan_data     = await get_user_plan(callback.from_user.id)
    plan_cfg      = PLANS.get(plan_data.get("plan", "free")) or PLANS["free"]
    multi_allowed = plan_cfg.get("multi_month", False)

    if month_val == "any":
        await state.update_data(travel_months=[], travel_month=None, travel_year=None)
        await state.set_state(HotDealsSub.choose_budget)
        await _ask_budget(callback)
        await callback.answer()
        return

    key      = f"{month_val}_{year_val}"
    selected = list(data.get("travel_months", []))

    if key in selected:
        selected.remove(key)
        await callback.answer("Снято")
    else:
        selected.append(key)
        await callback.answer("✅ Выбрано")

    await state.update_data(travel_months=selected)

    # Free: показываем галочку на выбранном месяце, затем следующий шаг новым сообщением
    if not multi_allowed and selected:
        first  = selected[0].split("_")
        m_key  = first[0]
        m_year = first[1]
        m_label = MONTHS_LABELS.get(m_key, m_key)
        # 1. Обновляем клавиатуру — выбранный месяц получает ✅
        await _ask_months(callback, selected=selected, multi_allowed=False)
        # 2. Фиксируем выбор в чате отдельным сообщением (как у платных)
        await callback.message.answer(
            f"📅 Месяц вылета: <b>{m_label} {m_year}</b>",
            parse_mode="HTML",
        )
        await callback.answer()
        # 3. Переходим к следующему шагу
        await state.update_data(travel_month=int(first[0]), travel_year=int(first[1]))
        await state.set_state(HotDealsSub.choose_budget)
        await _ask_budget(callback.message)
        return

    await _ask_months(callback, selected=selected, multi_allowed=multi_allowed)


@router.callback_query(HotDealsSub.choose_months, F.data == "hd_months_done")
async def hd_months_done(callback: CallbackQuery, state: FSMContext):
    data     = await state.get_data()
    selected = data.get("travel_months", [])
    if not selected:
        await callback.answer("Выбери хотя бы один месяц", show_alert=True)
        return
    first = selected[0].split("_")
    await state.update_data(travel_month=int(first[0]), travel_year=int(first[1]))
    await state.set_state(HotDealsSub.choose_budget)
    await _ask_budget(callback)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# ШАГ 5 — бюджет
# ════════════════════════════════════════════════════════════════

@router.message(HotDealsSub.choose_budget)
async def hd_step5_budget_text(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ", "").replace("₽", "").replace(",", "")
    if not raw.isdigit():
        await message.answer(
            "Введи сумму числом.\n<i>Пример: 12000</i>",
            parse_mode="HTML",
            reply_markup=BACK_TO_MAIN
        )
        return
    await state.update_data(max_price=int(raw))
    await state.set_state(HotDealsSub.choose_passengers)
    await _ask_passengers(message)


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


# ════════════════════════════════════════════════════════════════
# ШАГ 7 — частота (только для дайджеста)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_freq_"))
async def hd_step7_freq(callback: CallbackQuery, state: FSMContext):
    freq = callback.data.replace("hd_freq_", "")
    await state.update_data(frequency=freq)
    data = await state.get_data()
    await state.set_state(HotDealsSub.confirm)
    await _show_confirm(callback, data)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Сохранение подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "hd_save")
async def hd_save(callback: CallbackQuery, state: FSMContext):
    data    = await state.get_data()
    user_id = callback.from_user.id
    logger.info(f"[HotDeals] Сохранение подписки user_id={user_id}")

    origins = data.get("origins", [])
    # Обратная совместимость: первый город как origin_iata/origin_name
    first_origin = origins[0] if origins else {}

    # ── Проверка лимита тарифа ────────────────────────────────────────────────
    sub_type_val = data.get("sub_type", "hot")
    ok, reason   = await can_add_sub(user_id, sub_type_val, callback.from_user.username)
    if not ok:
        await state.clear()
        await show_paywall(callback, reason)
        return
    # ─────────────────────────────────────────────────────────────────────────

    sub = {
        "user_id":         user_id,
        "sub_type":        data.get("sub_type", "hot"),
        "category":        data.get("category", "world"),
        "dest_iata_list":  data.get("dest_iata_list", []),
        "dest_preset_name": data.get("dest_preset_name", ""),
        "origins":         origins,
        "origin_iata":     first_origin.get("iata", ""),
        "origin_name":     first_origin.get("name", ""),
        "travel_months":   data.get("travel_months", []),
        "travel_month":    data.get("travel_month"),
        "travel_year":     data.get("travel_year"),
        "max_price":       data.get("max_price", 0),
        "passengers":      data.get("passengers", 1),
        "frequency":       data.get("frequency", "daily"),
        "created_at":      int(time.time()),
        "last_notified":   0,
    }

    await redis_client.save_hot_sub(user_id, sub)
    import asyncio as _aio
    _aio.ensure_future(redis_client.track_subscription_event(sub.get("sub_type", "hot_deals"), "created"))
    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Подписка создана</b>\n\n"
        "Напишу, как только появится рейс по твоим условиям.\n\n"
        "<i>Управляй подпиской в разделе <b>Подписки</b>.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Мои подписки", callback_data="subs_menu")],
            [InlineKeyboardButton(text="↩️ В начало",  callback_data="main_menu")],
        ])
    )
    await callback.answer("Подписка сохранена")


# ════════════════════════════════════════════════════════════════
# Список подписок
# ════════════════════════════════════════════════════════════════

async def hd_my_subs_text_kb(user_id: int, subs: dict) -> tuple[str, InlineKeyboardMarkup]:
    """
    Вспомогательная функция: строит текст и клавиатуру списка подписок.
    Используется как из callback-хендлера hd_my_subs,
    так и из nav_subs в start.py (нажатие кнопки нижней панели).
    """
    blocks  = []
    buttons = []

    for idx, (sub_id, sub) in enumerate(subs.items(), 1):
        cat_label, _ = CATEGORIES.get(sub.get("category", ""), ("—", []))
        is_hot   = sub.get("sub_type") == "hot"
        sub_icon = "🔥" if is_hot else "📰"

        max_price = sub.get("max_price")
        price_str = f"до {max_price:,} ₽".replace(",", "\u202f") if max_price else "без ограничений"

        pax = sub.get("passengers", 1)
        if pax == 1:
            pax_str = "1 пассажир"
        elif pax < 5:
            pax_str = f"{pax} пассажира"
        else:
            pax_str = f"{pax} пассажиров"

        origins = sub.get("origins", [])
        origin_str = (
            ", ".join(o["name"] for o in origins)
            if origins
            else sub.get("origin_name", sub.get("origin_iata", "?"))
        )

        dest_str = sub.get("dest_preset_name") or cat_label

        travel_months = sub.get("travel_months", [])
        if travel_months:
            month_str = ", ".join(
                MONTHS_LABELS.get(mk.split("_")[0], mk) for mk in travel_months
            )
        elif sub.get("travel_month"):
            month_str = (
                f"{MONTHS_LABELS.get(str(sub['travel_month']), '?')} "
                f"{sub.get('travel_year', '')}"
            ).strip()
        else:
            month_str = "любой период"

        freq_map  = {"daily": "ежедневно", "weekly": "раз в неделю"}
        freq_line = f"\n{freq_map[sub['frequency']]}" if not is_hot and sub.get("frequency") else ""

        blocks.append(
            f"<b>{idx}. {dest_str}</b>\n"
            f"Откуда: {origin_str}\n"
            f"Период: {month_str}\n"
            f"Бюджет: {price_str} · {pax_str}{freq_line}"
        )
        buttons.append([InlineKeyboardButton(
            text=f"Удалить: {dest_str}",
            callback_data=f"hd_del_{sub_id}"
        )])

    divider = "\n\n" + "─" * 20 + "\n\n"
    count_str = f"1 подписка" if len(subs) == 1 else f"{len(subs)} подписки" if len(subs) < 5 else f"{len(subs)} подписок"
    text = (
        f"<b>Подписки</b> · <i>{count_str}</i>\n\n"
        + divider.join(blocks)
    )
    # Кнопки навигации НЕ добавляем здесь — их добавляет subscriptions.py
    # чтобы не было дублей "Добавить подписку" + "Добавить ещё"

    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "hd_my_subs")
async def hd_my_subs(callback: CallbackQuery, state: FSMContext = None):
    user_id = callback.from_user.id
    subs    = await redis_client.get_hot_subs(user_id)

    if not subs:
        await callback.message.edit_text(
            "<b>Подписки</b>\n\nАктивных подписок пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настроить",  callback_data="hd_new_sub")],
                [InlineKeyboardButton(text="↩️ В начало",   callback_data="main_menu")],
            ])
        )
        await callback.answer()
        return

    text, kb = await hd_my_subs_text_kb(user_id, subs)
    # Добавляем кнопки навигации (subscriptions.py здесь не участвует)
    new_buttons = list(kb.inline_keyboard)

    # Умная кнопка: если лимит исчерпан — ведём на тарифы, иначе — добавить
    ok, _ = await can_add_sub(user_id, "hot", callback.from_user.username)
    if ok:
        new_buttons.append([InlineKeyboardButton(text="➕ Добавить ещё", callback_data="hd_new_sub")])
    else:
        new_buttons.append([InlineKeyboardButton(text="💳 Увеличить лимит подписок", callback_data="billing_menu")])

    new_buttons.append([InlineKeyboardButton(text="↩️ К подпискам", callback_data="subs_menu")])
    new_buttons.append([InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")])
    new_kb = InlineKeyboardMarkup(inline_keyboard=new_buttons)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=new_kb)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Удаление подписки
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_del_"))
async def hd_delete_sub(callback: CallbackQuery, state: FSMContext):
    sub_id  = callback.data.replace("hd_del_", "")
    user_id = callback.from_user.id
    await redis_client.delete_hot_sub(user_id, sub_id)
    await callback.answer("Подписка удалена")
    await hd_my_subs(callback)


# ════════════════════════════════════════════════════════════════
# Кнопки из напоминалок (nudge)
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("hd_keep_"))
async def hd_keep_sub(callback: CallbackQuery):
    """Пользователь нажал 'Продолжать следить' в напоминалке."""
    await callback.answer("✅ Продолжаем следить за ценами!", show_alert=False)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои подписки", callback_data="hd_my_subs")],
                [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
            ])
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("hd_edit_"))
async def hd_edit_sub_budget(callback: CallbackQuery, state: FSMContext):
    """Пользователь нажал 'Изменить бюджет' из напоминалки — открываем его подписки."""
    await callback.answer()
    await hd_my_subs(callback)