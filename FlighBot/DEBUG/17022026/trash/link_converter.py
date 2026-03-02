import os
import asyncio
import aiohttp
import json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from utils.logger import logger

async def convert_to_partner_link(clean_link: str) -> str:
    """
    Единая точка преобразования ссылок через Travelpayouts API (links/v1/create).
    Возвращает партнёрскую ссылку или исходную при ошибке.
    """
    print(f"[DEBUG convert_to_partner_link] Вход: clean_link='{clean_link}'")
    
    # === 1. ОЧИСТКА ССЫЛКИ ===
    parsed = urlparse(clean_link)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    clean_link = urlunparse(parsed._replace(query=urlencode(query_params, doseq=True)))
    print(f"[DEBUG convert_to_partner_link] После очистки: clean_link='{clean_link}'")
    
    # === 2. ПОДГОТОВКА ПАРАМЕТРОВ ===
    api_token = (os.getenv("TRAVELPAYOUTS_API_TOKEN") or os.getenv("AVIASALES_TOKEN", "")).strip()
    trs = os.getenv("TRS_ID", "494709").strip()
    marker = os.getenv("TRAFFIC_SOURCE", "700812").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram_bot_v2").strip()
    
    print(f"[DEBUG convert_to_partner_link] Параметры: trs='{trs}', marker='{marker}', sub_id='{sub_id}'")
    
    if not api_token or not clean_link.startswith(('http://', 'https://')):
        print(f"[DEBUG convert_to_partner_link] Возврат: невалидные параметры (token={bool(api_token)}, link='{clean_link[:50]}...')")
        return clean_link
    
    # Преобразуем trs и marker в int (API требует числа!)
    try:
        trs = int(trs)
        marker = int(marker)
        print(f"[DEBUG convert_to_partner_link] Преобразованные параметры: trs={trs}, marker={marker}")
    except (ValueError, TypeError) as e:
        print(f"[DEBUG convert_to_partner_link] Ошибка преобразования trs/marker: {e}")
        return clean_link
    
    # === 3. ФОРМИРОВАНИЕ КОРРЕКТНОГО ЗАПРОСА ===
    payload = {
        "trs": trs,
        "marker": marker,
        "shorten": True,
        "links": [{
            "url": clean_link,
            "sub_id": sub_id
        }]
    }
    print(f"[DEBUG convert_to_partner_link] Отправляемый payload: {json.dumps(payload, indent=2)}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.travelpayouts.com/links/v1/create",
                headers={"X-Access-Token": api_token},
                json=payload,
                timeout=10
            ) as resp:
                print(f"[DEBUG convert_to_partner_link] Ответ API: status={resp.status}")
                
                if resp.status == 200:
                    data = await resp.json()
                    print(f"[DEBUG convert_to_partner_link] Ответ API (JSON): {json.dumps(data, indent=2)}")
                    
                    # Проверка общего статуса ответа
                    if data.get("code") != "success":
                        error_msg = data.get('error', 'Unknown error')
                        print(f"[DEBUG convert_to_partner_link] Ошибка API: {error_msg}")
                        return clean_link
                    
                    # Извлечение результата для первой ссылки
                    if (data.get("result") and 
                        data["result"].get("links") and 
                        len(data["result"]["links"]) > 0):
                        
                        link_result = data["result"]["links"][0]
                        print(f"[DEBUG convert_to_partner_link] Результат для ссылки: {json.dumps(link_result, indent=2)}")
                        
                        if link_result.get("code") == "success":
                            partner_url = link_result.get("partner_url", "").strip()
                            print(f"[DEBUG convert_to_partner_link] Полученная партнёрская ссылка: '{partner_url}'")
                            
                            if partner_url and partner_url.startswith("https://"):
                                print(f"[DEBUG convert_to_partner_link] УСПЕХ: Возвращаем партнёрскую ссылку: '{partner_url}'")
                                return partner_url
                            print(f"[DEBUG convert_to_partner_link] Ошибка: пустая или неправильная партнёрская ссылка")
                        else:
                            msg = link_result.get("message", "Unknown error")
                            print(f"[DEBUG convert_to_partner_link] Ошибка конвертации: {msg}")
                    else:
                        print(f"[DEBUG convert_to_partner_link] Некорректная структура ответа API")
                else:
                    error_text = await resp.text()
                    print(f"[DEBUG convert_to_partner_link] Ошибка HTTP {resp.status}: {error_text[:250]}")
                
                print(f"[DEBUG convert_to_partner_link] Возврат исходной ссылки: '{clean_link}'")
                return clean_link
                
    except asyncio.TimeoutError:
        print(f"[DEBUG convert_to_partner_link] Таймаут при конвертации ссылки")
        return clean_link
    except Exception as e:
        print(f"[DEBUG convert_to_partner_link] Ошибка конвертации: {str(e)[:200]}")
        return clean_link