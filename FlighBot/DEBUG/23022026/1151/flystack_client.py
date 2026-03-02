# services/flystack_client.py
import os
import aiohttp
from typing import Optional, Dict, Any, List
from utils.logger import logger

FLYSTACK_BASE_URL = "https://api.flystack.dev/v1"
API_KEY = os.getenv("FLYSTACK_API_KEY", "").strip()

class FlyStackClient:
    """Клиент для FlyStack API с полным набором методов"""
    
    def __init__(self):
        self.api_key = API_KEY
        self.base_url = FLYSTACK_BASE_URL
    
    async def _request(self, endpoint: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """Базовый метод для HTTP запросов"""
        if not self.api_key:
            logger.warning("⚠️ FLYSTACK_API_KEY не установлен")
            return None
        
        url = f"{self.base_url}/{endpoint}"
        params = params or {}
        params["api_key"] = self.api_key
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status == 429:
                        logger.warning("⚠️ Превышен лимит FlyStack API")
                        return {"error": "rate_limit"}
                    if resp.status != 200:
                        logger.error(f"❌ Ошибка FlyStack API {resp.status}: {await resp.text()}")
                        return None
                    
                    data = await resp.json()
                    return data.get("data") or data
        except Exception as e:
            logger.error(f"❌ Ошибка запроса к FlyStack: {e}")
            return None
    
    # ========== FLIGHTS ==========
    async def get_flight_details(
        self,
        airline: str,
        flight_number: str,
        departure_date: str
    ) -> Optional[Dict[str, Any]]:
        """Получить детальную информацию о рейсе"""
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
        return_date: Optional[str] = None,
        adults: int = 1,
        children: int = 0,
        infants: int = 0,
        cabin_class: str = "economy"
    ) -> List[Dict[str, Any]]:
        """Поиск рейсов с ценами"""
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
    async def get_airline(self, iata_code: str) -> Optional[Dict[str, Any]]:
        """Получить информацию об авиакомпании"""
        return await self._request("airlines", {"iata_code": iata_code})
    
    async def get_airline_fleet(self, iata_code: str) -> List[Dict[str, Any]]:
        """Получить флот авиакомпании"""
        return await self._request("fleets", {"airline_iata": iata_code}) or []
    
    # ========== AIRPORTS ==========
    async def get_airport(self, iata_code: str) -> Optional[Dict[str, Any]]:
        """Получить информацию об аэропорте"""
        return await self._request("airports", {"iata_code": iata_code})
    
    async def get_nearby_airports(
        self,
        latitude: float,
        longitude: float,
        radius: int = 100
    ) -> List[Dict[str, Any]]:
        """Найти аэропорты поблизости"""
        return await self._request("airports-nearby", {
            "lat": latitude,
            "lon": longitude,
            "radius": radius
        }) or []
    
    # ========== CITIES & COUNTRIES ==========
    async def get_city(self, iata_code: str) -> Optional[Dict[str, Any]]:
        """Получить информацию о городе"""
        return await self._request("cities", {"iata_code": iata_code})
    
    async def get_country(self, code: str) -> Optional[Dict[str, Any]]:
        """Получить информацию о стране"""
        return await self._request("countries", {"code": code})
    
    # ========== ROUTES & SCHEDULES ==========
    async def get_routes(
        self,
        airline_iata: Optional[str] = None,
        origin_iata: Optional[str] = None,
        destination_iata: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Получить маршруты"""
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
    ) -> List[Dict[str, Any]]:
        """Получить расписание рейсов аэропорта"""
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
    ) -> List[Dict[str, Any]]:
        """Получить информацию о задержках рейсов"""
        return await self._request("delays", {
            "airport_iata": airport_iata,
            "date": date,
            "type": flight_type
        }) or []
    
    # ========== TIMEZONES ==========
    async def get_timezone(self, timezone: str) -> Optional[Dict[str, Any]]:
        """Получить информацию о часовом поясе"""
        return await self._request("timezones", {"timezone": timezone})

# Singleton
flystack_client = FlyStackClient()

def format_flight_details(data: Dict[str, Any]) -> str:
    """Форматирует информацию о рейсе для Telegram"""
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
    
    return "\n".join(lines) if lines else "ℹ️ Информация временно недоступна"