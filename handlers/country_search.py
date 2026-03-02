# handlers/country_search.py
"""
Логика выбора города когда пользователь вводит страну.
Показывает топ-4 города + "Ввести свой" + "Любой в стране".
Экспортирует: router, _ask_country_city (вызывается из flight_wizard)
"""
from aiogram import Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.context import FSMContext

from utils.cities_loader import (
    get_iata, get_city_name, fuzzy_get_iata,
    CITY_TO_IATA, _normalize_name,
    COUNTRY_TOP_CITIES, COUNTRY_NAME_TO_ISO,
)
from utils.smart_reminder import schedule_inactivity
from handlers.flight_constants import CANCEL_KB
from handlers.flight_fsm import (
    FlightSearch, _get_metro, _has_multi_airports, _airport_keyboard,
)

router = Router()

# ════════════════════════════════════════════════════════════════
# Выбор города из страны
# ════════════════════════════════════════════════════════════════

async def _ask_country_city(
    message: Message,
    state: FSMContext,
    country_name: str,
    cities: list,
    role: str,  # "origin" или "dest"
):
    """Показывает топ-города страны + 'Ввести свой' + 'Любой город' + 'Отменить'."""
    # Получаем все IATA городов страны для режима "Любой"
    iso = COUNTRY_NAME_TO_ISO.get(country_name.lower().strip().replace("ё", "е"))
    all_country_iatas = COUNTRY_TOP_CITIES.get(iso, []) if iso else [c["iata"] for c in cities]

    await state.update_data(
        _country_role=role,
        _country_name=country_name,
        _country_iatas=all_country_iatas,
    )
    await state.set_state(FlightSearch.choose_country_city)

    prompt = "вылета" if role == "origin" else "назначения"
    text = (
        f"🌍 <b>{country_name.capitalize()}</b> — популярные города {prompt}:\n\n"
        f"Выберите город или найдите самый дешёвый билет по всей стране."
    )

    buttons = []
    for i, city in enumerate(cities, 1):
        buttons.append([InlineKeyboardButton(
            text=f"{i}. {city['name']}",
            callback_data=f"cc_{role}_{city['iata']}",
        )])
    buttons.append([InlineKeyboardButton(
        text=f"{len(cities) + 1}. ✏️ Ввести свой город",
        callback_data=f"cc_{role}_custom",
    )])
    buttons.append([InlineKeyboardButton(
        text=f"{len(cities) + 2}. 🔍 Любой город в {country_name}",
        callback_data=f"cc_{role}_any",
    )])
    buttons.append([InlineKeyboardButton(
        text="✖ Отменить поиск",
        callback_data="main_menu",
    )])

    await message.answer(text, parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(FlightSearch.choose_country_city, F.data.startswith("cc_"))
async def process_country_city_pick(callback: CallbackQuery, state: FSMContext):
    """Пользователь нажал на один из городов страны."""
    parts = callback.data.split("_", 2)  # cc_{role}_{iata|custom|any}
    role  = parts[1]  # "origin" или "dest"
    value = parts[2]  # IATA или "custom" или "any"

    if value == "custom":
        # Просим ввести город вручную
        prompt = "отправления" if role == "origin" else "назначения"
        await callback.message.edit_text(
            f"Введите город {prompt}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
            ])
        )
        await state.update_data(_country_custom_role=role)
        await state.set_state(FlightSearch.choose_country_city)
        await callback.answer()
        return

    if value == "any":
        # Режим "Любой город в стране" — ищем как везде, но только по городам страны
        data = await state.get_data()
        country_name = data.get("_country_name", "стране")
        country_iatas = data.get("_country_iatas", [])

        await callback.answer()
        await callback.message.edit_text(
            f"🔍 Буду искать самый дешёвый рейс по всей <b>{country_name}</b>",
            parse_mode="HTML",
        )

        if role == "dest":
            await state.update_data(
                dest=f"везде_{country_name}",
                dest_iata=None,
                dest_name=country_name,
                _country_dest_iatas=country_iatas,
                _country_role=None, _country_custom_role=None,
            )
            await _finalize_route(callback.message, state)
        else:
            await state.update_data(
                origin=f"везде_{country_name}",
                origin_iata=None,
                origin_name=country_name,
                _country_origin_iatas=country_iatas,
                _country_role=None, _country_custom_role=None,
            )
            dest_val = data.get("dest", "")
            if not data.get("dest_iata") and dest_val != "везде":
                await callback.message.answer(
                    "Теперь введите <b>город назначения</b>:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                    ])
                )
                await state.update_data(_country_custom_role="dest")
            else:
                await _finalize_route(callback.message, state)
        return

    # Конкретный город выбран — запоминаем и идём дальше
    city_name = get_city_name(value) or value
    await callback.answer()
    await callback.message.edit_text(
        f"{'Город вылета' if role == 'origin' else 'Город назначения'}: <b>{city_name}</b>",
        parse_mode="HTML",
    )

    data = await state.get_data()

    if role == "origin":
        await state.update_data(
            origin=city_name, origin_iata=value, origin_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        dest_val = data.get("dest", "")
        if not data.get("dest_iata") and dest_val != "везде":
            await callback.message.answer(
                f"Отлично! Теперь введите <b>город назначения</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
            await state.set_state(FlightSearch.choose_country_city)
            await state.update_data(_country_custom_role="dest")
        else:
            await _finalize_route(callback.message, state)
    else:  # dest
        await state.update_data(
            dest=city_name, dest_iata=value, dest_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        await _finalize_route(callback.message, state)


@router.message(FlightSearch.choose_country_city)
async def process_country_city_text(message: Message, state: FSMContext):
    """Пользователь написал свой город вместо нажатия кнопки."""
    data  = await state.get_data()
    role  = data.get("_country_custom_role") or data.get("_country_role", "dest")
    city  = message.text.strip()

    iata = get_iata(city) or CITY_TO_IATA.get(_normalize_name(city))
    if not iata:
        # Fuzzy
        fuzzy_iata, fuzzy_name = fuzzy_get_iata(city)
        if fuzzy_iata:
            await message.answer(
                f"❓ Не нашёл «{city}» — вы имели в виду <b>{fuzzy_name}</b>?\n"
                f"Напишите название ещё раз.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
        else:
            await message.answer(
                f"❌ Город «{city}» не найден. Проверьте написание и попробуйте ещё раз.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
        return

    city_name = get_city_name(iata) or city.capitalize()

    if role == "origin":
        await state.update_data(
            origin=city_name, origin_iata=iata, origin_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        dest_val = data.get("dest", "")
        if not data.get("dest_iata") and dest_val != "везде":
            await message.answer(
                f"✅ Город вылета: <b>{city_name}</b>\n\nТеперь введите <b>город назначения</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✖ Отменить поиск", callback_data="main_menu")]
                ])
            )
            await state.update_data(_country_custom_role="dest")
        else:
            await _finalize_route(message, state)
    else:
        await state.update_data(
            dest=city_name, dest_iata=iata, dest_name=city_name,
            _country_role=None, _country_custom_role=None,
        )
        await _finalize_route(message, state)


async def _finalize_route(target, state: FSMContext):
    """После выбора обоих городов — проверки и переход к следующему шагу."""
    data = await state.get_data()
    orig_iata   = data.get("origin_iata")
    dest_iata   = data.get("dest_iata")
    origin_name = data.get("origin_name", "")
    dest_name   = data.get("dest_name", "")

    msg = target if isinstance(target, Message) else target

    if orig_iata and dest_iata and orig_iata == dest_iata:
        await msg.answer(
            "❌ Город вылета и прибытия не могут совпадать. Выберите разные города.",
            reply_markup=CANCEL_KB,
        )
        await state.set_state(FlightSearch.route)
        return

    await state.set_state(FlightSearch.route)

    if orig_iata and _has_multi_airports(orig_iata):
        metro = _get_metro(orig_iata) or orig_iata
        await state.update_data(origin_iata=metro)
        await state.set_state(FlightSearch.choose_airport)
        kb = _airport_keyboard(metro)
        await msg.answer(
            f"Вы выбрали: <b>{origin_name}</b>\n\nИз {origin_name} летят из нескольких аэропортов — выберите нужный:",
            parse_mode="HTML", reply_markup=kb,
        )
    else:
        await state.set_state(FlightSearch.depart_date)
        await msg.answer(
            "Введите дату вылета в формате <code>ДД.ММ</code>\n<i>Пример: 10.03</i>",
            parse_mode="HTML", reply_markup=CANCEL_KB,
        )
        from utils.inactivity import schedule_inactivity
        schedule_inactivity(msg.chat.id, msg.from_user.id if hasattr(msg, 'from_user') else 0)