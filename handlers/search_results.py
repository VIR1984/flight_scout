# handlers/search_results.py
"""
Показ результатов поиска, обработка после поиска:
confirm_search, _do_confirm_search, watch_price, трансферы,
retry_with_transfers, edit_from_results, _show_no_flights.
"""
import json
import asyncio
import os
from uuid import uuid4

from aiogram import Router, F
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.fsm.context import FSMContext

from services.flight_search import (
    search_flights, search_flights_realtime,
    generate_booking_link, normalize_date, format_avia_link_date,
    find_cheapest_flight_on_exact_date, update_passengers_in_link, format_passenger_desc,
)
from services.transfer_search import search_transfers, generate_transfer_link
from utils.cities_loader import get_city_name, IATA_TO_CITY
from utils.redis_client import redis_client
from utils.logger import logger
from utils.link_converter import convert_to_partner_link
from utils.trip_link import build_trip_link, is_trip_supported
from utils.smart_reminder import cancel_inactivity, mark_fsm_inactive, remind_after_search, schedule_inactivity
from handlers.flight_constants import (
    CANCEL_KB, MULTI_AIRPORT_CITIES, AIRPORT_NAMES,
    SUPPORTED_TRANSFER_AIRPORTS, AIRLINE_NAMES,
)
from handlers.everywhere_search import (
    search_origin_everywhere, search_destination_everywhere,
    process_everywhere_search, format_user_date, build_passenger_desc,
)
from handlers.flight_fsm import (
    FlightSearch, _format_datetime, _format_duration, build_choices_summary, _get_metro,
)
from handlers.flight_wizard import show_summary
from handlers.billing import can_add_sub, show_paywall
from handlers.start import _SEARCH_SEMAPHORE

router = Router()

# Контекст трансферов: user_id → dict
transfer_context: dict[int, dict] = {}

@router.callback_query(FlightSearch.confirm, F.data == "confirm_search")
async def confirm_search(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    mark_fsm_inactive(callback.message.chat.id)
    data = await state.get_data()
    logger.info(f"[confirm_search] user={callback.from_user.id} маршрут={data.get('origin_iata')}→{data.get('dest_iata')}")
    await callback.message.edit_text("⏳ Ищу билеты...")
    async with _SEARCH_SEMAPHORE:
        await _do_confirm_search(callback, state, data)


async def _do_confirm_search(callback: CallbackQuery, state: FSMContext, data: dict):
    """Основная логика поиска. Вызывается внутри семафора."""
    is_origin_everywhere = data["origin"] == "везде"
    is_dest_everywhere   = data["dest"]   == "везде"
    # Режим "любой город в стране"
    is_dest_country   = str(data.get("dest",   "")).startswith("везде_") or bool(data.get("_country_dest_iatas"))
    is_origin_country = str(data.get("origin", "")).startswith("везде_") or bool(data.get("_country_origin_iatas"))
    flight_type    = data.get("flight_type", "all")
    direct_only    = flight_type == "direct"
    transfers_only = flight_type == "transfer"

    # ── Любой город в стране (назначение) ──────────────────────
    if is_dest_country and not is_origin_everywhere and not is_origin_country:
        country_iatas = data.get("_country_dest_iatas", [])
        country_name  = data.get("dest_name", "стране")
        origin_iata   = data.get("origin_iata", "")
        depart_date   = data.get("depart_date", "")

        # Ищем параллельно по всем городам страны
        tasks = [
            search_flights(origin_iata, dest, normalize_date(depart_date), None)
            for dest in country_iatas if dest != origin_iata
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_flights = []
        for dest, result in zip(
            [d for d in country_iatas if d != origin_iata], results
        ):
            if isinstance(result, Exception):
                continue
            for f in result:
                f["origin"] = origin_iata
                f["destination"] = dest
            all_flights.extend(result)

        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]

        # Обновляем dest на реальный город победителя перед показом
        if all_flights:
            cheapest = min(all_flights, key=lambda f: f.get("value") or f.get("price") or 999999)
            winner_iata = cheapest.get("destination", "")
            winner_name = get_city_name(winner_iata) or winner_iata
            await state.update_data(
                dest=winner_name, dest_iata=winner_iata, dest_name=winner_name,
                _country_dest_iatas=None,
            )
            data = await state.get_data()

        success = await process_everywhere_search(callback, data, all_flights, "destination_everywhere")
        if success:
            await state.clear()
        else:
            await callback.message.edit_text(
                f"😔 <b>Ничего не найдено</b>\n\nИз <b>{data.get('origin_name', '')}</b> в <b>{country_name}</b> "
                f"на <b>{data.get('depart_date', '')}</b> рейсов не нашлось.\n\n"
                "Попробуйте другую дату.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="start_search")],
                    [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
                ]),
            )
        return

    # ── Везде ──────────────────────────────────────────────────
    if is_origin_everywhere and not is_dest_everywhere:
        all_flights = await search_origin_everywhere(
            dest_iata=data["dest_iata"], depart_date=data["depart_date"],
            flight_type=flight_type,
        )
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, "origin_everywhere")
        if success:
            await state.clear()
        else:
            await callback.message.edit_text(
                f"😔 <b>Ничего не найдено</b>\n\nПо направлению <b>Везде → {data.get('dest_name', '')}</b> "
                f"на <b>{data.get('depart_date', '')}</b> рейсов не нашлось.\n\n"
                "Попробуйте другую дату или направление.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="start_search")],
                    [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
                ]),
            )
        return

    if not is_origin_everywhere and is_dest_everywhere:
        all_flights = await search_destination_everywhere(
            origin_iata=data["origin_iata"], depart_date=data["depart_date"],
            flight_type=flight_type,
        )
        if direct_only:
            all_flights = [f for f in all_flights if f.get("transfers", 999) == 0]
        elif transfers_only:
            all_flights = [f for f in all_flights if f.get("transfers", 0) > 0]
        success = await process_everywhere_search(callback, data, all_flights, "destination_everywhere")
        if success:
            await state.clear()
        else:
            await callback.message.edit_text(
                f"😔 <b>Ничего не найдено</b>\n\nИз <b>{data.get('origin_name', '')}</b> "
                f"на <b>{data.get('depart_date', '')}</b> рейсов не нашлось.\n\n"
                "Попробуйте другую дату или направление.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="start_search")],
                    [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
                ]),
            )
        return

    # ── Обычный поиск ──────────────────────────────────────────
    origins      = data.get("origin_airports") or [data["origin_iata"]]
    destinations = [data["dest_iata"]]
    all_flights  = []

    pax_code = data.get("passenger_code", "1")
    try:
        rt_adults   = int(pax_code[0])
        rt_children = int(pax_code[1]) if len(pax_code) > 1 else 0
        rt_infants  = int(pax_code[2]) if len(pax_code) > 2 else 0
    except (ValueError, IndexError):
        rt_adults, rt_children, rt_infants = 1, 0, 0

    # Прогресс-анимация
    progress_msg = await callback.message.edit_text("⏳ <b>Ищу билеты...</b>", parse_mode="HTML")

    async def _update_progress():
        await asyncio.sleep(10)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Запрашиваю актуальные цены...</b>\n<i>Получаю данные от авиакомпаний</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await asyncio.sleep(20)
        try:
            await progress_msg.edit_text(
                "⏳ <b>Почти готово...</b>\n<i>Сравниваю предложения</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass

    progress_task = asyncio.create_task(_update_progress())

    # Собираем все пары маршрутов
    _search_pairs = [(o, d) for o in origins for d in destinations if o != d]

    async def _fetch_pair(orig: str, dest: str):
        """Один запрос пары маршрут — вызывается параллельно через gather."""
        result = await search_flights_realtime(
            origin=orig, destination=dest,
            depart_date=normalize_date(data["depart_date"]),
            return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
            adults=rt_adults, children=rt_children, infants=rt_infants,
        )
        for f in result:
            f["origin"] = orig
            f["destination"] = dest
        return result

    try:
        # Все пары маршрутов запускаются ПАРАЛЛЕЛЬНО
        # Москва (SVO+DME+VKO+ZIA) → Бангкок = 4 запроса одновременно вместо последовательно
        results = await asyncio.gather(*[_fetch_pair(o, d) for o, d in _search_pairs], return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"[Search] Ошибка в паре: {r}")
                continue
            flights = r
            if direct_only:
                flights = [f for f in flights if f.get("transfers", 999) == 0]
            elif transfers_only:
                flights = [f for f in flights if f.get("transfers", 0) > 0]
            all_flights.extend(flights)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass

    logger.info(f"🔍 [Search] {len(all_flights)} рейсов от {set(f.get('_source') for f in all_flights)}")

    # ── Нет прямых → предлагаем с пересадками ──────────────────
    if direct_only and not all_flights:
        _fb_results = await asyncio.gather(*[
            search_flights_realtime(
                origin=o, destination=d,
                depart_date=normalize_date(data["depart_date"]),
                return_date=normalize_date(data["return_date"]) if data.get("return_date") else None,
                adults=rt_adults, children=rt_children, infants=rt_infants,
            )
            for o, d in _search_pairs
        ], return_exceptions=True)
        all_any = [f for r in _fb_results if not isinstance(r, Exception) for f in r]

        if all_any:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Показать рейсы с пересадками",
                                      callback_data="retry_with_transfers")],
                [InlineKeyboardButton(text="✏️ Изменить параметры", callback_data="back_to_summary")],
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
            ])
            await callback.message.edit_text(
                "😔 <b>Прямых рейсов на эти даты не найдено.</b>\n\nЕсть варианты с пересадками — они часто дешевле!",
                parse_mode="HTML", reply_markup=kb,
            )
        else:
            await _show_no_flights(callback, data, origins, destinations, pax_code)
        return

    # ── Вообще нет рейсов ───────────────────────────────────────
    if not all_flights:
        await _show_no_flights(callback, data, origins, destinations, pax_code)
        await state.clear()
        return

    # ── Сохраняем кэш и показываем результат ───────────────────
    cache_id       = str(uuid4())
    display_depart = format_user_date(data["depart_date"])
    display_return = format_user_date(data["return_date"]) if data.get("return_date") else None

    await redis_client.set_search_cache(cache_id, {
        "flights": all_flights,
        "origin": data.get("origin", ""), "origin_iata": data.get("origin_iata", ""),
        "origin_name": data.get("origin_name", ""),
        "dest": data.get("dest", ""),     "dest_iata": data["dest_iata"],
        "dest_name": data.get("dest_name", ""),
        "depart_date": data["depart_date"],       "return_date": data.get("return_date"),
        "need_return": data.get("need_return", False),
        "display_depart": display_depart,         "display_return": display_return,
        "original_depart": data["depart_date"],   "original_return": data.get("return_date"),
        "passenger_desc": data["passenger_desc"], "passengers_code": data["passenger_code"],
        "passenger_code": data["passenger_code"],
        "adults": data.get("adults", 1), "children": data.get("children", 0),
        "infants": data.get("infants", 0),
        "origin_everywhere": False, "dest_everywhere": False,
        "flight_type": flight_type,
    })

    # Трекинг: тип поиска и воронка
    import asyncio as _aio
    _aio.ensure_future(redis_client.track_search_type("normal"))
    _aio.ensure_future(redis_client.track_funnel_step("5_result_shown"))
    top_flight   = find_cheapest_flight_on_exact_date(all_flights, data["depart_date"], data.get("return_date"))
    price        = top_flight.get("value") or top_flight.get("price") or "?"
    origin_iata  = top_flight["origin"]
    dest_iata    = top_flight.get("destination") or data["dest_iata"]

    # origin_name — название города (не аэропорта).
    # Если top_flight вернул аэропорт (DME), ищем его город через metro (MOW → Москва).
    # Приоритет: data["origin_name"] → metro города → IATA_TO_CITY → сам IATA.
    def _city_name_for(iata: str, fallback_name: str) -> str:
        if fallback_name and fallback_name != "Везде":
            return fallback_name
        metro = _get_metro(iata)
        if metro:
            return IATA_TO_CITY.get(metro, IATA_TO_CITY.get(iata, iata))
        return IATA_TO_CITY.get(iata, iata)

    origin_name  = _city_name_for(origin_iata, data.get("origin_name", ""))
    dest_name    = _city_name_for(dest_iata,   data.get("dest_name",   ""))
    duration     = _format_duration(top_flight.get("duration", 0))
    transfers    = top_flight.get("transfers", 0)
    origin_airport = AIRPORT_NAMES.get(origin_iata, origin_iata)
    dest_airport   = AIRPORT_NAMES.get(dest_iata, dest_iata)

    if transfers == 0:   transfer_text = "Прямой рейс"
    elif transfers == 1: transfer_text = "1 пересадка"
    else:                transfer_text = f"{transfers} пересадки"

    price_per_pax = int(float(price)) if price != "?" else 0
    passengers_code = data.get("passenger_code", "1")
    try:
        num_adults = int(passengers_code[0])
    except (IndexError, ValueError):
        num_adults = 1
    estimated_total = price_per_pax * num_adults if price != "?" else "?"

    text = "<b>Лучший результат</b>\n"
    if price != "?":
        text += f"\n<b>{price_per_pax} ₽</b> за пассажира"
        if num_adults > 1:
            text += f"\n<i>~{estimated_total} ₽ за {num_adults} взрослых</i>"
    else:
        text += "\n<i>Цену уточни на Aviasales</i>"

    if data.get("children", 0) > 0 or data.get("infants", 0) > 0:
        text += "\n<i>(стоимость для детей/младенцев может рассчитываться по-другому)</i>"

    # Формируем строку рейса: Москва (Шереметьево (SVO)) → Сочи (Адлер (AER))
    # Если название аэропорта совпадает с городом — показываем просто город (IATA)
    def _route_part(city: str, airport: str, iata: str) -> str:
        if airport and airport.lower() != city.lower():
            return f"{city} ({airport} ({iata}))"
        return f"{city} ({iata})"

    route_str = (
        f"{_route_part(origin_name, origin_airport, origin_iata)}"
        f" → "
        f"{_route_part(dest_name, dest_airport, dest_iata)}"
    )

    text += (
        f"\n\n<b>Рейс:</b> {route_str}"
        f"\n<b>Туда:</b> {display_depart}"
    )
    if data.get("need_return") and display_return:
        text += f"\n<b>Обратно:</b> {display_return}"
    text += f"\n{duration} · {transfer_text}"

    airline       = top_flight.get("airline", "")
    flight_number = top_flight.get("flight_number", "")
    airline_name  = AIRLINE_NAMES.get(airline, "")  # пустая строка если код неизвестен
    if airline_name:
        text += f"\n✈️ <b>Авиакомпания:</b> {airline_name}"
    if airline and flight_number:
        text += f"\n🔢 <b>Рейс:</b> {airline} {flight_number}"

    booking_link = top_flight.get("link") or top_flight.get("deep_link")
    if booking_link:
        booking_link = update_passengers_in_link(booking_link, passengers_code)
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"
    else:
        booking_link = generate_booking_link(
            flight=top_flight, origin=origin_iata, dest=dest_iata,
            depart_date=data["depart_date"], passengers_code=passengers_code,
            return_date=data["return_date"] if data.get("need_return") else None,
        )
        if not booking_link.startswith(("http://", "https://")):
            booking_link = f"https://www.aviasales.ru{booking_link}"

    fallback_link = generate_booking_link(
        flight=top_flight, origin=origin_iata, dest=dest_iata,
        depart_date=data["depart_date"], passengers_code=passengers_code,
        return_date=data["return_date"] if data.get("need_return") else None,
    )
    if not fallback_link.startswith(("http://", "https://")):
        fallback_link = f"https://www.aviasales.ru{fallback_link}"

    # Оба запроса к Travelpayouts API параллельно — экономим ~1-2с
    booking_link, fallback_link = await asyncio.gather(
        convert_to_partner_link(booking_link, context="search_results"),
        convert_to_partner_link(fallback_link, context="search_results_fallback"),
    )

    kb_buttons = []
    if booking_link:
        kb_buttons.append([InlineKeyboardButton(text=f"🔍 Посмотреть детали за  {price_per_pax:,} ₽".replace(",", "\u202f"), url=booking_link)])
    kb_buttons.append([InlineKeyboardButton(text="Все варианты на эти даты", url=fallback_link)])

    # Кнопка Trip.com — альтернативная площадка
    _trip_url = build_trip_link(
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data["depart_date"],
        passengers_code=passengers_code,
        return_date=data.get("return_date") if data.get("need_return") else None,
    )
    if _trip_url and is_trip_supported(origin_iata, dest_iata):
        kb_buttons.append([InlineKeyboardButton(text="🌐 Сравнить на Trip.com", url=_trip_url)])

    kb_buttons.append([InlineKeyboardButton(text="Следить за ценой", callback_data=f"watch_all_{cache_id}")])
    kb_buttons.append([InlineKeyboardButton(text="Изменить запрос", callback_data=f"edit_from_results_{cache_id}")])

    if dest_iata in SUPPORTED_TRANSFER_AIRPORTS:
        transfer_link = os.getenv("GETTRANSFER_LINK", "https://gettransfer.tpx.gr/Rr2KJIey?erid=2VtzqwJZYS7")
        kb_buttons.insert(-1, [
            InlineKeyboardButton(text=f"🚖 Трансфер в {dest_name}", url=transfer_link)
        ])

    kb_buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await state.clear()
    await callback.answer()

    # Умное напоминание — предложим вау-цены через 15 минут если ещё не подписаны
    asyncio.create_task(
        remind_after_search(callback.message.chat.id, callback.from_user.id, delay_min=15)
    )


async def _show_no_flights(callback: CallbackQuery, data: dict,
                            origins: list, destinations: list, pax_code: str):
    """Показать экран 'билеты не найдены' со ссылками на Aviasales и Trip.com."""
    for orig in origins:
        for dest in destinations:
            asyncio.create_task(redis_client.track_no_results(
                orig, dest, data.get("depart_date", "")
            ))

    origin_iata = origins[0] if origins else data.get("origin_iata", "MOW")
    dest_iata   = destinations[0] if destinations else data.get("dest_iata", "")
    d1 = format_avia_link_date(data["depart_date"])
    d2 = format_avia_link_date(data["return_date"]) if data.get("return_date") else ""
    route        = f"{origin_iata}{d1}{dest_iata}{d2}{pax_code}"
    partner_link = await convert_to_partner_link(f"https://www.aviasales.ru/search/{route}")

    # Сохраняем данные в кэш, чтобы "Изменить маршрут" работало даже после state.clear()
    cache_id = str(uuid4())
    await redis_client.set_search_cache(cache_id, {
        "flights": [],
        "origin":         data.get("origin", ""),
        "origin_iata":    data.get("origin_iata", ""),
        "origin_name":    data.get("origin_name", ""),
        "dest":           data.get("dest", ""),
        "dest_iata":      dest_iata,
        "dest_name":      data.get("dest_name", ""),
        "depart_date":    data.get("depart_date", ""),
        "return_date":    data.get("return_date"),
        "need_return":    data.get("need_return", False),
        "flight_type":    data.get("flight_type", "all"),
        "adults":         data.get("adults", 1),
        "children":       data.get("children", 0),
        "infants":        data.get("infants", 0),
        "passenger_code": data.get("passenger_code", pax_code),
        "passenger_desc": data.get("passenger_desc", ""),
        "original_depart": data.get("depart_date", ""),
        "original_return": data.get("return_date"),
    })

    kb_buttons = [
        [InlineKeyboardButton(text="🔍 Поискать на Aviasales", url=partner_link)],
    ]

    # Кнопка Trip.com на экране "не найдено"
    _trip_url = build_trip_link(
        origin=origin_iata,
        dest=dest_iata,
        depart_date=data.get("depart_date", ""),
        passengers_code=pax_code,
        return_date=data.get("return_date") if data.get("need_return") else None,
    )
    if _trip_url and is_trip_supported(origin_iata, dest_iata):
        kb_buttons.append([InlineKeyboardButton(text="🌐 Поискать на Trip.com", url=_trip_url)])

    kb_buttons.append([InlineKeyboardButton(text="✏️ Изменить маршрут", callback_data=f"edit_from_results_{cache_id}")])
    kb_buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    await callback.message.edit_text(
        "😔 <b>Билеты не найдены.</b>\n\nПопробуйте изменить даты или маршрут.",
        parse_mode="HTML", reply_markup=kb,
    )


# ════════════════════════════════════════════════════════════════
# Callback-хендлеры результатов
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "retry_with_transfers")
async def retry_with_transfers(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    if not data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели. Выполните новый поиск.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Найти билеты", callback_data="start_search")]
            ]),
        )
        await callback.answer()
        return
    await state.update_data(flight_type="all")
    await confirm_search(callback, state)
    await callback.answer()


# Обратная совместимость
@router.callback_query(F.data.startswith("retry_with_transfers_"))
async def retry_with_transfers_legacy(callback: CallbackQuery, state: FSMContext):
    await retry_with_transfers(callback, state)


@router.callback_query(F.data == "back_to_summary")
async def back_to_summary(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    data = await state.get_data()
    if not data or "depart_date" not in data:
        await callback.message.edit_text(
            "😔 Данные поиска устарели.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✈️ Новый поиск", callback_data="start_search")]
            ]),
        )
        await callback.answer()
        return
    summary = "Проверьте даты и данные:\n\n" + build_choices_summary(data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",    callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",        callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",   callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",   callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
    ])
    await callback.message.edit_text(summary, parse_mode="HTML")
    await callback.message.answer("Подтвердите или измените параметры:", reply_markup=kb)
    await state.set_state(FlightSearch.confirm)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_from_results_"))
async def edit_from_results(callback: CallbackQuery, state: FSMContext):
    cancel_inactivity(callback.message.chat.id)
    cache_id = callback.data.replace("edit_from_results_", "")
    cached   = await redis_client.get_search_cache(cache_id)
    if not cached:
        await callback.answer("Данные устарели, начните новый поиск", show_alert=True)
        return

    cached.pop("flights", None)
    fsm_data = {
        "origin":         cached.get("origin", ""),
        "origin_iata":    cached.get("origin_iata", ""),
        "origin_name":    cached.get("origin_name", ""),
        "dest":           cached.get("dest", ""),
        "dest_iata":      cached.get("dest_iata", ""),
        "dest_name":      cached.get("dest_name", ""),
        "depart_date":    cached.get("depart_date") or cached.get("original_depart", ""),
        "return_date":    cached.get("return_date") or cached.get("original_return"),
        "need_return":    cached.get("need_return", False),
        "flight_type":    cached.get("flight_type", "all"),
        "adults":         cached.get("adults", 1),
        "children":       cached.get("children", 0),
        "infants":        cached.get("infants", 0),
        "passenger_code": cached.get("passenger_code") or cached.get("passengers_code", "1"),
        "passenger_desc": cached.get("passenger_desc", "1 взр."),
        "_edit_mode":     False,
    }
    await state.update_data(**fsm_data)
    await state.set_state(FlightSearch.confirm)

    summary = "Проверьте даты и данные:\n\n" + build_choices_summary(fsm_data)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_search")],
        [InlineKeyboardButton(text="✏️ Маршрут",    callback_data="edit_route"),
         InlineKeyboardButton(text="✏️ Даты",        callback_data="edit_dates")],
        [InlineKeyboardButton(text="✏️ Тип рейса",   callback_data="edit_flight_type"),
         InlineKeyboardButton(text="✏️ Пассажиры",   callback_data="edit_passengers")],
        [InlineKeyboardButton(text="↩️ В начало",    callback_data="main_menu")],
    ])
    await callback.message.edit_text(summary, parse_mode="HTML", reply_markup=kb)
    schedule_inactivity(callback.message.chat.id, callback.from_user.id)
    await callback.answer()


# ════════════════════════════════════════════════════════════════
# Слежение за ценой
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("watch_"))
async def handle_watch_price(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    parts = callback.data.split("_")

    if parts[1] == "all":
        cache_id = parts[2]
        data = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        is_origin_everywhere = data.get("origin_everywhere", False)
        is_dest_everywhere   = data.get("dest_everywhere", False)
        flights = data["flights"]
        if is_dest_everywhere:
            origin, dest = flights[0]["origin"], None
        elif is_origin_everywhere:
            origin = None
            dest   = data.get("dest_iata") or flights[0].get("destination")
        else:
            origin = flights[0]["origin"]
            dest   = data.get("dest_iata") or flights[0].get("destination")
        min_flight  = min(flights, key=lambda f: f.get("value") or f.get("price") or 999999)
        price       = int(float(min_flight.get("value") or min_flight.get("price") or 0))
        depart_date = data["original_depart"]
        return_date = data.get("original_return") or data.get("return_date")
    else:
        cache_id = parts[1]
        price    = int(parts[2])
        data     = await redis_client.get_search_cache(cache_id)
        if not data:
            await callback.answer("Данные устарели", show_alert=True)
            return
        top  = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
        origin      = top["origin"]
        dest        = data.get("dest_iata") or top.get("destination")
        depart_date = data["original_depart"]
        return_date = data.get("original_return") or data.get("return_date")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Любое изменение",            callback_data=f"set_threshold:0:{cache_id}:{price}")],
        [InlineKeyboardButton(text="Изменение на 100 ₽ и больше",  callback_data=f"set_threshold:100:{cache_id}:{price}")],
        [InlineKeyboardButton(text="Изменение на 1 000 ₽ и больше", callback_data=f"set_threshold:1000:{cache_id}:{price}")],
        [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
    ])
    await callback.message.answer("<b>Когда уведомлять об изменении цены?</b>", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("set_threshold:"))
async def handle_set_threshold(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    _, threshold_str, cache_id, price_str = callback.data.split(":", 3)
    threshold = int(threshold_str)
    price     = int(float(price_str))

    data = await redis_client.get_search_cache(cache_id)
    if not data:
        await callback.answer("Данные устарели", show_alert=True)
        return

    is_origin_everywhere = data.get("origin_everywhere", False)
    is_dest_everywhere   = data.get("dest_everywhere",   False)

    top    = min(data["flights"], key=lambda f: f.get("value") or f.get("price") or 999999)
    origin = None if is_origin_everywhere else (top.get("origin") or data.get("origin_iata", ""))
    dest   = None if is_dest_everywhere   else (data.get("dest_iata") or top.get("destination", ""))

    # ── Проверяем лимит тарифа ────────────────────────────────
    ok, reason = await can_add_sub(
        callback.from_user.id, "watch", callback.from_user.username
    )
    if not ok:
        await show_paywall(callback, reason)
        return
    # ────────────────────────────────────────────────────────────

    await redis_client.save_price_watch(
        user_id=callback.from_user.id,
        origin=origin,
        dest=dest,
        depart_date=data["original_depart"],
        return_date=data.get("original_return") or data.get("return_date"),
        current_price=price,
        passengers=data.get("passenger_code") or data.get("passengers_code", "1"),
        threshold=threshold,
    )
    import asyncio as _aio
    _aio.ensure_future(redis_client.track_subscription_event("price_watch", "created"))

    origin_name = "Везде" if is_origin_everywhere else (IATA_TO_CITY.get(origin, origin) if origin else "—")
    dest_name   = "Везде" if is_dest_everywhere   else (IATA_TO_CITY.get(dest, dest)     if dest   else "—")
    condition   = {0: "любом изменении", 100: "изменении на сотни ₽", 1000: "изменении на тысячи ₽"}.get(threshold, "изменении цены")

    response = (
        f"✅ <b>Слежение активировано</b>\n\n"
        f"<b>Маршрут:</b> {origin_name} → {dest_name}\n"
        f"<b>Вылет:</b> {data.get('display_depart', '')}\n"
    )
    if data.get("display_return"):
        response += f"<b>Обратно:</b> {data['display_return']}\n"
    response += (
        f"Цена сейчас: <b>{price} ₽</b>\n"
        f"<i>Уведомлю при {condition}</i>\n\n"
        "Управляй слежением в разделе <b>Подписки</b>."
    )
    await callback.message.edit_text(
        response, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Мои подписки", callback_data="subs_section_watches")],
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("unwatch_"))
async def handle_unwatch(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    key     = callback.data.split("unwatch_")[1]
    user_id = callback.from_user.id
    if f":{user_id}:" not in key:
        await callback.answer("Это не твоё отслеживание.", show_alert=True)
        return
    await redis_client.remove_watch(user_id, key)
    await callback.answer("Слежение удалено")
    # Показываем обновлённый список слежений или меню подписок
    watches = await redis_client.get_user_watches(user_id)
    if watches:
        from handlers.subscriptions import cb_section_watches
        # Эмулируем callback для обновления списка
        try:
            await callback.message.edit_text(
                "Слежение удалено.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Все отслеживания", callback_data="subs_section_watches")],
                    [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
                ])
            )
        except Exception:
            pass
    else:
        await callback.message.edit_text(
            "Слежение удалено.\n\nАктивных отслеживаний нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои подписки", callback_data="subs_menu")],
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")],
            ]),
        )


# ════════════════════════════════════════════════════════════════
# Трансфер
# ════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("ask_transfer_"))
async def handle_ask_transfer(callback: CallbackQuery):
    cancel_inactivity(callback.message.chat.id)
    user_id = callback.from_user.id
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return
    airport_iata = context["airport_iata"]
    airport_name = AIRPORT_NAMES.get(airport_iata, airport_iata)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, покажи варианты", callback_data=f"show_transfer_{user_id}")],
        [InlineKeyboardButton(text="❌ Нет, спасибо",        callback_data=f"decline_transfer_{user_id}")],
        [InlineKeyboardButton(text="↩️ В начало",            callback_data="main_menu")],
    ])
    await callback.message.answer(
        f"🚖 <b>Нужен трансфер из аэропорта {airport_name}?</b>\n"
        "Я могу найти для вас варианты трансфера по лучшим ценам.\nПоказать предложения?",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("decline_transfer_"))
async def handle_decline_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    transfer_context.pop(user_id, None)
    if redis_client.client:
        await redis_client.client.setex(f"declined_transfer:{user_id}", 86400 * 7, "1")
    await callback.message.edit_text(
        "Хорошо! Если передумаете — просто выполните новый поиск билетов. ✈️",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("show_transfer_"))
async def handle_show_transfer(callback: CallbackQuery):
    user_id = callback.from_user.id
    if redis_client.client:
        if await redis_client.client.get(f"declined_transfer:{user_id}"):
            await callback.answer(
                "Ты недавно отказался от трансферов. Предложения снова появятся через несколько дней.",
                show_alert=True,
            )
            return
    context = transfer_context.get(user_id)
    if not context:
        await callback.answer("Данные устарели, пожалуйста, выполните поиск заново", show_alert=True)
        return

    await callback.message.edit_text("Ищу варианты трансфера... 🚖")
    transfers = await search_transfers(airport_iata=context["airport_iata"], transfer_date=context["transfer_date"], adults=1)

    if not transfers:
        await callback.message.edit_text(
            "К сожалению, трансферы для этого аэропорта временно недоступны. 😢\n"
            "Попробуйте позже или забронируйте на сайте напрямую.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")]
            ]),
        )
        return

    airport_name = AIRPORT_NAMES.get(context["airport_iata"], context["airport_iata"])
    msg = (
        f"🚀 <b>Варианты трансфера {context['depart_date']}</b>\n"
        f"📍 <b>{airport_name}</b> → центр города\n"
    )
    buttons = []
    for i, t in enumerate(transfers[:3], 1):
        price    = t.get("price", 0)
        vehicle  = t.get("vehicle", "Economy")
        duration = t.get("duration_minutes", 0)
        msg += f"\n<b>{i}. {vehicle}</b>\n💰 {price} ₽\n⏱️ ~{duration} мин в пути"
        tlink = generate_transfer_link(
            transfer_id=str(t.get("id", "")),
            marker=os.getenv("TRAFFIC_SOURCE", ""),
            sub_id=f"telegram_{user_id}",
        )
        buttons.append([InlineKeyboardButton(text=f"🚀 Вариант {i}: {price} ₽", url=tlink)])
    buttons.append([InlineKeyboardButton(text="↩️ В начало", callback_data="main_menu")])
    await callback.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# ════════════════════════════════════════════════════════════════