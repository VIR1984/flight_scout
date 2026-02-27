import re
import asyncio
from uuid import uuid4
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from states.flight_states import FlightSearch
from services.flight_search import search_flights, generate_booking_link, normalize_date
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities import CITY_TO_IATA, GLOBAL_HUBS, IATA_TO_CITY
from utils.cities_loader import get_iata_fuzzy, fuzzy_search_city, format_fuzzy_suggestion, _normalize_name
from utils.redis_client import redis_client
from datetime import datetime

router = Router()

# ===== Вспомогательные функции =====
def validate_route(text: str) -> tuple:
    """Парсит маршрут: 'Москва - Сочи' или 'Москва Сочи'"""
    text = text.strip().lower()
    
    # Разделяем по дефису, стрелке или пробелуу
    if any(sym in text for sym in ['-', '→', '—', '>']):
        parts = re.split(r'[-→—>]+', text)
    else:
        parts = text.split()
    
    if len(parts) < 2:
        return None, None
    
    origin = parts[0].strip()
    dest = parts[1].strip()
    
    # Если "везде" в начале
    if origin == "везде":
        return "везде", dest
    
    return origin, dest

def validate_date(date_str: str) -> bool:
    """Проверяет формат даты ДД.ММ"""
    try:
        day, month = map(int, date_str.split('.'))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return True
    except:
        pass
    return False

def build_passenger_code(adults: int, children: int = 0, infants: int = 0) -> str:
    """Формирует код пассажиров с ограничениями Aviasales"""
    adults = max(1, adults)  # Минимум 1 взрослый
    total = adults + children + infants
    
    # Максимум 9 человек
    if total > 9:
        remaining = 9 - adults
        if children + infants > remaining:
            children = min(children, remaining)
            infants = max(0, remaining - children)
    
    # Младенцев не больше взрослых
    if infants > adults:
        infants = adults
    
    code = str(adults)
    if children > 0:
        code += str(children)
    if infants > 0:
        code += str(infants)
    
    return code

def build_passenger_desc(code: str) -> str:
    """Формирует описание пассажиров для отображения"""
    try:
        ad = int(code[0])
        ch = int(code[1]) if len(code) > 1 else 0
        inf = int(code[2]) if len(code) > 2 else 0
        
        parts = []
        if ad: parts.append(f"{ad} взр.")
        if ch: parts.append(f"{ch} реб.")
        if inf: parts.append(f"{inf} мл.")
        
        return ", ".join(parts)
    except:
        return "1 взр."

def format_user_date(date_str: str) -> str:
    """Форматирует дату для отображения пользователю"""
    try:
        d, m = map(int, date_str.split('.'))
        year = datetime.now().year
        current_month = datetime.now().month
        current_day = datetime.now().day
        
        if (m < current_month) or (m == current_month and d < current_day):
            year += 1
        
        return f"{d:02d}.{m:02d}.{year}"
    except:
        return date_str


def _parse_city_input(raw: str) -> list[dict]:
    """
    Разбирает строку с городами (через запятую или один город).
    
    Каждый токен прогоняется через нечёткий поиск.
    Возвращает список словарей:
        {
            "input":   оригинальный ввод,
            "iata":    найденный IATA или None,
            "name":    отображаемое название или None,
            "score":   0.0–1.0,
            "exact":   True если точное совпадение,
        }
    """
    tokens = [t.strip() for t in re.split(r'[,;]+', raw) if t.strip()]
    results = []
    seen_iata: set[str] = set()

    for token in tokens:
        norm = _normalize_name(token)

        # "Везде" — особый случай
        if norm in ("везде", "anywhere", "any"):
            results.append({"input": token, "iata": "ANY", "name": "Везде", "score": 1.0, "exact": True})
            continue

        # IATA напрямую (3 латинских буквы)
        if re.match(r'^[A-Za-z]{3}$', token.strip()):
            iata = token.strip().upper()
            name = IATA_TO_CITY.get(iata, iata)
            entry = {"input": token, "iata": iata, "name": name, "score": 1.0, "exact": True}
        else:
            iata, name, score = get_iata_fuzzy(token)
            entry = {"input": token, "iata": iata, "name": name, "score": score, "exact": score == 1.0}

        # Дедупликация по IATA
        if entry["iata"] and entry["iata"] in seen_iata:
            continue

        if entry["iata"]:
            seen_iata.add(entry["iata"])

        results.append(entry)

    return results


def _build_origin_summary(cities: list[dict]) -> str:
    """Формирует строку-сводку найденных городов для подтверждения."""
    lines = []
    for c in cities:
        if c["exact"]:
            lines.append(f"✅ {c['name']} ({c['iata']})")
        else:
            pct = int(c["score"] * 100)
            lines.append(f"🤔 {c['name']} ({c['iata']}) ~{pct}% — уточнение")
    return "\n".join(lines)


def _build_origin_edit_keyboard(cities: list[dict]) -> InlineKeyboardMarkup:
    """
    Строит клавиатуру редактирования списка городов вылета.
    Каждый город — строка с кнопкой ❌ для удаления.
    Внизу — «➕ Добавить город» и «✅ Готово».
    """
    rows = []
    for i, c in enumerate(cities):
        label = f"{c['name']} ({c['iata']})"
        rows.append([
            InlineKeyboardButton(text=f"🏙 {label}", callback_data="noop"),
            InlineKeyboardButton(text="❌", callback_data=f"origin_remove:{i}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить город", callback_data="origin_add")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="origin_done")])
    rows.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ===== Шаг 1а: Запрос городов вылета =====

@router.callback_query(F.data == "start_search")
async def start_flight_search(callback: CallbackQuery, state: FSMContext):
    """Начало пошагового поиска — запрашиваем города вылета"""
    await callback.message.edit_text(
        "✈️ <b>Начнём поиск билетов!</b>\n\n"
        "📍 <b>Шаг 1 из 5:</b> Введите город(а) вылета\n\n"
        "💡 <b>Можно ввести несколько городов через запятую:</b>\n"
        "<code>Москва, Казань, Екатеринбург</code>\n\n"
        "📌 <b>Также поддерживается:</b>\n"
        "• IATA-коды: <code>MOW, KZN</code>\n"
        "• Один город: <code>Санкт-Петербург</code>\n"
        "• Везде: <code>Везде</code>",
        parse_mode="HTML"
    )
    await state.set_state(FlightSearch.origin_cities)
    await callback.answer()


@router.message(FlightSearch.origin_cities)
async def process_origin_cities(message: Message, state: FSMContext):
    """Обработка ввода городов вылета"""
    parsed = _parse_city_input(message.text)

    # Нет ни одного токена
    if not parsed:
        await message.answer(
            "❌ Не удалось распознать города. Попробуйте ещё раз.\n"
            "Пример: <code>Москва, Казань</code>",
            parse_mode="HTML"
        )
        return

    # Есть нераспознанные города
    not_found = [c for c in parsed if not c["iata"]]
    if not_found:
        names = ", ".join(f"«{c['input']}»" for c in not_found)
        await message.answer(
            f"❌ Не нашёл город(а): {names}\n"
            "Проверьте написание или используйте IATA-код (например, MOW).",
            parse_mode="HTML"
        )
        return

    # Нечёткие совпадения — показываем для подтверждения
    fuzzy = [c for c in parsed if not c["exact"]]
    if fuzzy:
        lines = []
        for c in fuzzy:
            lines.append(
                f"  «{c['input']}» → <b>{c['name']}</b> ({c['iata']}, {int(c['score']*100)}%)"
            )
        confirm_text = (
            "🤔 Уточните, правильно ли распознаны города:\n\n"
            + "\n".join(lines) + "\n\n"
            "Нажмите <b>Да</b>, чтобы продолжить, или <b>Нет</b>, чтобы ввести заново."
        )
        await state.update_data(pending_origins=parsed)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, верно", callback_data="origins_fuzzy_confirm"),
            InlineKeyboardButton(text="✏️ Ввести заново", callback_data="origins_fuzzy_retry"),
        ]])
        await message.answer(confirm_text, parse_mode="HTML", reply_markup=kb)
        return

    # Всё точно — сохраняем и спрашиваем город прилёта
    await state.update_data(origins=parsed)
    await _ask_dest_city(message, state, parsed)


@router.callback_query(F.data == "origins_fuzzy_confirm")
async def origins_fuzzy_confirm(callback: CallbackQuery, state: FSMContext):
    """Пользователь подтвердил нечёткие совпадения"""
    data = await state.get_data()
    origins = data.get("pending_origins", [])
    await state.update_data(origins=origins, pending_origins=None)
    await _ask_dest_city(callback.message, state, origins, edit=True)
    await callback.answer()


@router.callback_query(F.data == "origins_fuzzy_retry")
async def origins_fuzzy_retry(callback: CallbackQuery, state: FSMContext):
    """Пользователь хочет ввести города заново"""
    await callback.message.edit_text(
        "📍 Введите город(а) вылета заново:\n"
        "Пример: <code>Москва, Казань, Сочи</code>",
        parse_mode="HTML"
    )
    await state.set_state(FlightSearch.origin_cities)
    await callback.answer()


async def _ask_dest_city(message, state: FSMContext, origins: list[dict], edit: bool = False):
    """Показываем подтверждение городов вылета и запрашиваем город прилёта"""
    summary = _build_origin_summary(origins)
    names_list = ", ".join(c["name"] for c in origins)

    text = (
        f"✅ <b>Город(а) вылета:</b>\n{summary}\n\n"
        "🏁 <b>Шаг 1б:</b> Введите город прилёта\n\n"
        "📌 Пример: <code>Сочи</code> или <code>AER</code>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")
    await state.set_state(FlightSearch.dest_city)


# ===== Шаг 1б: Город прилёта =====

@router.message(FlightSearch.dest_city)
async def process_dest_city(message: Message, state: FSMContext):
    """Обработка города прилёта"""
    parsed = _parse_city_input(message.text)

    if not parsed or not parsed[0]["iata"]:
        # Нечёткий поиск для подсказки
        suggestions = fuzzy_search_city(message.text, limit=3)
        hint = ""
        if suggestions:
            hint = "\n💡 Похожие города: " + ", ".join(f"{n} ({i})" for n, i, _ in suggestions)
        await message.answer(
            f"❌ Не нашёл город «{message.text}».{hint}\n"
            "Попробуйте ещё раз или введите IATA-код.",
            parse_mode="HTML"
        )
        return

    dest = parsed[0]

    # Нечёткое совпадение — подтверждение
    if not dest["exact"]:
        await state.update_data(pending_dest=dest)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, {dest['name']}", callback_data="dest_fuzzy_confirm"),
            InlineKeyboardButton(text="✏️ Ввести заново", callback_data="dest_fuzzy_retry"),
        ]])
        await message.answer(
            f"🤔 Имели в виду <b>{dest['name']}</b> ({dest['iata']}, {int(dest['score']*100)}%)?",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    await _save_dest_and_continue(message, state, dest)


@router.callback_query(F.data == "dest_fuzzy_confirm")
async def dest_fuzzy_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    dest = data.get("pending_dest")
    await state.update_data(pending_dest=None)
    await _save_dest_and_continue(callback.message, state, dest, edit=True)
    await callback.answer()


@router.callback_query(F.data == "dest_fuzzy_retry")
async def dest_fuzzy_retry(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🏁 Введите город прилёта:\nПример: <code>Дубай</code> или <code>DXB</code>",
        parse_mode="HTML"
    )
    await state.set_state(FlightSearch.dest_city)
    await callback.answer()


async def _save_dest_and_continue(message, state: FSMContext, dest: dict, edit: bool = False):
    """Сохраняем город прилёта и переходим к дате"""
    await state.update_data(
        dest=dest["name"].lower(),
        dest_iata=dest["iata"],
        dest_name=dest["name"],
    )
    data = await state.get_data()
    origins = data.get("origins", [])
    origins_str = ", ".join(c["name"] for c in origins)

    text = (
        f"✅ <b>Маршрут:</b> {origins_str} → {dest['name']}\n\n"
        "📅 <b>Шаг 2 из 5:</b> Введите дату вылета в формате <code>ДД.ММ</code>\n\n"
        "📌 Пример: <code>10.03</code>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")
    await state.set_state(FlightSearch.depart_date)


# ===== Редактирование городов вылета из сводки =====

@router.callback_query(F.data == "edit_origins")
async def edit_origins(callback: CallbackQuery, state: FSMContext):
    """Открыть экран редактирования городов вылета"""
    data = await state.get_data()
    origins = data.get("origins", [])
    kb = _build_origin_edit_keyboard(origins)
    names = "\n".join(f"  • {c['name']} ({c['iata']})" for c in origins)
    await callback.message.edit_text(
        f"📍 <b>Города вылета:</b>\n{names}\n\n"
        "Нажмите ❌ рядом с городом, чтобы удалить его.\n"
        "Нажмите <b>➕ Добавить город</b>, чтобы добавить ещё.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("origin_remove:"))
async def origin_remove(callback: CallbackQuery, state: FSMContext):
    """Удалить город из списка вылетов"""
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    origins: list = data.get("origins", [])

    if len(origins) <= 1:
        await callback.answer("⚠️ Должен остаться хотя бы один город вылета!", show_alert=True)
        return

    removed = origins.pop(idx)
    await state.update_data(origins=origins)

    kb = _build_origin_edit_keyboard(origins)
    names = "\n".join(f"  • {c['name']} ({c['iata']})" for c in origins)
    await callback.message.edit_text(
        f"🗑 Удалён: <b>{removed['name']}</b>\n\n"
        f"📍 <b>Города вылета:</b>\n{names}\n\n"
        "Нажмите ❌ рядом с городом, чтобы удалить его.\n"
        "Нажмите <b>➕ Добавить город</b>, чтобы добавить ещё.",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data == "origin_add")
async def origin_add(callback: CallbackQuery, state: FSMContext):
    """Запросить ввод нового города для добавления"""
    data = await state.get_data()
    origins = data.get("origins", [])
    existing = ", ".join(c["name"] for c in origins)
    await callback.message.edit_text(
        f"📍 Текущие города вылета: <b>{existing}</b>\n\n"
        "✍️ Введите название города, который хотите добавить:",
        parse_mode="HTML"
    )
    await state.set_state(FlightSearch.edit_origin_add)
    await callback.answer()


@router.message(FlightSearch.edit_origin_add)
async def process_origin_add(message: Message, state: FSMContext):
    """Обработка нового города из режима редактирования"""
    parsed = _parse_city_input(message.text)

    if not parsed or not parsed[0]["iata"]:
        suggestions = fuzzy_search_city(message.text, limit=3)
        hint = ""
        if suggestions:
            hint = "\n💡 Похожие: " + ", ".join(f"{n} ({i})" for n, i, _ in suggestions)
        await message.answer(
            f"❌ Не нашёл город «{message.text}».{hint}\n"
            "Попробуйте ещё раз:",
            parse_mode="HTML"
        )
        return

    new_city = parsed[0]
    data = await state.get_data()
    origins: list = data.get("origins", [])

    # Проверка дубликата
    if any(c["iata"] == new_city["iata"] for c in origins):
        await message.answer(
            f"⚠️ Город <b>{new_city['name']}</b> уже есть в списке.",
            parse_mode="HTML"
        )
        return

    # Нечёткое — подтверждение
    if not new_city["exact"]:
        await state.update_data(pending_add_city=new_city)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"✅ Да, {new_city['name']}", callback_data="origin_add_confirm"),
            InlineKeyboardButton(text="✏️ Ввести заново", callback_data="origin_add"),
        ]])
        await message.answer(
            f"🤔 Имели в виду <b>{new_city['name']}</b> ({new_city['iata']}, {int(new_city['score']*100)}%)?",
            parse_mode="HTML",
            reply_markup=kb
        )
        return

    origins.append(new_city)
    await state.update_data(origins=origins)
    await _show_origins_edit(message, state, origins, added=new_city["name"])


@router.callback_query(F.data == "origin_add_confirm")
async def origin_add_confirm(callback: CallbackQuery, state: FSMContext):
    """Подтвердить добавление нечётко найденного города"""
    data = await state.get_data()
    new_city = data.get("pending_add_city")
    origins: list = data.get("origins", [])
    origins.append(new_city)
    await state.update_data(origins=origins, pending_add_city=None)
    await _show_origins_edit(callback.message, state, origins, added=new_city["name"], edit=True)
    await callback.answer()


async def _show_origins_edit(message, state: FSMContext, origins: list, added: str = None, edit: bool = False):
    """Показать обновлённый список городов вылета"""
    kb = _build_origin_edit_keyboard(origins)
    names = "\n".join(f"  • {c['name']} ({c['iata']})" for c in origins)
    prefix = f"✅ Добавлен: <b>{added}</b>\n\n" if added else ""
    text = (
        f"{prefix}"
        f"📍 <b>Города вылета:</b>\n{names}\n\n"
        "Нажмите ❌ рядом с городом, чтобы удалить его.\n"
        "Нажмите <b>➕ Добавить город</b>, чтобы добавить ещё."
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)


@router.callback_query(F.data == "noop")
async def noop_handler(callback: CallbackQuery):
    """Заглушка для информационных кнопок"""
    await callback.answer()


@router.callback_query(F.data == "origin_done")
async def origin_done(callback: CallbackQuery, state: FSMContext):
    """Завершить редактирование городов, вернуться к сводке"""
    await show_summary(callback.message, state)
    await callback.answer()


# ===== Шаги 2–5 остаются без изменений =====

@router.message(FlightSearch.route)
async def process_route(message: Message, state: FSMContext):
    """Обработка маршрута (legacy-ввод одной строкой)"""
    origin, dest = validate_route(message.text)
    
    if not origin or not dest:
        await message.answer(
            "❌ Неверный формат маршрута.\n"
            "Попробуйте ещё раз: <code>Москва - Сочи</code>",
            parse_mode="HTML"
        )
        return
    
    # Проверяем города
    if origin != "везде":
        orig_iata = CITY_TO_IATA.get(origin)
        if not orig_iata:
            await message.answer(f"❌ Не знаю город отправления: {origin}\nПопробуйте ещё раз.")
            return
        origin_name = IATA_TO_CITY.get(orig_iata, origin.capitalize())
    else:
        orig_iata = None
        origin_name = "Везде"
    
    dest_iata = CITY_TO_IATA.get(dest)
    if not dest_iata:
        await message.answer(f"❌ Не знаю город прибытия: {dest}\nПопробуйте ещё раз.")
        return
    
    dest_name = IATA_TO_CITY.get(dest_iata, dest.capitalize())
    
    # Сохраняем как список из одного города для совместимости
    origins = [{"input": origin, "iata": orig_iata, "name": origin_name, "score": 1.0, "exact": True}]
    await state.update_data(
        origins=origins,
        origin=origin,
        origin_iata=orig_iata,
        dest=dest,
        dest_iata=dest_iata,
        origin_name=origin_name,
        dest_name=dest_name
    )
    
    await message.answer(
        f"✅ Маршрут: <b>{origin_name} → {dest_name}</b>\n\n"
        "📅 <b>Шаг 2 из 5:</b> Введите дату вылета в формате <code>ДД.ММ</code>\n\n"
        "📌 <b>Пример:</b> 10.03",
        parse_mode="HTML"
    )
    await state.set_state(FlightSearch.depart_date)

@router.message(FlightSearch.depart_date)
async def process_depart_date(message: Message, state: FSMContext):
    """Обработка даты вылета"""
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 10.03)",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(depart_date=message.text)
    
    # Спрашиваем про обратный билет
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, нужен", callback_data="need_return_yes")],
        [InlineKeyboardButton(text="❌ Нет, спасибо", callback_data="need_return_no")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")]
    ])
    
    await message.answer(
        f"✅ Дата вылета: <b>{message.text}</b>\n\n"
        "🔄 <b>Шаг 3 из 5:</b> Нужен ли обратный билет?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await state.set_state(FlightSearch.need_return)

@router.callback_query(FlightSearch.need_return, F.data.startswith("need_return_"))
async def process_need_return(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора обратного билета"""
    need_return = callback.data == "need_return_yes"
    
    await state.update_data(need_return=need_return)
    
    if need_return:
        await callback.message.edit_text(
            "📅 <b>Шаг 4 из 5:</b> Введите дату возврата в формате <code>ДД.ММ</code>\n\n"
            "📌 <b>Пример:</b> 15.03",
            parse_mode="HTML"
        )
        await state.set_state(FlightSearch.return_date)
    else:
        # Пропускаем дату возврата, переходим к пассажирам
        await state.update_data(return_date=None)
        await ask_adults(callback.message, state)
    
    await callback.answer()

@router.message(FlightSearch.return_date)
async def process_return_date(message: Message, state: FSMContext):
    """Обработка даты возврата"""
    if not validate_date(message.text):
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ</code> (например: 15.03)",
            parse_mode="HTML"
        )
        return
    
    await state.update_data(return_date=message.text)
    
    # Переходим к пассажирам
    await ask_adults(message, state)

async def ask_adults(message_or_callback, state: FSMContext):
    """Запрашиваем количество взрослых"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1", callback_data="adults_1"),
            InlineKeyboardButton(text="2", callback_data="adults_2"),
            InlineKeyboardButton(text="3", callback_data="adults_3"),
            InlineKeyboardButton(text="4", callback_data="adults_4"),
        ],
        [
            InlineKeyboardButton(text="5", callback_data="adults_5"),
            InlineKeyboardButton(text="6", callback_data="adults_6"),
            InlineKeyboardButton(text="7", callback_data="adults_7"),
            InlineKeyboardButton(text="8", callback_data="adults_8"),
        ],
        [
            InlineKeyboardButton(text="9", callback_data="adults_9"),
        ],
        [
            InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")
        ]
    ])
    
    text = "👥 <b>Шаг 5 из 5:</b> Сколько взрослых пассажиров (от 12 лет)?\n(max. до 9 человек)"
    
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)
    
    await state.set_state(FlightSearch.adults)

@router.callback_query(FlightSearch.adults, F.data.startswith("adults_"))
async def process_adults(callback: CallbackQuery, state: FSMContext):
    """Обработка количества взрослых"""
    adults = int(callback.data.split("_")[1])
    await state.update_data(adults=adults)
    
    # Если 9 взрослых - пропускаем детей и младенцев
    if adults == 9:
        await state.update_data(children=0, infants=0)
        await show_summary(callback.message, state)
    else:
        # Спрашиваем про детей
        max_children = 9 - adults
        kb_buttons = []
        row = []
        
        for i in range(0, max_children + 1):
            row.append(InlineKeyboardButton(text=str(i), callback_data=f"children_{i}"))
            if len(row) == 4:
                kb_buttons.append(row)
                row = []
        
        if row:
            kb_buttons.append(row)
        
        kb_buttons.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        
        await callback.message.edit_text(
            f"👥 Взрослых: <b>{adults}</b>\n\n"
            f"👶 Сколько детей (от 2-11 лет)?"
            f"Если у вас младенцы, укажете дальше",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.children)
    
    await callback.answer()

@router.callback_query(FlightSearch.children, F.data.startswith("children_"))
async def process_children(callback: CallbackQuery, state: FSMContext):
    """Обработка количества детей"""
    children = int(callback.data.split("_")[1])
    await state.update_data(children=children)
    
    data = await state.get_data()
    adults = data["adults"]
    remaining = 9 - adults - children
    
    # Если места закончились - пропускаем младенцев
    if remaining == 0:
        await state.update_data(infants=0)
        await show_summary(callback.message, state)
    else:
        # Спрашиваем про младенцев (не больше взрослых)
        max_infants = min(adults, remaining)
        kb_buttons = []
        row = []
        
        for i in range(0, max_infants + 1):
            row.append(InlineKeyboardButton(text=str(i), callback_data=f"infants_{i}"))
            if len(row) == 4:
                kb_buttons.append(row)
                row = []
        
        if row:
            kb_buttons.append(row)
        
        kb_buttons.append([InlineKeyboardButton(text="↩️ Отмена", callback_data="cancel_search")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
        
        await callback.message.edit_text(
            f"👥 Взрослых: <b>{adults}</b>\n"
            f"👶 Детей: <b>{children}</b>\n\n"
            f"🍼 Сколько младенцев? (младше 2-х лет без места)",
            parse_mode="HTML",
            reply_markup=kb
        )
        await state.set_state(FlightSearch.infants)
    
    await callback.answer()

@router.callback_query(FlightSearch.infants, F.data.startswith("infants_"))
async def process_infants(callback: CallbackQuery, state: FSMContext):
    """Обработка количества младенцев"""
    infants = int(callback.data.split("_")[1])
    await state.update_data(infants=infants)
    
    await show_summary(callback.message, state)
    await callback.answer()

async def show_summary(message, state: FSMContext):
    """Показываем сводку и подтверждаем поиск"""
    data = await state.get_data()
    
    adults = data["adults"]
    children = data.get("children", 0)
    infants = data.get("infants", 0)
    
    passenger_code = build_passenger_code(adults, children, infants)
    passenger_desc = build_passenger_desc(passenger_code)
    
    # Города вылета (новый формат — список, или старый — одиночный)
    origins: list = data.get("origins") or []
    if not origins and data.get("origin_name"):
        origins = [{"name": data["origin_name"], "iata": data.get("origin_iata", ""), "exact": True}]
    
    origins_display = ", ".join(c["name"] for c in origins) if origins else "—"
    dest_name = data.get("dest_name", "—")
    
    summary = (
        "📋 <b>Проверьте данные:</b>\n\n"
        f"📍 Откуда: <b>{origins_display}</b>\n"
        f"🏁 Куда: <b>{dest_name}</b>\n"
        f"📅 Вылет: <b>{data['depart_date']}</b>\n"
    )
    
    if data.get("need_return") and data.get("return_date"):
        summary += f"📅 Возврат: <b>{data['return_date']}</b>\n"
    
    summary += f"👥 Пассажиры: <b>{passenger_desc}</b>\n\n"
    summary += "🔍 Начать поиск?"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Начать поиск", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Изменить города вылета", callback_data="edit_origins")],
        [InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data="edit_route")],
        [InlineKeyboardButton(text="✏️ Изменить даты", callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Изменить пассажиров", callback_data="edit_passengers")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_search")]
    ])
    
    await state.update_data(
        passenger_code=passenger_code,
        passenger_desc=passenger_desc
    )
    
    await message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и запуск поиска"""
    data = await state.get_data()
    
    await callback.message.edit_text("⏳ Ищу билеты (включая с пересадками)...")
    
    # Определяем пункты вылета
    origins_data: list = data.get("origins") or []
    if origins_data and origins_data[0].get("iata") == "ANY":
        # Везде
        origins = GLOBAL_HUBS[:5]
        origin_name = "Везде"
    elif origins_data:
        origins = [c["iata"] for c in origins_data if c.get("iata")]
        origin_name = ", ".join(c["name"] for c in origins_data)
    else:
        # Fallback на старый формат
        origins = [data["origin_iata"]] if data.get("origin_iata") else GLOBAL_HUBS[:5]
        origin_name = data.get("origin_name", "Везде")
    
    dest_iata = data["dest_iata"]
    dest_name = data["dest_name"]
    
    # Запросы к API
    all_flights = []
    for i, orig in enumerate(origins):
        if i > 0:
            await asyncio.sleep(1)
        
        flights = await search_flights(
            orig,
            dest_iata,
            normalize_date(data["depart_date"]),
            normalize_date(data["return_date"]) if data.get("return_date") else None
        )
        
        for f in flights:
            f["origin"] = orig
        
        all_flights.extend(flights)
    
    if not all_flights:
        origin_iata = origins[0]
        d1 = data["depart_date"].replace('.', '')
        d2 = data["return_date"].replace('.', '') if data.get("return_date") else ''
        route = f"{origin_iata}{d1}{dest_iata}{d2}1"
        
        from dotenv import load_dotenv
        import os
        load_dotenv()
        
        marker = os.getenv("TRAFFIC_SOURCE", "").strip()
        link = f"https://www.aviasales.ru/search/{route}"
        if marker:
            link += f"?marker={marker}"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Посмотреть на Aviasales", url=link)],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="main_menu")]
        ])
        
        await callback.message.edit_text(
            "😔 Билеты не найдены.\n"
            "На Aviasales могут быть рейсы с пересадками — попробуйте:",
            reply_markup=kb
        )
        
        await state.clear()
        return
    
    # Сохраняем в кэш
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "dest_iata": dest_iata,
        "is_roundtrip": data.get("need_return", False),
        "display_depart": format_user_date(data["depart_date"]),
        "display_return": format_user_date(data["return_date"]) if data.get("return_date") else None,
        "original_depart": data["depart_date"],
        "original_return": data["return_date"],
        "passenger_desc": data["passenger_desc"],
        "passengers_code": data["passenger_code"]
    })
    
    # Расчет минимальной цены
    min_price = min([f.get("value") or f.get("price") or 999999 for f in all_flights])
    total_flights = len(all_flights)
    
    # Формируем сообщение
    text = (
        f"✅ <b>Билеты найдены!</b>\n\n"
        f"📍 <b>Маршрут:</b> {origin_name} → {dest_name}\n"
        f"📅 <b>Дата вылета:</b> {format_user_date(data['depart_date'])}\n"
    )
    
    if data.get("need_return") and data.get("return_date"):
        text += f"📅 <b>Дата возврата:</b> {format_user_date(data['return_date'])}\n"
    
    text += (
        f"👥 <b>Пассажиры:</b> {data['passenger_desc']}\n"
        f"💰 <b>Самая низкая цена от:</b> {min_price} ₽/чел.\n"
        f"📊 <b>Всего вариантов:</b> {total_flights}\n\n"
        f"Выберите, как хотите посмотреть билеты:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"✈️ Самый дешёвый ({min_price} ₽)",
                callback_data=f"show_top_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"📋 Все варианты ({total_flights})",
                callback_data=f"show_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="📉 Следить за ценой",
                callback_data=f"watch_all_{cache_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="↩️ В меню",
                callback_data="main_menu"
            )
        ]
    ])
    
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

# ===== Обработчики редактирования =====
@router.callback_query(FlightSearch.confirm, F.data.startswith("edit_"))
async def edit_step(callback: CallbackQuery, state: FSMContext):
    """Возврат к редактированию шага"""
    step = callback.data.split("_")[1]
    
    if step == "route":
        await callback.message.edit_text(
            "📍 Введите маршрут: <code>Город - Город</code>",
            parse_mode="HTML"
        )
        await state.set_state(FlightSearch.route)
    
    elif step == "dates":
        await callback.message.edit_text(
            "📅 Введите дату вылета: <code>ДД.ММ</code>",
            parse_mode="HTML"
        )
        await state.set_state(FlightSearch.depart_date)
    
    elif step == "passengers":
        await ask_adults(callback, state)
    
    await callback.answer()