# utils/link_converter.py
import os
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger

async def convert_to_partner_link(clean_link: str) -> str:
    """
    Единая точка преобразования ссылок через Travelpayouts API.
    Автоматически очищает от старых параметров и возвращает партнёрскую ссылку.
    """
    # Очистка от старых параметров
    parsed = urlparse(clean_link)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    clean_link = urlunparse(parsed._replace(query=urlencode(query_params, doseq=True)))
    
    # Подготовка параметров
    api_token = (os.getenv("TRAVELPAYOUTS_API_TOKEN") or os.getenv("AVIASALES_TOKEN", "")).strip()
    marker = os.getenv("TRAFFIC_SOURCE", "700812").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    if not api_token or not clean_link.startswith(('http://', 'https://')):
        logger.warning(f"⚠️ Невалидные параметры для конвертации ссылки")
        return clean_link
    
    # Вызов API
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.travelpayouts.com/links/v1/create",  # Правильный endpoint
                headers={"X-Access-Token": api_token},
                json={"link": clean_link, "marker": marker, "subid": sub_id},
                timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    partner_link = data.get("link")  # Правильное поле в ответе
                    if partner_link and partner_link.startswith("https://tp.media"):
                        logger.info(f"✅ Partner link: {partner_link[:70]}...")
                        return partner_link
                logger.error(f"⚠️ TP API error {resp.status}: {await resp.text()[:200]}")
                return clean_link
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут при конвертации ссылки")
        return clean_link
    except Exception as e:
        logger.exception(f"💥 КРИТИЧЕСКАЯ ОШИБКА при конвертации: {str(e)[:200]}")
        return clean_link