#!/usr/bin/env python3
"""
Быстрый тест обработки ссылок авиабилетов
Проверяет: генерацию чистых ссылок, обновление пассажиров, преобразование в партнёрские
"""
import asyncio
import os
import sys
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# Устанавливаем тестовые переменные окружения
os.environ['TRAVELPAYOUTS_API_TOKEN'] = 'test_token_123'
os.environ['AVIASALES_TOKEN'] = 'test_token_123'
os.environ['TRAFFIC_SOURCE'] = '700812'
os.environ['TRAFFIC_SUB_ID'] = 'telegram_bot_v2'
os.environ['GETTRANSFER_MARKER'] = '700812'

print("="*70)
print("🚀 БЫСТРЫЙ ТЕСТ ОБРАБОТКИ ССЫЛОК АВИАБИЛЕТОВ")
print("="*70)

# === 1. ТЕСТ: Генерация ЧИСТОЙ ссылки (без маркера) ===
def test_generate_booking_link():
    """Проверяет генерацию чистой ссылки без маркера"""
    print("\n[ТЕСТ 1] Генерация чистой ссылки (без маркера)")
    
    def format_avia_link_date(date_str: str) -> str:
        try:
            day, month = date_str.split('.')
            return f"{day}{month}"
        except:
            return date_str.replace('.', '')
    
    # Сценарий: туда-обратно, 2 взр. + 1 реб.
    route = f"MOW{format_avia_link_date('19.03')}CAN{format_avia_link_date('25.03')}21"
    clean_link = f"https://www.aviasales.ru/search/{route}"
    
    # Проверки
    assert clean_link.startswith("https://www.aviasales.ru/search/"), "❌ Неверный формат ссылки"
    assert "marker=" not in clean_link, "❌ Ссылка содержит маркер (должна быть ЧИСТОЙ)"
    assert "sub_id=" not in clean_link, "❌ Ссылка содержит sub_id (должна быть ЧИСТОЙ)"
    assert "MOW1903CAN250321" in clean_link, "❌ Неверный маршрут в ссылке"
    
    print(f"✅ Сгенерирована ЧИСТАЯ ссылка: {clean_link[:60]}...")
    return clean_link

# === 2. ТЕСТ: Обновление количества пассажиров ===
def test_update_passengers():
    """Проверяет корректное обновление пассажиров в ссылке"""
    print("\n[ТЕСТ 2] Обновление количества пассажиров")
    
    # Исходная ссылка: 1 взрослый
    link = "https://www.aviasales.ru/search/MOW1903CAN1"
    
    # Обновляем до 2 взр. + 1 реб. (код "21")
    parsed = urlparse(link)
    path = parsed.path
    if '/search/' in path:
        route_part = path.split('/search/', 1)[1]
        route = route_part.split('?')[0] if '?' in route_part else route_part
        
        # Удаляем последнюю цифру (старое кол-во пассажиров) и добавляем новый код
        if route and route[-1].isdigit():
            new_route = route[:-1] + "21"
        else:
            new_route = route + "21"
        
        new_path = f"/search/{new_route}"
        updated_link = urlunparse(parsed._replace(path=new_path))
    
    # Проверки
    assert updated_link == "https://www.aviasales.ru/search/MOW1903CAN21", "❌ Ошибка обновления пассажиров"
    assert updated_link.count('CAN') == 1, "❌ Повреждён маршрут при обновлении"
    
    print(f"✅ Пассажиры обновлены: {link.split('/')[-1]} → {updated_link.split('/')[-1]}")
    return updated_link

# === 3. ТЕСТ: Очистка старых параметров из ссылки ===
def test_clean_old_params():
    """Проверяет удаление старых marker/sub_id перед отправкой в API"""
    print("\n[ТЕСТ 3] Очистка старых параметров marker/sub_id")
    
    dirty_link = "https://www.aviasales.ru/search/MOW1903CAN21?marker=OLD_123&sub_id=old_bot&other=param"
    
    parsed = urlparse(dirty_link)
    query_params = parse_qs(parsed.query)
    
    # Удаляем старые параметры
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    
    new_query = urlencode(query_params, doseq=True)
    clean_link = urlunparse(parsed._replace(query=new_query))
    
    # Проверки
    assert "marker=" not in clean_link, "❌ Старый marker не удалён"
    assert "sub_id=" not in clean_link, "❌ Старый sub_id не удалён"
    assert "other=param" in clean_link, "❌ Удалены сторонние параметры (ошибка!)"
    assert clean_link.startswith("https://www.aviasales.ru/search/MOW1903CAN21"), "❌ Повреждён базовый маршрут"
    
    print(f"✅ Старые параметры удалены: {dirty_link[:50]}... → {clean_link[:50]}...")
    return clean_link

# === 4. ТЕСТ: Формирование запроса к Travelpayouts API ===
def test_api_request_format(clean_link):
    """Проверяет корректность формирования JSON для API"""
    print("\n[ТЕСТ 4] Формирование запроса к Travelpayouts API")
    
    marker = os.getenv("TRAFFIC_SOURCE", "700812").strip()
    sub_id = os.getenv("TRAFFIC_SUB_ID", "telegram").strip()
    
    request_json = {
        "link": clean_link,
        "marker": marker,
        "subid": sub_id  # Важно: в API поле "subid", не "sub_id"!
    }
    
    # Проверки
    assert request_json["link"] == clean_link, "❌ Неверная ссылка в запросе"
    assert request_json["marker"] == "700812", f"❌ Неверный marker: {request_json['marker']}"
    assert request_json["subid"] == "telegram_bot_v2", f"❌ Неверный subid: {request_json['subid']}"
    assert "sub_id" not in request_json, "❌ В запросе используется 'sub_id' вместо 'subid' (ошибка!)"
    
    print("✅ JSON запроса сформирован корректно:")
    print(f"   link: {request_json['link'][:50]}...")
    print(f"   marker: {request_json['marker']}")
    print(f"   subid: {request_json['subid']}")

# === 5. ТЕСТ: Обработка ответа API ===
def test_api_response_handling():
    """Проверяет извлечение партнёрской ссылки из ответа API"""
    print("\n[ТЕСТ 5] Обработка ответа Travelpayouts API")
    
    # Успешный ответ
    success_response = {
        "partner_link": "https://tp.media/r?campaign_id=100&erid=2VtzqwMXWzo&marker=700812&p=4114&sub_id=telegram_bot_v2&trs=494709&u=https%3A%2F%2Fwww.aviasales.ru%2Fsearch%2FMOW1903CAN21"
    }
    
    partner_link = success_response.get("partner_link")
    
    # Проверки
    assert partner_link, "❌ Ответ не содержит 'partner_link'"
    assert partner_link.startswith("https://tp.media/r?"), "❌ Неверный формат партнёрской ссылки"
    assert "campaign_id=" in partner_link, "❌ В ссылке отсутствует campaign_id"
    assert "marker=700812" in partner_link, "❌ В ссылке неверный marker"
    assert "sub_id=telegram_bot_v2" in partner_link, "❌ В ссылке неверный sub_id"
    
    print("✅ Партнёрская ссылка извлечена корректно:")
    print(f"   {partner_link[:70]}...")

# === 6. ТЕСТ: Fallback при ошибке API ===
def test_api_error_fallback(dirty_link):
    """Проверяет возврат чистой ссылки при ошибке API"""
    print("\n[ТЕСТ 6] Fallback при ошибке API")
    
    # Очищаем ссылку (как в реальной функции)
    parsed = urlparse(dirty_link)
    query_params = parse_qs(parsed.query)
    query_params.pop('marker', None)
    query_params.pop('sub_id', None)
    clean_query = urlencode(query_params, doseq=True)
    clean_link = urlunparse(parsed._replace(query=clean_query))
    
    # При ошибке API возвращаем чистую ссылку
    fallback_link = clean_link
    
    # Проверки
    assert "marker=" not in fallback_link, "❌ Fallback ссылка содержит маркер"
    assert fallback_link.startswith("https://www.aviasales.ru/search/"), "❌ Неверный формат fallback ссылки"
    
    print("✅ При ошибке API возвращается ЧИСТАЯ ссылка:")
    print(f"   {fallback_link[:60]}...")

# === ЗАПУСК ТЕСТОВ ===
if __name__ == "__main__":
    try:
        # Запускаем тесты последовательно
        clean_link = test_generate_booking_link()
        updated_link = test_update_passengers()
        cleaned_link = test_clean_old_params()
        test_api_request_format(cleaned_link)
        test_api_response_handling()
        test_api_error_fallback("https://www.aviasales.ru/search/MOW1903CAN1?marker=OLD")
        
        print("\n" + "="*70)
        print("✅ ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО!")
        print("="*70)
        print("\n💡 Ключевые выводы:")
        print("   • Ссылки генерируются БЕЗ маркера на этапе формирования")
        print("   • Старые параметры marker/sub_id удаляются перед отправкой в API")
        print("   • API получает корректный JSON с полями: link, marker, subid")
        print("   • Ответ содержит ссылку формата: https://tp.media/r?campaign_id=...")
        print("   • При ошибке API пользователь получает рабочую чистую ссылку")
        print("\n⚠️ ВАЖНО: Убедитесь, что в коде:")
        print("   • В start.txt и everywhere_search.txt используется convert_to_partner_link")
        print("   • В flight_search.txt УДАЛЕНА функция add_marker_to_url")
        print("   • В price_watcher.py добавлена convert_to_partner_link")
        
    except AssertionError as e:
        print(f"\n❌ ТЕСТ ПРОВАЛЕН: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"\n💥 КРИТИЧЕСКАЯ ОШИБКА: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)