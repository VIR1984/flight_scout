# services/transfer_search.py
"""
Поиск трансферов через GetTransfer API
Документация: https://support.travelpayouts.com/hc/ru/articles/360016375920
"""
import os
import aiohttp
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

async def search_transfers(
    airport_iata: str,
    transfer_date: str,
    adults: int = 1
) -> List[Dict[str, Any]]:
    """
    Ищет трансферы из аэропорта через GetTransfer API
    
    Args:
        airport_iata: IATA-код аэропорта (например, "AER", "SVO")
        transfer_date: дата трансфера в формате ГГГГ-ММ-ДД
        adults: количество взрослых пассажиров
    
    Returns:
        Список трансферов, отсортированный по цене (от дешёвых к дорогим)
    """
    token = os.getenv("GETTRANSFER_TOKEN")
    if not token:
        logger.warning("GETTRANSFER_TOKEN не задан — поиск трансферов недоступен")
        return []
    
    url = "https://api.travelpayouts.com/v2/prices/get-transfer"
    params = {
        "origin": airport_iata.upper(),
        "date": transfer_date,
        "token": token
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as response:
                data = await response.json()
                
                if not data.get("success"):
                    error_msg = data.get("error", "Unknown error")
                    logger.warning(f"GetTransfer API error: {error_msg}")
                    return []
                
                transfers = data.get("data", [])
                
                # Фильтруем только эконом-класс и сортируем по цене
                transfers = [t for t in transfers if t.get("vehicle") == "Economy"]
                transfers.sort(key=lambda x: x.get("price", 999999))
                
                return transfers[:3]
                
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка сети при поиске трансферов: {e}")
        return []
    except Exception as e:
        logger.error(f"Неизвестная ошибка при поиске трансферов: {e}")
        return []

def generate_transfer_link(
    transfer_id: str,
    marker: Optional[str] = None,
    sub_id: Optional[str] = None
) -> str:
    """
    Генерирует партнёрскую ссылку на бронирование трансфера
    """
    base_url = f"https://gettransfer.com/ru/transfers/{transfer_id}"
    params = []
    
    marker = marker or os.getenv("GETTRANSFER_MARKER", "") or os.getenv("TRAFFIC_SOURCE", "")
    if marker:
        params.append(f"marker={marker}")
    
    if sub_id:
        params.append(f"sub_id={sub_id}")
    
    if params:
        return f"{base_url}?{'&'.join(params)}"
    return base_url