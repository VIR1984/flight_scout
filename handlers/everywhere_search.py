import logging
from typing import List, Dict, Optional
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from services.flight_search import search_flights, validate_date, format_avia_link_date
from utils.cities import CITY_TO_IATA, IATA_TO_CITY, GLOBAL_HUBS
from utils.logger import logger
from utils.redis_client import redis_client
from utils.link_converter import convert_to_partner_link

async def search_origin_everywhere(
    dest_iata: str,
    depart_date: str,
    return_date: Optional[str] = None,
    passengers_code: str = "1"
) -> List[Dict]:
    """Ищет рейсы из всех городов России в указанный город"""
    logger.info(f"[search_origin_everywhere] Поиск из всех городов в {dest_iata} на даты {depart_date}/{return_date}")
    
    all_flights = []
    
    # Используем только российские города из нашего справочника
    russian_cities = [iata for iata, city in IATA_TO_CITY.items() if "россия" in city.lower()]
    
    # Добавляем популярные города из GLOBAL_HUBS, если они есть в справочнике
    for city in GLOBAL_HUBS:
        iata = CITY_TO_IATA.get(city.lower())
        if iata and iata not in russian_cities:
            russian_cities.append(iata)
    
    # Убираем целевой город из списка отправления
    if dest_iata in russian_cities:
        russian_cities.remove(dest_iata)
    
    logger.debug(f"[search_origin_everywhere] Будем искать из {len(russian_cities)} городов")
    
    # Ищем рейсы из каждого города
    for i, origin_iata in enumerate(russian_cities):
        if i % 5 == 0:  # Чтобы не спамить логами
            logger.debug(f"[search_origin_everywhere] Проверяем город {i+1}/{len(russian_cities)}: {origin_iata}")
        
        try:
            flights = await search_flights(
                origins=[origin_iata],
                destinations=[dest_iata],
                depart_date=depart_date,
                return_date=return_date,
                passengers_code=passengers_code
            )
            
            for flight in flights:
                # Добавляем информацию об отправлении для идентификации
                flight["origin"] = origin_iata
                flight["destination"] = dest_iata
                all_flights.append(flight)
                
            if len(all_flights) >= 15:  # Ограничиваем количество результатов
                break
                
        except Exception as e:
            logger.error(f"[search_origin_everywhere] Ошибка поиска из {origin_iata}: {e}")
            continue
    
    # Сортируем по цене
    all_flights.sort(key=lambda x: x.get('price', float('inf')))
    
    logger.info(f"[search_origin_everywhere] Найдено {len(all_flights)} рейсов из всех городов")
    return all_flights[:10]  # Возвращаем топ-10 самых дешевых

async def search_dest_everywhere(
    origin_iata: str,
    depart_date: str,
    return_date: Optional[str] = None,
    passengers_code: str = "1"
) -> List[Dict]:
    """Ищет рейсы из указанного города во все популярные города мира"""
    logger.info(f"[search_dest_everywhere] Поиск из {origin_iata} во все популярные города на даты {depart_date}/{return_date}")
    
    all_flights = []
    
    # Используем популярные города из GLOBAL_HUBS и IATA_TO_CITY
    destinations = list(GLOBAL_HUBS) + list(IATA_TO_CITY.keys())
    destination_iatas = []
    
    for city in destinations:
        if isinstance(city, str) and len(city) == 3:  # Это уже IATA код
            iata = city
        else:
            iata = CITY_TO_IATA.get(city.lower())
        
        if iata and iata != origin_iata and iata not in destination_iatas:
            destination_iatas.append(iata)
    
    logger.debug(f"[search_dest_everywhere] Будем искать в {len(destination_iatas)} направлениях")
    
    # Ищем рейсы в каждый город
    for i, dest_iata in enumerate(destination_iatas):
        if i % 5 == 0:  # Чтобы не спамить логами
            logger.debug(f"[search_dest_everywhere] Проверяем направление {i+1}/{len(destination_iatas)}: {dest_iata}")
        
        try:
            flights = await search_flights(
                origins=[origin_iata],
                destinations=[dest_iata],
                depart_date=depart_date,
                return_date=return_date,
                passengers_code=passengers_code
            )
            
            for flight in flights:
                # Добавляем информацию о прибытии для идентификации
                flight["origin"] = origin_iata
                flight["destination"] = dest_iata
                all_flights.append(flight)
                
            if len(all_flights) >= 15:  # Ограничиваем количество результатов
                break
                
        except Exception as e:
            logger.error(f"[search_dest_everywhere] Ошибка поиска в {dest_iata}: {e}")
            continue
    
    # Сортируем по цене
    all_flights.sort(key=lambda x: x.get('price', float('inf')))
    
    logger.info(f"[search_dest_everywhere] Найдено {len(all_flights)} рейсов во все направления")
    return all_flights[:10]  # Возвращаем топ-10 самых дешевых

async def handle_everywhere_search(
    message: Message,
    state: FSMContext,
    is_origin_everywhere: bool = False,
    is_dest_everywhere: bool = False
):
    """Обработка поиска с 'везде' через FSM"""
    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return
    
    # ДОБАВЛЕНО: Отображаем выбор пользователя
    if is_origin_everywhere:
        await message.answer("📍 Выбрано: Все города России → Конкретный город", parse_mode="HTML")
    else:
        await message.answer("📍 Выбрано: Конкретный город → Все популярные города", parse_mode="HTML")
    
    await message.answer(
        "✈️ Теперь укажите дату вылета в формате `ДД.ММ`",
        parse_mode="HTML",
        reply_markup=CANCEL_KB
    )
    await state.set_state(FlightSearch.depart_date)
    
    # Сохраняем тип поиска
    await state.update_data(
        is_origin_everywhere=is_origin_everywhere,
        is_dest_everywhere=is_dest_everywhere
    )

async def handle_everywhere_search_manual(
    message: Message,
    origin_city: str,
    dest_city: str,
    depart_date: str,
    return_date: Optional[str],
    passengers_code: str,
    is_origin_everywhere: bool,
    is_dest_everywhere: bool
) -> bool:
    """Обработка поиска с 'везде' в ручном режиме"""
    if is_origin_everywhere and is_dest_everywhere:
        await message.answer(
            "❌ Нельзя искать «Везде → Везде».\n"
            "Укажите хотя бы один конкретный город.",
            reply_markup=CANCEL_KB
        )
        return False
    
    # ДОБАВЛЕНО: Отображаем выбор пользователя
    if is_origin_everywhere:
        await message.answer(f"📍 Выбрано: Все города России → {dest_city.strip().capitalize()}", parse_mode="HTML")
    else:
        await message.answer(f"📍 Выбрано: {origin_city.strip().capitalize()} → Все популярные города", parse_mode="HTML")
    
    if is_origin_everywhere:
        dest_iata = CITY_TO_IATA.get(dest_city.strip())
        if not dest_iata:
            await message.answer(f"❌ Не знаю город прилёта: {dest_city.strip()}", reply_markup=CANCEL_KB)
            return False
        
        all_flights = await search_origin_everywhere(
            dest_iata=dest_iata,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code
        )
        search_type = "origin_everywhere"
    else:  # is_dest_everywhere
        origin_iata = CITY_TO_IATA.get(origin_city.strip())
        if not origin_iata:
            await message.answer(f"❌ Не знаю город отправления: {origin_city.strip()}", reply_markup=CANCEL_KB)
            return False
        
        all_flights = await search_dest_everywhere(
            origin_iata=origin_iata,
            depart_date=depart_date,
            return_date=return_date,
            passengers_code=passengers_code
        )
        search_type = "dest_everywhere"
    
    if not all_flights:
        await message.answer(
            "😔 К сожалению, рейсов не найдено. Попробуйте изменить даты.",
            reply_markup=CANCEL_KB
        )
        return False
    
    # Формирование и отправка результатов
    response = []
    for i, flight in enumerate(all_flights[:3], 1):
        price = flight['price']
        origin = flight['origin']
        dest = flight['destination']
        origin_name = IATA_TO_CITY.get(origin, origin)
        dest_name = IATA_TO_CITY.get(dest, dest)
        response.append(
            f"{i}. {price} ₽ • {origin_name} → {dest_name}\n"
            f"   {flight['airline']} • {flight['departure']} → {flight['arrival']}"
        )
    
    if return_date:
        dates_text = f"{depart_date} → {return_date}"
    else:
        dates_text = depart_date
    
    if is_origin_everywhere:
        header = f"✈️ Из всех городов → {dest_city.strip().capitalize()} | {dates_text}"
    else:
        header = f"✈️ {origin_city.strip().capitalize()} → Все популярные города | {dates_text}"
    
    response_text = (
        f"{header}\n\n" +
        "\n".join(response) +
        "\n\n🔔 Установите уведомление о снижении цены!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Уведомить о снижении цены", callback_data="watch_price")],
        [InlineKeyboardButton(text="🔄 Изменить поиск", callback_data="edit_search")]
    ])
    
    # ИСПРАВЛЕНО: используем answer() вместо edit_text()
    await message.answer(response_text, parse_mode="Markdown", reply_markup=kb)
    return True