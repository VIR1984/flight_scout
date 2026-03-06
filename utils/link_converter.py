import os
import asyncio
import aiohttp
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger

# Глобальный коннектор — переиспользуем TCP для Travelpayouts API
_lc_connector: aiohttp.TCPConnector | None = None

def _lc_session() -> aiohttp.ClientSession:
    global _lc_connector
    if _lc_connector is None or _lc_connector.closed:
        _lc_connector = aiohttp.TCPConnector(
            limit=10, ttl_dns_cache=300, enable_cleanup_closed=True
        )
    return aiohttp.ClientSession(connector=_lc_connector, connector_owner=False)

async def convert_to_partner_link(clean_link: str, context: str = "unknown") -> str:
    """
    Единая точка преобразования ссылок через Travelpayouts API.
    context — откуда вызван (search_results / everywhere / quick / multi).
    Счётчик кликов пишется в Redis при каждом вызове.
    """
    # Трекаем генерацию ссылки (= показ кнопки пользователю)
    try:
        from utils.redis_client import redis_client
        asyncio.ensure_future(redis_client.track_link_click(context))
    except Exception:
        pass

    parsed = urlparse(clean_link)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    clean_link = urlunparse(parsed._replace(query=urlencode(query_params, doseq=True)))

    api_token = (os.getenv("TRAVELPAYOUTS_API_TOKEN") or os.getenv("AVIASALES_TOKEN", "")).strip()
    trs    = os.getenv("TRS_ID", "494709").strip()
    marker = os.getenv("TRAFFIC_SOURCE", "700812").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram_bot_v2").strip()

    if not api_token or not clean_link.startswith(('http://', 'https://')):
        logger.warning(f"⚠️ Невалидные параметры: token={bool(api_token)}, link={clean_link[:50]}...")
        return clean_link

    try:
        trs    = int(trs)
        marker = int(marker)
    except (ValueError, TypeError) as e:
        logger.error(f"❌ Ошибка преобразования trs/marker: {e}")
        return clean_link

    payload = {
        "trs": trs, "marker": marker, "shorten": True,
        "links": [{"url": clean_link, "sub_id": sub_id}]
    }

    try:
        async with _lc_session() as session:
            async with session.post(
                "https://api.travelpayouts.com/links/v1/create",
                headers={"X-Access-Token": api_token},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") != "success":
                        logger.error(f"❌ API error: {data.get('error', 'Unknown')}")
                        return clean_link
                    if (data.get("result") and data["result"].get("links")
                            and len(data["result"]["links"]) > 0):
                        link_result = data["result"]["links"][0]
                        if link_result.get("code") == "success":
                            partner_url = link_result.get("partner_url")
                            if partner_url and partner_url.startswith("https://"):
                                logger.info(f"✅ Partner URL: {partner_url[:70]}...")
                                return partner_url
                            logger.error(f"❌ Ответ без валидной ссылки: {link_result}")
                        else:
                            logger.error(f"❌ Конвертация не удалась: {link_result.get('message')}")
                    else:
                        logger.error(f"❌ Некорректная структура ответа: {data}")
                else:
                    logger.error(f"⚠️ TP API HTTP {resp.status}: {(await resp.text())[:250]}")
                return clean_link
    except asyncio.TimeoutError:
        logger.error("❌ Таймаут при конвертации ссылки")
        return clean_link
    except Exception as e:
        logger.exception(f"💥 КРИТИЧЕСКАЯ ОШИБКА в convert_to_partner_link: {str(e)[:200]}")
        return clean_link