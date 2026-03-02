import os
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger

async def convert_to_partner_link(clean_link: str) -> str:
    """
    Единая точка преобразования ссылок через Travelpayouts API (links/v1/create).
    Возвращает партнёрскую ссылку вида https://tp.media/r?... или исходную при ошибке.
    """
    # === 1. ОЧИСТКА ССЫЛКИ ===
    parsed = urlparse(clean_link)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    clean_link = urlunparse(parsed._replace(query=urlencode(query_params, doseq=True)))
    
    # === 2. ПОДГОТОВКА ПАРАМЕТРОВ (ОБЯЗАТЕЛЬНО ЧИСЛА ДЛЯ trs И marker) ===
    api_token = (os.getenv("TRAVELPAYOUTS_API_TOKEN") or os.getenv("AVIASALES_TOKEN", "")).strip()
    trs = os.getenv("TRS_ID", "494709").strip()  # ← КРИТИЧЕСКИ ВАЖНО!
    marker = os.getenv("TRAFFIC_SOURCE", "700812").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram_bot_v2").strip()  # ← Рекомендуется "telegram_bot_v2"
    
    if not api_token or not clean_link.startswith(('http://', 'https://')):
        logger.warning(f"⚠️ Невалидные параметры: token={bool(api_token)}, link={clean_link[:50]}...")
        return clean_link
    
    # Преобразуем trs и marker в int (API требует числа!)
    try:
        trs = int(trs)
        marker = int(marker)
    except (ValueError, TypeError) as e:
        logger.error(f"❌ Ошибка преобразования trs/marker в число: {e} | trs='{trs}', marker='{marker}'")
        return clean_link
    
    # === 3. ФОРМИРОВАНИЕ КОРРЕКТНОГО ЗАПРОСА ===
    payload = {
        "trs": trs,          # ← Project ID (обязательно!)
        "marker": marker,    # ← Partner ID (число!)
        "shorten": True,    # ← False для полной ссылки с campaign_id
        "links": [{          # ← Массив объектов (обязательно!)
            "url": clean_link,
            "sub_id": sub_id  # ← Обратите внимание: sub_id (с подчёркиванием!)
        }]
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.travelpayouts.com/links/v1/create",  # ← ПРАВИЛЬНЫЙ ENDPOINT
                headers={"X-Access-Token": api_token},
                json=payload,
                timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    # Проверка общего статуса ответа
                    if data.get("code") != "success":
                        logger.error(f"❌ API error: {data.get('error', 'Unknown')}")
                        return clean_link
                    
                    # Извлечение результата для первой ссылки
                    if (data.get("result") and 
                        data["result"].get("links") and 
                        len(data["result"]["links"]) > 0):
                        
                        link_result = data["result"]["links"][0]
                        if link_result.get("code") == "success":
                            partner_url = link_result.get("partner_url")
                            if partner_url and partner_url.startswith("https://tp.media"):
                                logger.info(f"✅ Partner URL: {partner_url[:70]}...")
                                return partner_url
                            logger.error(f"❌ Ответ без валидной ссылки: {link_result}")
                        else:
                            msg = link_result.get("message", "Unknown error")
                            logger.error(f"❌ Конвертация ссылки не удалась: {msg}")
                    else:
                        logger.error(f"❌ Некорректная структура ответа: {data}")
                else:
                    error_text = await resp.text()
                    logger.error(f"⚠️ TP API HTTP {resp.status}: {error_text[:250]}")
                return clean_link
                
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут при конвертации ссылки в партнёрскую")
        return clean_link
    except Exception as e:
        logger.exception(f"💥 КРИТИЧЕСКАЯ ОШИБКА в convert_to_partner_link: {str(e)[:200]}")
        return clean_link