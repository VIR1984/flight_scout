# services/flystack_client.py
import os
import aiohttp

_fs_connector: aiohttp.TCPConnector | None = None  # noqa

class FlyStackClient:
    """Клиент для FlyStack API с полным набором методов"""
    
    def __init__(self):
        self.api_key = API_KEY
        self.base_url = FLYSTACK_BASE_URL
        self.logger = logger
    
    async def _request(self, endpoint: str, params: dict = None) -> dict | None:
        """Базовый метод для HTTP запросов с полным логированием"""
        self.logger.info(f"🔍 [FlyStack] Запрос к API: {endpoint}")
        self.logger.debug(f"📝 [FlyStack] Параметры запроса: {params}")
        
        if not self.api_key:
            self.logger.warning("⚠️ [FlyStack] FLYSTACK_API_KEY не установлен")
            return None
        
        url = f"{self.base_url}/{endpoint}"
        params = params or {}
        params["api_key"] = self.api_key
        
        self.logger.debug(f"🌐 [FlyStack] Формируем URL: {url}")
        self.logger.debug(f"🔑 [FlyStack] Используем API ключ: {self.api_key[:4]}...{self.api_key[-4:]}")
        
        try:
            async with _make_flystackclientsession() as session:
                self.logger.info(f"📡 [FlyStack] Отправляем запрос к {url}")
                async with session.get(url, params=params, timeout=10) as resp:
                    self.logger.info(f"📥 [FlyStack] Получен ответ: {resp.status}")
                    
                    if resp.status == 429:
                        self.logger.warning("⚠️ [FlyStack] Превышен лимит FlyStack API")
                        return {"error": "rate_limit"}
                    if resp.status != 200:
                        error_text = await resp.text()
                        self.logger.error(f"❌ [FlyStack] Ошибка API {resp.status}: {error_text}")
                        return None
                    
                    data = await resp.json()
                    self.logger.info(f"✅ [FlyStack] Данные получены успешно")
                    self.logger.debug(f"📊 [FlyStack] Ответ API: {data}")
                    return data.get("data") or data
        except aiohttp.ClientError as e:
            self.logger.error(f"❌ [FlyStack] Ошибка соединения с FlyStack: {str(e)}")
            return None
        except Exception as e:
            self.logger.exception(f"❌ [FlyStack] Неизвестная ошибка: {str(e)}")
            return None
    
    # ========== FLIGHTS ==========
    async def get_flight_details(
        self,
        airline: str,
        flight_number: str,
        departure_date: str
    ) -> dict | None:
        """Получить детальную информацию о рейсе с полным логированием"""
        self.logger.info(f"✈️ [FlyStack] Запрос информации о рейсе: {airline}{flight_number} на {departure_date}")
        self.logger.debug(f"📝 [FlyStack] Параметры: airline={airline}, flight_number={flight_number}, departure_date={departure_date}")
        
        return await self._request("flight", {
            "airline": airline,
            "flight_number": flight_number,
            "departure_date": departure_date
        })
    
    async def search_flights(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str | None = None,
        adults: int = 1,
        children: int = 0,
        infants: int = 0,
        cabin_class: str = "economy"
    ) -> list[dict]:
        """Поиск рейсов с ценами с полным логированием"""
        self.logger.info(f"🔍 [FlyStack] Поиск рейсов: {origin} → {destination} на {departure_date}")
        self.logger.debug(f"📝 [FlyStack] Параметры поиска: adults={adults}, children={children}, infants={infants}, cabin_class={cabin_class}")
        
        params = {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "adults": adults,
            "children": children,
            "infants": infants,
            "cabin_class": cabin_class
        }
        if return_date:
            params["return_date"] = return_date
        
        result = await self._request("flights", params)
        return result if isinstance(result, list) else []
    
    # ========== AIRLINES ==========
    async def get_airline(self, iata_code: str) -> dict | None:
        """Получить информацию об авиакомпании с логированием"""
        self.logger.info(f"✈️ [FlyStack] Запрос информации об авиакомпании: {iata_code}")
        return await self._request("airlines", {"iata_code": iata_code})
    
    async def get_airline_fleet(self, iata_code: str) -> list[dict]:
        """Получить флот авиакомпании с логированием"""
        self.logger.info(f"🛩️ [FlyStack] Запрос флота авиакомпании: {iata_code}")
        return await self._request("fleets", {"airline_iata": iata_code}) or []
    
    # ========== AIRPORTS ==========
    async def get_airport(self, iata_code: str) -> dict | None:
        """Получить информацию об аэропорте с логированием"""
        self.logger.info(f"🛫 [FlyStack] Запрос информации об аэропорте: {iata_code}")
        return await self._request("airports", {"iata_code": iata_code})
    
    async def get_nearby_airports(
        self,
        latitude: float,
        longitude: float,
        radius: int = 100
    ) -> list[dict]:
        """Найти аэропорты поблизости с логированием"""
        self.logger.info(f"📍 [FlyStack] Поиск аэропортов рядом: {latitude}, {longitude} (радиус: {radius} км)")
        return await self._request("airports-nearby", {
            "lat": latitude,
            "lon": longitude,
            "radius": radius
        }) or []
    
    # ========== CITIES & COUNTRIES ==========
    async def get_city(self, iata_code: str) -> dict | None:
        """Получить информацию о городе с логированием"""
        self.logger.info(f"🏙️ [FlyStack] Запрос информации о городе: {iata_code}")
        return await self._request("cities", {"iata_code": iata_code})
    
    async def get_country(self, code: str) -> dict | None:
        """Получить информацию о стране с логированием"""
        self.logger.info(f"🌍 [FlyStack] Запрос информации о стране: {code}")
        return await self._request("countries", {"code": code})
    
    # ========== ROUTES & SCHEDULES ==========
    async def get_routes(
        self,
        airline_iata: str | None = None,
        origin_iata: str | None = None,
        destination_iata: str | None = None
    ) -> list[dict]:
        """Получить маршруты с логированием"""
        self.logger.info("🗺️ [FlyStack] Запрос маршрутов")
        self.logger.debug(f"📝 [FlyStack] Параметры: airline={airline_iata}, origin={origin_iata}, destination={destination_iata}")
        
        params = {}
        if airline_iata:
            params["airline_iata"] = airline_iata
        if origin_iata:
            params["origin_iata"] = origin_iata
        if destination_iata:
            params["destination_iata"] = destination_iata
        
        return await self._request("routes", params) or []
    
    async def get_schedule(
        self,
        airport_iata: str,
        date: str,
        flight_type: str = "departure"
    ) -> list[dict]:
        """Получить расписание рейсов аэропорта с логированием"""
        self.logger.info(f"📅 [FlyStack] Запрос расписания для аэропорта {airport_iata} на {date}")
        return await self._request("schedules", {
            "airport_iata": airport_iata,
            "date": date,
            "type": flight_type
        }) or []
    
    # ========== DELAYS ==========
    async def get_delays(
        self,
        airport_iata: str,
        date: str,
        flight_type: str = "departure"
    ) -> list[dict]:
        """Получить информацию о задержках рейсов с логированием"""
        self.logger.info(f"⏳ [FlyStack] Запрос задержек для аэропорта {airport_iata} на {date}")
        return await self._request("delays", {
            "airport_iata": airport_iata,
            "date": date,
            "type": flight_type
        }) or []
    
    # ========== TIMEZONES ==========
    async def get_timezone(self, timezone: str) -> dict | None:
        """Получить информацию о часовом поясе с логированием"""
        self.logger.info(f"🕒 [FlyStack] Запрос информации о часовом поясе: {timezone}")
        return await self._request("timezones", {"timezone": timezone})

# Singleton
flystack_client = FlyStackClient()

def format_flight_details(data: dict) -> str:
    """Форматирует информацию о рейсе для Telegram с логированием"""
    logger.debug(f"📝 [FlyStack] Форматируем данные рейса: {data}")
    
    lines = []
    
    # Основная информация
    if data.get("aircraft_type"):
        lines.append(f"✈️ <b>Самолёт:</b> {data['aircraft_type']}")
    
    # Питание
    meal_map = {
        "B": "🍽️ Завтрак",
        "L": "🍽️ Обед",
        "D": "🍽️ Ужин",
        "S": "🥪 Закуска",
        "M": "🍽️ Питание",
        "R": "🍷 Напитки",
        "F": "🍽️ Полное питание",
        "O": "❌ Без питания"
    }
    if data.get("meal_service"):
        meal = data["meal_service"]
        lines.append(meal_map.get(meal, f"🍽️ {meal}"))
    
    # Багаж
    if data.get("baggage_allowance"):
        lines.append(f"🧳 <b>Багаж:</b> {data['baggage_allowance']}")
    if data.get("carry_on_allowance"):
        lines.append(f"🎒 <b>Ручная кладь:</b> {data['carry_on_allowance']}")
    
    # Комфорт
    if data.get("seat_pitch"):
        lines.append(f"💺 <b>Шаг кресел:</b> {data['seat_pitch']} см")
    if data.get("entertainment"):
        lines.append(f"🎬 <b>Развлечения:</b> {data['entertainment']}")
    if data.get("wifi") is not None:
        wifi_text = "✅ Есть" if data["wifi"] else "❌ Нет"
        lines.append(f"📶 <b>Wi-Fi:</b> {wifi_text}")
    
    # Статус
    status_map = {
        "scheduled": "🟢 По расписанию",
        "delayed": "🟡 Задержка",
        "cancelled": "🔴 Отменён",
        "landed": "✅ Приземлился",
        "departed": "🛫 Вылетел"
    }
    if data.get("status"):
        status = data["status"]
        lines.append(f"⚡ <b>Статус:</b> {status_map.get(status, status)}")
    
    # Информация о гейтах и времени
    if data.get("gate"):
        lines.append(f"🚪 <b>Гейт:</b> {data['gate']}")
    if data.get("actual_departure"):
        lines.append(f"🕐 <b>Фактический вылет:</b> {data['actual_departure']}")
    if data.get("actual_arrival"):
        lines.append(f"🕐 <b>Фактический прилёт:</b> {data['actual_arrival']}")
    if data.get("baggage_claim"):
        lines.append(f"🧳 <b>Выдача багажа:</b> лента {data['baggage_claim']}")
    
    return "\n".join(lines) if lines else "ℹ️ Информация временно недоступна"