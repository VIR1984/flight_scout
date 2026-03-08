# services/flystack_client.py
import os
import logging
import aiohttp

logger = logging.getLogger(__name__)

FLYSTACK_BASE_URL = os.getenv("FLYSTACK_BASE_URL", "https://api.flystack.io/v1")
API_KEY = os.getenv("FLYSTACK_API_KEY", "")

_fs_connector: aiohttp.TCPConnector | None = None


def _get_connector() -> aiohttp.TCPConnector:
    global _fs_connector
    if _fs_connector is None or _fs_connector.closed:
        _fs_connector = aiohttp.TCPConnector(limit=10)
    return _fs_connector


class FlyStackClient:
    """Клиент для FlyStack API"""

    def __init__(self):
        self.api_key = API_KEY
        self.base_url = FLYSTACK_BASE_URL
        self.logger = logger

    async def _request(self, endpoint: str, params: dict = None) -> dict | None:
        if not self.api_key:
            self.logger.warning("⚠️ [FlyStack] FLYSTACK_API_KEY не установлен")
            return None

        url = f"{self.base_url}/{endpoint}"
        params = params or {}
        params["api_key"] = self.api_key

        try:
            async with aiohttp.ClientSession(connector=_get_connector(),
                                             connector_owner=False) as session:
                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 429:
                        self.logger.warning("⚠️ [FlyStack] Rate limit")
                        return {"error": "rate_limit"}
                    if resp.status != 200:
                        self.logger.error(f"❌ [FlyStack] {resp.status}")
                        return None
                    data = await resp.json()
                    return data.get("data") or data
        except aiohttp.ClientError as e:
            self.logger.error(f"❌ [FlyStack] Ошибка соединения: {e}")
            return None
        except Exception as e:
            self.logger.exception(f"❌ [FlyStack] Неизвестная ошибка: {e}")
            return None

    async def get_flight_details(
        self,
        airline: str,
        flight_number: str,
        departure_date: str,
    ) -> dict | None:
        return await self._request("flight", {
            "airline": airline,
            "flight_number": flight_number,
            "departure_date": departure_date,
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
        cabin_class: str = "economy",
    ) -> list[dict]:
        params = {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date,
            "adults": adults,
            "children": children,
            "infants": infants,
            "cabin_class": cabin_class,
        }
        if return_date:
            params["return_date"] = return_date
        result = await self._request("flights", params)
        return result if isinstance(result, list) else []

    async def get_airline(self, iata_code: str) -> dict | None:
        return await self._request("airlines", {"iata_code": iata_code})

    async def get_airline_fleet(self, iata_code: str) -> list[dict]:
        return await self._request("fleets", {"airline_iata": iata_code}) or []


# Singleton
flystack_client = FlyStackClient()


def format_flight_details(data: dict) -> str:
    """Форматирует информацию о рейсе для Telegram."""
    lines = []

    if data.get("aircraft_type"):
        lines.append(f"✈️ <b>Самолёт:</b> {data['aircraft_type']}")

    meal_map = {
        "B": "🍽️ Завтрак", "L": "🍽️ Обед", "D": "🍽️ Ужин",
        "S": "🥪 Закуска",  "M": "🍽️ Питание", "R": "🍷 Напитки",
        "F": "🍽️ Полное питание", "O": "❌ Без питания",
    }
    if data.get("meal_service"):
        lines.append(meal_map.get(data["meal_service"], f"🍽️ {data['meal_service']}"))

    if data.get("baggage_allowance"):
        lines.append(f"🧳 <b>Багаж:</b> {data['baggage_allowance']}")
    if data.get("carry_on_allowance"):
        lines.append(f"🎒 <b>Ручная кладь:</b> {data['carry_on_allowance']}")
    if data.get("seat_pitch"):
        lines.append(f"💺 <b>Шаг кресел:</b> {data['seat_pitch']} см")
    if data.get("entertainment"):
        lines.append(f"🎬 <b>Развлечения:</b> {data['entertainment']}")
    if data.get("wifi") is not None:
        lines.append(f"📶 <b>Wi-Fi:</b> {'✅ Есть' if data['wifi'] else '❌ Нет'}")

    status_map = {
        "scheduled": "🟢 По расписанию", "delayed": "🟡 Задержка",
        "cancelled": "🔴 Отменён",        "landed":   "✅ Приземлился",
        "departed":  "🛫 Вылетел",
    }
    if data.get("status"):
        lines.append(f"⚡ <b>Статус:</b> {status_map.get(data['status'], data['status'])}")

    if data.get("gate"):
        lines.append(f"🚪 <b>Гейт:</b> {data['gate']}")
    if data.get("actual_departure"):
        lines.append(f"🕐 <b>Фактический вылет:</b> {data['actual_departure']}")
    if data.get("actual_arrival"):
        lines.append(f"🕐 <b>Фактический прилёт:</b> {data['actual_arrival']}")
    if data.get("baggage_claim"):
        lines.append(f"🧳 <b>Выдача багажа:</b> лента {data['baggage_claim']}")

    return "\n".join(lines) if lines else "ℹ️ Информация временно недоступна"