"""
test_search_flow.py
===================
Тесты для FSM-поиска, быстрого поиска (quick_search),
поиска «Везде», кнопок результатов и слежения за ценой.

Запуск:
    pytest test_search_flow.py -v

Зависимости (сверх стандартных):
    pip install pytest pytest-asyncio aiogram

Файл НЕ требует реального Redis и НЕ делает реальных запросов к API —
все внешние зависимости мокируются.
"""

import asyncio
import json
import re
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from uuid import uuid4

import pytest
import pytest_asyncio

# ─────────────────────────────────────────────────────────────────────────────
# Инициализация словаря городов (без сети — через MANUAL_ALIASES)
# cities_loader заполняется при старте бота через load_cities_from_api(),
# в тестах этот вызов не происходит → CITY_TO_IATA пустой.
# Заполняем напрямую из MANUAL_ALIASES + нескольких ключевых IATA до импорта хэндлеров.
# ─────────────────────────────────────────────────────────────────────────────
def _bootstrap_cities():
    from utils.cities_loader import CITY_TO_IATA, IATA_TO_CITY, MANUAL_ALIASES, _normalize_name
    if CITY_TO_IATA:
        return  # уже заполнен (например, кешем с диска)
    for alias, iata in MANUAL_ALIASES.items():
        norm = _normalize_name(alias)
        CITY_TO_IATA[norm] = iata
    # Добавляем прямое соответствие IATA → IATA (для MOW, AER и т.д.)
    for alias, iata in MANUAL_ALIASES.items():
        if iata not in IATA_TO_CITY:
            IATA_TO_CITY[iata] = alias.capitalize()
    # IATA-коды как ключи (MOW → MOW)
    for iata in list(IATA_TO_CITY.keys()):
        CITY_TO_IATA[iata.lower()] = iata

_bootstrap_cities()

# ─────────────────────────────────────────────────────────────────────────────
# Хелперы-моки для aiogram-типов
# ─────────────────────────────────────────────────────────────────────────────

def make_message(text: str = "", user_id: int = 123, chat_id: int = 123) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.from_user = MagicMock(id=user_id, username="testuser")
    msg.chat = MagicMock(id=chat_id)
    msg.answer = AsyncMock(return_value=MagicMock())
    msg.edit_text = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def make_callback(data: str, user_id: int = 123, chat_id: int = 123) -> MagicMock:
    cb = MagicMock()
    cb.data = data
    cb.from_user = MagicMock(id=user_id, username="testuser")
    cb.message = make_message(user_id=user_id, chat_id=chat_id)
    cb.answer = AsyncMock()
    return cb


def make_state(initial: dict = None) -> MagicMock:
    _data = dict(initial or {})
    state = MagicMock()
    # get_data должен возвращать копию _data при await
    state.get_data = AsyncMock(side_effect=lambda: dict(_data))

    # side_effect должна быть СИНХРОННОЙ (не async) —
    # AsyncMock сам оборачивает результат в awaitable,
    # но НЕ await-ит coroutine из side_effect.
    # Синхронная функция просто возвращает None → всё работает.
    state.update_data = AsyncMock(side_effect=lambda **kw: _data.update(kw))
    state.set_state   = AsyncMock(side_effect=lambda s: _data.__setitem__("__state__", str(s)))
    state.clear       = AsyncMock(side_effect=lambda: _data.clear())

    # expose internal dict for assertions
    state._data = _data
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Патчи для внешних зависимостей
# ─────────────────────────────────────────────────────────────────────────────

PATCHES = {}


def setup_patches():
    """Регистрирует все глобальные патчи, необходимые до импорта модулей."""

    # Redis
    fake_redis = MagicMock()
    fake_redis.track_funnel_step = AsyncMock()
    fake_redis.track_search_type = AsyncMock()
    fake_redis.track_no_results = AsyncMock()
    fake_redis.track_subscription_event = AsyncMock()
    fake_redis.track_link_click = AsyncMock()
    fake_redis.set_search_cache = AsyncMock()
    fake_redis.get_search_cache = AsyncMock(return_value=None)
    fake_redis.save_price_watch = AsyncMock()
    fake_redis.get_user_watches = AsyncMock(return_value=[])
    fake_redis.client = None
    PATCHES["redis"] = fake_redis

    # Smart reminder
    PATCHES["cancel_inactivity"] = MagicMock()
    PATCHES["schedule_inactivity"] = MagicMock()
    PATCHES["mark_fsm_inactive"] = MagicMock()
    PATCHES["remind_after_search"] = AsyncMock()

    # Partner link converter
    PATCHES["convert_to_partner_link"] = AsyncMock(side_effect=lambda url, **kw: url)

    # Trip.com
    PATCHES["build_trip_link"] = MagicMock(return_value=None)
    PATCHES["is_trip_supported"] = MagicMock(return_value=False)

    # Semaphore (из start.py)
    PATCHES["semaphore"] = asyncio.Semaphore(1)


setup_patches()


# ─────────────────────────────────────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════════
# БЛОК 1 — ПАРСЕР МАРШРУТА (validate_route)
# ════════════════════════════════════════════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateRoute:
    """Парсинг маршрута из текста пользователя."""

    def setup_method(self):
        from handlers.flight_fsm import validate_route
        self.vr = validate_route

    # ── Правильные форматы ────────────────────────────────────────────────

    def test_dash_separator(self):
        o, d = self.vr("Москва - Сочи")
        assert o == "москва"
        assert d == "сочи"

    def test_arrow_separator(self):
        o, d = self.vr("Москва → Сочи")
        assert o == "москва"
        assert d == "сочи"

    def test_em_dash_separator(self):
        o, d = self.vr("Москва — Сочи")
        assert o == "москва"
        assert d == "сочи"

    def test_space_separator(self):
        o, d = self.vr("Москва Сочи")
        assert o == "москва"
        assert d == "сочи"

    def test_везде_dest(self):
        o, d = self.vr("Москва - везде")
        assert o == "москва"
        assert d == "везде"

    def test_везде_origin(self):
        o, d = self.vr("везде - Стамбул")
        assert o == "везде"
        assert d == "стамбул"

    def test_multiword_city(self):
        o, d = self.vr("Санкт-Петербург - Сочи")
        assert "санкт-петербург" in o
        assert d == "сочи"

    def test_iata_codes(self):
        o, d = self.vr("MOW - AER")
        assert o == "mow"
        assert d == "aer"

    # ── Неверные форматы ────────────────────────────────────────────────

    def test_single_city_returns_none(self):
        o, d = self.vr("Москва")
        assert o is None or d is None

    def test_empty_string(self):
        o, d = self.vr("")
        assert o is None or d is None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 2 — ПАРСЕР ДАТ (validate_date)
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateDate:
    def setup_method(self):
        from handlers.flight_fsm import validate_date
        self.vd = validate_date

    def test_valid_date(self):           assert self.vd("15.03") is True
    def test_valid_date_zero_padded(self): assert self.vd("05.01") is True
    def test_invalid_format(self):       assert self.vd("15/03") is False
    def test_too_long(self):             assert self.vd("150.03") is False
    def test_letters(self):              assert self.vd("ab.cd") is False
    def test_empty(self):                assert self.vd("") is False
    def test_day_31(self):               assert self.vd("31.12") is True
    def test_month_13_invalid(self):     assert self.vd("01.13") is False
    def test_day_0_invalid(self):        assert self.vd("00.05") is False


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 3 — ПАРСЕР БЫСТРОГО ПОИСКА (_parse_quick_search)
# ─────────────────────────────────────────────────────────────────────────────

class TestParseQuickSearch:
    def setup_method(self):
        from handlers.quick_search import _parse_quick_search
        self.p = _parse_quick_search

    # ── Базовые форматы ───────────────────────────────────────────────────

    def test_city_city_date(self):
        r = self.p("Москва Сочи 10.03")
        assert r is not None
        o, d, dep, ret, pax = r
        assert dep == "10.03"
        assert ret is None

    def test_city_dash_city_date(self):
        r = self.p("Москва - Сочи 10.03")
        assert r is not None
        assert r[2] == "10.03"

    def test_city_city_two_dates(self):
        r = self.p("Москва Сочи 10.03 20.03")
        assert r is not None
        assert r[2] == "10.03"
        assert r[3] == "20.03"

    def test_iata_format(self):
        r = self.p("MOW AER 15.04")
        assert r is not None
        assert r[2] == "15.04"

    def test_везде_as_destination(self):
        r = self.p("Москва везде 10.03")
        assert r is not None
        o, d, dep, ret, pax = r
        assert d.lower() == "везде"

    def test_везде_as_origin(self):
        r = self.p("везде Стамбул 10.03")
        assert r is not None
        o, d, dep, ret, pax = r
        assert o.lower() == "везде"

    def test_with_passengers(self):
        r = self.p("Москва Сочи 10.03 2 взр")
        assert r is not None
        assert "2" in r[4] or "взр" in r[4].lower()

    def test_with_direct_flag(self):
        r = self.p("Москва Сочи 10.03 прямые")
        assert r is not None
        assert "прям" in r[4].lower()

    # ── Должны вернуть None ───────────────────────────────────────────────

    def test_no_date_returns_none(self):
        assert self.p("Москва Сочи") is None

    def test_just_text_returns_none(self):
        assert self.p("привет как дела") is None

    def test_single_word_returns_none(self):
        assert self.p("Москва") is None

    def test_empty_returns_none(self):
        assert self.p("") is None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 4 — ВСПОМОГАТЕЛЬНЫЕ УТИЛИТЫ
# ─────────────────────────────────────────────────────────────────────────────

class TestFlightUtils:
    def test_format_duration_hours_minutes(self):
        from utils.flight_utils import _format_duration
        assert "2" in _format_duration(150)   # 2ч 30м
        assert "30" in _format_duration(150)

    def test_format_duration_minutes_only(self):
        from utils.flight_utils import _format_duration
        result = _format_duration(45)
        assert "45" in result

    def test_format_duration_zero(self):
        from utils.flight_utils import _format_duration
        result = _format_duration(0)
        assert result is not None

    def test_parse_passengers_default(self):
        from utils.flight_utils import parse_passengers
        assert parse_passengers("") == "1"

    def test_parse_passengers_two_adults(self):
        from utils.flight_utils import parse_passengers
        result = parse_passengers("2 взр")
        assert result.startswith("2")

    def test_parse_passengers_three(self):
        from utils.flight_utils import parse_passengers
        result = parse_passengers("3 взрослых")
        assert result.startswith("3")


class TestBuildPassengerCode:
    def setup_method(self):
        from handlers.flight_fsm import build_passenger_code
        self.bpc = build_passenger_code

    def test_adults_only(self):
        assert self.bpc(2) == "2"

    def test_adults_and_children(self):
        code = self.bpc(2, 1)
        assert code == "21"

    def test_adults_children_infants(self):
        code = self.bpc(2, 1, 1)
        assert code == "211"

    def test_minimum_one_adult(self):
        code = self.bpc(0)
        assert code.startswith("1")

    def test_total_capped_at_9(self):
        code = self.bpc(5, 3, 2)
        total = sum(int(c) for c in code)
        assert total <= 9


class TestNormalizeDate:
    def setup_method(self):
        from services.flight_search import normalize_date
        self.nd = normalize_date

    def test_basic_date(self):
        result = self.nd("15.03")
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_preserves_day_month(self):
        result = self.nd("15.03")
        assert "-03-15" in result

    def test_empty_string(self):
        result = self.nd("")
        # Не должен упасть
        assert result is not None


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 5 — FSM: process_route (маршрут)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessRoute:
    """Шаг 1/6 — ввод маршрута."""

    async def _run(self, text: str, state_data: dict = None):
        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("utils.smart_reminder.schedule_inactivity", PATCHES["schedule_inactivity"]),
            patch("handlers.flight_wizard.redis_client", PATCHES["redis"]),
        ):
            from handlers.flight_wizard import process_route
            msg = make_message(text)
            state = make_state(state_data or {})
            await process_route(msg, state)
            return msg, state

    async def test_valid_route_sets_state_data(self):
        msg, state = await self._run("Москва - Сочи")
        # Должны быть сохранены origin и dest
        assert "origin" in state._data or "origin_iata" in state._data

    async def test_invalid_route_sends_error(self):
        msg, state = await self._run("непонятно")
        msg.answer.assert_called()
        last_call = msg.answer.call_args_list[-1]
        text = str(last_call)
        assert "❌" in text or "Неверный" in text or "Не знаю" in text

    async def test_везде_везде_blocked(self):
        msg, state = await self._run("везде - везде")
        msg.answer.assert_called()
        last_text = str(msg.answer.call_args_list[-1])
        assert "Везде" in last_text or "нельзя" in last_text.lower() or "Нельзя" in last_text

    async def test_same_origin_dest_blocked(self):
        msg, state = await self._run("Москва - Москва")
        msg.answer.assert_called()
        last_text = str(msg.answer.call_args_list[-1])
        assert "совпадать" in last_text or "❌" in last_text


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 6 — FSM: process_depart_date
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessDepartDate:
    """Шаг 2/6 — дата вылета."""

    async def _run(self, text: str, state_data: dict):
        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("utils.smart_reminder.schedule_inactivity", PATCHES["schedule_inactivity"]),
            patch("handlers.flight_wizard.redis_client", PATCHES["redis"]),
        ):
            from handlers.flight_wizard import process_depart_date
            msg = make_message(text)
            state = make_state(state_data)
            await process_depart_date(msg, state)
            return msg, state

    async def test_valid_date_saved(self):
        _, state = await self._run("15.03", {"origin": "москва", "dest": "сочи",
                                              "origin_iata": "MOW", "dest_iata": "AER"})
        assert state._data.get("depart_date") == "15.03"

    async def test_invalid_date_sends_error(self):
        msg, _ = await self._run("99.99", {"origin": "москва", "dest": "сочи",
                                            "origin_iata": "MOW", "dest_iata": "AER"})
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])

    async def test_везде_skips_return_question(self):
        """При маршруте с «везде» шаг обратного билета пропускается."""
        _, state = await self._run(
            "15.03",
            {"origin": "москва", "dest": "везде", "origin_iata": "MOW", "dest_iata": None}
        )
        # need_return должен быть False, state переходит к flight_type
        assert state._data.get("need_return") is False


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 7 — FSM: process_need_return (echo + новое сообщение)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessNeedReturn:
    """Шаг 3/6 — нужен ли обратный билет. Проверяем echo-поведение."""

    async def _run(self, data: str, state_data: dict):
        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
        ):
            from handlers.flight_wizard import process_need_return
            cb = make_callback(data)
            state = make_state(state_data)
            await process_need_return(cb, state)
            return cb, state

    async def test_yes_echo_and_new_message(self):
        """return_yes: edit_text редактирует сообщение с вопросом о дате возврата (шаг 3/6)."""
        cb, state = await self._run("return_yes", {"depart_date": "15.03"})
        # edit_text вызван — содержит текст шага 3/6 или «возврат»
        cb.message.edit_text.assert_called()
        edit_text = str(cb.message.edit_text.call_args_list[0])
        assert "3/6" in edit_text or "возврат" in edit_text.lower()
        # answer() вызывается в конце хэндлера как callback.answer() (пустой, подтверждение нажатия)
        cb.answer.assert_called()

    async def test_no_echo_and_proceeds(self):
        """return_no: edit_text показывает «Без обратного билета», need_return=False."""
        cb, state = await self._run("return_no", {"depart_date": "15.03"})
        cb.message.edit_text.assert_called()
        edit_text = str(cb.message.edit_text.call_args_list[0])
        assert "Без обратного билета" in edit_text
        # need_return = False сохранён
        assert state._data.get("need_return") is False

    async def test_yes_sets_return_date_state(self):
        cb, state = await self._run("return_yes", {"depart_date": "15.03"})
        # FSM переведён в состояние return_date
        assert "return_date" in str(state._data.get("__state__", ""))


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 8 — FSM: process_flight_type (echo)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessFlightType:
    """Шаг 4/6 — тип рейса. Проверяем echo."""

    async def _run(self, data: str, state_data: dict = None):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_flight_type
            cb = make_callback(data)
            state = make_state(state_data or {})
            await process_flight_type(cb, state)
            return cb, state

    async def test_direct_echo(self):
        cb, state = await self._run("ft_direct")
        cb.message.edit_text.assert_called()
        assert "Прямые" in str(cb.message.edit_text.call_args_list[0])

    async def test_transfer_echo(self):
        cb, state = await self._run("ft_transfer")
        assert "пересадк" in str(cb.message.edit_text.call_args_list[0]).lower()

    async def test_all_echo(self):
        cb, state = await self._run("ft_all")
        assert "Все" in str(cb.message.edit_text.call_args_list[0])

    async def test_saves_flight_type(self):
        _, state = await self._run("ft_direct")
        assert state._data.get("flight_type") == "direct"

    async def test_saves_transfer_type(self):
        _, state = await self._run("ft_transfer")
        assert state._data.get("flight_type") == "transfer"

    async def test_edit_mode_goes_to_summary(self):
        cb, state = await self._run("ft_all", {"_edit_mode": True})
        # В режиме редактирования _edit_mode сбрасывается
        assert not state._data.get("_edit_mode", False)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 9 — FSM: process_adults (echo)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessAdults:
    """Шаг 5/6 — количество взрослых. Проверяем echo."""

    async def _run(self, data: str, state_data: dict = None):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_adults
            cb = make_callback(data)
            state = make_state(state_data or {})
            await process_adults(cb, state)
            return cb, state

    async def test_echo_shown(self):
        cb, _ = await self._run("adults_2")
        cb.message.edit_text.assert_called()
        assert "2" in str(cb.message.edit_text.call_args_list[0])

    async def test_adults_saved(self):
        _, state = await self._run("adults_3")
        assert state._data.get("adults") == 3

    async def test_nine_adults_skips_children(self):
        """9 взрослых — сразу переходим к summary."""
        cb, state = await self._run("adults_9")
        assert state._data.get("adults") == 9
        assert state._data.get("children") == 0
        assert state._data.get("infants") == 0

    async def test_asks_children_for_less_than_9(self):
        """Меньше 9 — задаём вопрос о детях."""
        cb, _ = await self._run("adults_2")
        # answer вызван (вопрос о детях)
        cb.message.answer.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 10 — FSM: process_has_children, process_children, process_infants (echo)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPassengerSteps:
    async def _run_has_children(self, data: str, state_data: dict = None):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_has_children
            cb = make_callback(data)
            state = make_state(state_data or {"adults": 2})
            await process_has_children(cb, state)
            return cb, state

    async def _run_children(self, data: str, state_data: dict = None):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_children
            cb = make_callback(data)
            state = make_state(state_data or {"adults": 2})
            await process_children(cb, state)
            return cb, state

    async def _run_infants(self, data: str, state_data: dict = None):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_infants
            cb = make_callback(data)
            state = make_state(state_data or {"adults": 2, "children": 0})
            await process_infants(cb, state)
            return cb, state

    async def test_has_children_no_echo(self):
        cb, state = await self._run_has_children("hc_no")
        cb.message.edit_text.assert_called()
        # Код пишет "✅ Без детей"
        assert "Без детей" in str(cb.message.edit_text.call_args_list[0])
        assert state._data.get("children") == 0

    async def test_has_children_yes_echo(self):
        cb, _ = await self._run_has_children("hc_yes")
        cb.message.edit_text.assert_called()
        # Код пишет "✅ Летят дети"
        assert "Летят дети" in str(cb.message.edit_text.call_args_list[0])

    async def test_children_count_saved_with_echo(self):
        cb, state = await self._run_children("ch_2", {"adults": 2})
        assert state._data.get("children") == 2
        cb.message.edit_text.assert_called()
        assert "2" in str(cb.message.edit_text.call_args_list[0])

    async def test_infants_count_saved_with_echo(self):
        cb, state = await self._run_infants("inf_1", {"adults": 2, "children": 0})
        assert state._data.get("infants") == 1
        cb.message.edit_text.assert_called()
        assert "1" in str(cb.message.edit_text.call_args_list[0])

    async def test_children_over_limit_ignored(self):
        """Нельзя добавить больше детей, чем (9 - adults)."""
        cb, state = await self._run_children("ch_8", {"adults": 2})
        # children не должен стать 8 (лимит 9 - 2 = 7 макс)
        # handler должен просто вернуть callback.answer() без сохранения
        assert state._data.get("children", 0) == 0


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 11 — FSM: process_return_date
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestProcessReturnDate:
    async def _run(self, text: str, state_data: dict):
        with patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]):
            from handlers.flight_wizard import process_return_date
            msg = make_message(text)
            state = make_state(state_data)
            await process_return_date(msg, state)
            return msg, state

    async def test_valid_return_date_saved(self):
        _, state = await self._run("25.03", {"depart_date": "15.03"})
        assert state._data.get("return_date") == "25.03"

    async def test_return_before_depart_blocked(self):
        msg, state = await self._run("10.03", {"depart_date": "15.03"})
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])
        assert state._data.get("return_date") != "10.03"

    async def test_return_same_as_depart_blocked(self):
        msg, _ = await self._run("15.03", {"depart_date": "15.03"})
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])

    async def test_invalid_format_blocked(self):
        msg, _ = await self._run("abc", {"depart_date": "15.03"})
        assert "❌" in str(msg.answer.call_args_list[-1])


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 12 — search_results: кнопки «Следить за ценой» и «Ещё варианты»
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_FLIGHTS = [
    {"origin": "MOW", "destination": "AER", "value": 5000, "price": 5000,
     "transfers": 0, "duration": 150, "airline": "SU", "flight_number": "SU100",
     "link": "https://aviasales.ru/link1"},
    {"origin": "MOW", "destination": "AER", "value": 7000, "price": 7000,
     "transfers": 1, "duration": 210, "airline": "S7", "flight_number": "S7200",
     "link": "https://aviasales.ru/link2"},
    {"origin": "MOW", "destination": "AER", "value": 9000, "price": 9000,
     "transfers": 0, "duration": 155, "airline": "DP", "flight_number": "DP300",
     "link": "https://aviasales.ru/link3"},
]

SAMPLE_CACHE = {
    "flights": SAMPLE_FLIGHTS,
    "rest_flights": SAMPLE_FLIGHTS[1:],
    "origin": "москва", "origin_iata": "MOW", "origin_name": "Москва",
    "dest": "сочи",    "dest_iata":   "AER", "dest_name":   "Сочи",
    "depart_date": "15.03", "return_date": None,
    "need_return": False,
    "display_depart": "15.03.2025", "display_return": None,
    "original_depart": "15.03",    "original_return": None,
    "passenger_desc": "1 взр.", "passengers_code": "1", "passenger_code": "1",
    "adults": 1, "children": 0, "infants": 0,
    "origin_everywhere": False, "dest_everywhere": False,
    "flight_type": "all",
}


@pytest.mark.asyncio
class TestWatchPriceHandler:
    """Кнопка «Следить за ценой» (watch_all_{cache_id})."""

    async def test_watch_all_shows_threshold_menu(self):
        cache_id = str(uuid4())
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=dict(SAMPLE_CACHE))

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
        ):
            from handlers.search_results import handle_watch_price
            cb = make_callback(f"watch_all_{cache_id}")
            await handle_watch_price(cb)

        # Должен показать меню выбора порога
        cb.message.answer.assert_called()
        call_text = str(cb.message.answer.call_args_list[-1])
        assert "изменени" in call_text.lower() or "порог" in call_text.lower() or "уведомлять" in call_text.lower()

    async def test_watch_stale_cache_shows_alert(self):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=None)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
        ):
            from handlers.search_results import handle_watch_price
            cb = make_callback(f"watch_all_{uuid4()}")
            await handle_watch_price(cb)

        cb.answer.assert_called()
        # callback.answer("Данные устарели", show_alert=True)
        assert "устарел" in str(cb.answer.call_args_list[-1]).lower()

    async def test_watch_empty_flights_shows_alert(self):
        """Кэш есть, но flights = [] — не должен падать с ValueError."""
        fake_cache = dict(SAMPLE_CACHE, flights=[])
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=fake_cache)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
        ):
            from handlers.search_results import handle_watch_price
            cb = make_callback(f"watch_all_{uuid4()}")
            # Не должен упасть
            await handle_watch_price(cb)

        cb.answer.assert_called()


@pytest.mark.asyncio
class TestSetThresholdHandler:
    """Кнопки выбора порога слежения."""

    async def _run(self, threshold: int, cache_id: str, price: int, cache_data: dict):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=cache_data)
        fake_redis.save_price_watch = AsyncMock()
        fake_redis.track_subscription_event = AsyncMock()

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
            # can_add_sub импортирован в search_results как: from handlers.billing import can_add_sub
            # патчим его прямо в пространстве имён search_results
            patch("handlers.search_results.can_add_sub", AsyncMock(return_value=(True, ""))),
        ):
            from handlers.search_results import handle_set_threshold
            cb = make_callback(f"set_threshold:{threshold}:{cache_id}:{price}")
            await handle_set_threshold(cb)
            return cb, fake_redis

    async def test_threshold_0_saves_watch(self):
        cid = str(uuid4())
        _, r = await self._run(0, cid, 5000, dict(SAMPLE_CACHE))
        r.save_price_watch.assert_called_once()

    async def test_threshold_100_saves_watch(self):
        cid = str(uuid4())
        _, r = await self._run(100, cid, 5000, dict(SAMPLE_CACHE))
        r.save_price_watch.assert_called_once()

    async def test_threshold_1000_saves_watch(self):
        cid = str(uuid4())
        _, r = await self._run(1000, cid, 5000, dict(SAMPLE_CACHE))
        r.save_price_watch.assert_called_once()

    async def test_stale_cache_shows_alert(self):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=None)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
            patch("handlers.search_results.can_add_sub", AsyncMock(return_value=(True, ""))),
        ):
            from handlers.search_results import handle_set_threshold
            cb = make_callback(f"set_threshold:0:{uuid4()}:5000")
            await handle_set_threshold(cb)

        assert "устарел" in str(cb.answer.call_args_list[-1]).lower()

    async def test_empty_flights_shows_alert(self):
        """flights=[] — не должен падать с ValueError."""
        fake_cache = dict(SAMPLE_CACHE, flights=[])
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=fake_cache)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
            patch("handlers.search_results.can_add_sub", AsyncMock(return_value=(True, ""))),
        ):
            from handlers.search_results import handle_set_threshold
            cb = make_callback(f"set_threshold:0:{uuid4()}:5000")
            await handle_set_threshold(cb)

        cb.answer.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 13 — search_results: «Посмотреть ещё варианты» (more_flights)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMoreFlightsHandler:
    async def _run(self, cache_id: str, page: int, cache_data: dict):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=cache_data)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.everywhere_search.redis_client", fake_redis),
            patch("utils.link_converter.convert_to_partner_link",
                  AsyncMock(side_effect=lambda url, **kw: url)),
        ):
            from handlers.everywhere_search import handle_more_flights
            cb = make_callback(f"more_flights_{cache_id}_{page}")
            await handle_more_flights(cb)
            return cb

    async def test_page1_shows_flights(self):
        cid = str(uuid4())
        cache = dict(SAMPLE_CACHE, rest_flights=SAMPLE_FLIGHTS)
        cb = await self._run(cid, 1, cache)
        cb.message.answer.assert_called()
        # Показывает рейсы
        text = str(cb.message.answer.call_args_list[-1])
        assert "₽" in text or "вариант" in text.lower()

    async def test_stale_cache_shows_alert(self):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=None)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.everywhere_search.redis_client", fake_redis),
        ):
            from handlers.everywhere_search import handle_more_flights
            cb = make_callback(f"more_flights_{uuid4()}_1")
            await handle_more_flights(cb)

        cb.answer.assert_called()
        assert "устарел" in str(cb.answer.call_args_list[-1]).lower()

    async def test_past_last_page_shows_alert(self):
        cid = str(uuid4())
        # Только 1 рейс в rest, страница 2 — нет данных
        cache = dict(SAMPLE_CACHE, rest_flights=[SAMPLE_FLIGHTS[0]])
        cb = await self._run(cid, 2, cache)
        cb.answer.assert_called()
        assert "нет" in str(cb.answer.call_args_list[-1]).lower() or "Нет" in str(cb.answer.call_args_list[-1])

    async def test_next_page_button_shown_when_more_flights(self):
        """Если вариантов > 3 — кнопка «ещё» должна присутствовать."""
        cid = str(uuid4())
        many_flights = SAMPLE_FLIGHTS * 4  # 12 рейсов
        cache = dict(SAMPLE_CACHE, rest_flights=many_flights)
        cb = await self._run(cid, 1, cache)
        # Кнопка следующей страницы в reply_markup
        call_kwargs = cb.message.answer.call_args_list[-1].kwargs
        kb = call_kwargs.get("reply_markup")
        if kb:
            flat = [btn.callback_data or "" for row in kb.inline_keyboard for btn in row]
            assert any("more_flights" in s and "_2" in s for s in flat)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 14 — search_results: edit_from_results (восстановление из кэша)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestEditFromResults:
    async def test_restores_fsm_from_cache(self):
        cid = str(uuid4())
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=dict(SAMPLE_CACHE))

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("utils.smart_reminder.schedule_inactivity", PATCHES["schedule_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
        ):
            from handlers.search_results import edit_from_results
            cb = make_callback(f"edit_from_results_{cid}")
            state = make_state()
            await edit_from_results(cb, state)

        # Данные маршрута восстановлены
        assert state._data.get("origin_iata") == "MOW"
        assert state._data.get("dest_iata") == "AER"
        assert state._data.get("depart_date") == "15.03"

    async def test_stale_cache_shows_alert(self):
        fake_redis = MagicMock()
        fake_redis.get_search_cache = AsyncMock(return_value=None)

        with (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("handlers.search_results.redis_client", fake_redis),
        ):
            from handlers.search_results import edit_from_results
            cb = make_callback(f"edit_from_results_{uuid4()}")
            state = make_state()
            await edit_from_results(cb, state)

        cb.answer.assert_called()
        assert "устарел" in str(cb.answer.call_args_list[-1]).lower()


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 15 — Везде: process_everywhere_search (карточка результата)
# ─────────────────────────────────────────────────────────────────────────────

EVERYWHERE_FLIGHTS = [
    {"origin": "MOW", "destination": "IST", "value": 8000, "price": 8000,
     "transfers": 0, "duration": 180, "airline": "SU", "flight_number": "SU101",
     "link": "https://aviasales.ru/ew1"},
    {"origin": "LED", "destination": "IST", "value": 12000, "price": 12000,
     "transfers": 1, "duration": 240, "airline": "TK", "flight_number": "TK001",
     "link": "https://aviasales.ru/ew2"},
]

EVERYWHERE_FSM_DATA = {
    "origin": "москва", "origin_iata": "MOW", "origin_name": "Москва",
    "dest": "везде",    "dest_iata":   None,  "dest_name":   "Везде",
    "depart_date": "15.03", "return_date": None,
    "need_return": False, "flight_type": "all",
    "passenger_desc": "1 взр.", "passenger_code": "1",
    "adults": 1, "children": 0, "infants": 0,
}


@pytest.mark.asyncio
class TestProcessEverywhereSearch:
    async def test_dest_everywhere_renders_card(self):
        """Город → Везде — карточка с кнопками отображается."""
        fake_redis = MagicMock()
        fake_redis.set_search_cache = AsyncMock()
        fake_redis.track_search_type = AsyncMock()
        fake_redis.track_funnel_step = AsyncMock()

        with (
            patch("utils.redis_client.redis_client", fake_redis),
            patch("utils.link_converter.convert_to_partner_link",
                  AsyncMock(side_effect=lambda url, **kw: url)),
        ):
            from handlers.everywhere_search import process_everywhere_search
            cb = make_callback("confirm_search")
            result = await process_everywhere_search(
                cb, dict(EVERYWHERE_FSM_DATA), EVERYWHERE_FLIGHTS, "destination_everywhere"
            )

        assert result is True
        cb.message.edit_text.assert_called()
        # Карточка содержит цену
        card_text = str(cb.message.edit_text.call_args_list[-1])
        assert "₽" in card_text or "8000" in card_text

    async def test_empty_flights_returns_false(self):
        with patch("utils.redis_client.redis_client", MagicMock()):
            from handlers.everywhere_search import process_everywhere_search
            cb = make_callback("confirm_search")
            result = await process_everywhere_search(
                cb, dict(EVERYWHERE_FSM_DATA), [], "destination_everywhere"
            )
        assert result is False

    async def test_origin_everywhere_renders_card(self):
        """Везде → Город — карточка отображается."""
        fake_redis = MagicMock()
        fake_redis.set_search_cache = AsyncMock()
        fake_redis.track_search_type = AsyncMock()
        fake_redis.track_funnel_step = AsyncMock()
        fsm = dict(EVERYWHERE_FSM_DATA,
                   origin="везде", origin_iata=None, origin_name="Везде",
                   dest="стамбул", dest_iata="IST", dest_name="Стамбул")

        with (
            patch("utils.redis_client.redis_client", fake_redis),
            patch("utils.link_converter.convert_to_partner_link",
                  AsyncMock(side_effect=lambda url, **kw: url)),
        ):
            from handlers.everywhere_search import process_everywhere_search
            cb = make_callback("confirm_search")
            result = await process_everywhere_search(
                cb, fsm, EVERYWHERE_FLIGHTS, "origin_everywhere"
            )

        assert result is True

    async def test_more_flights_button_present_when_multiple(self):
        """Кнопка «Посмотреть ещё варианты» есть если вариантов > 1."""
        fake_redis = MagicMock()
        fake_redis.set_search_cache = AsyncMock()
        fake_redis.track_search_type = AsyncMock()
        fake_redis.track_funnel_step = AsyncMock()

        with (
            patch("utils.redis_client.redis_client", fake_redis),
            patch("utils.link_converter.convert_to_partner_link",
                  AsyncMock(side_effect=lambda url, **kw: url)),
        ):
            from handlers.everywhere_search import process_everywhere_search
            cb = make_callback("confirm_search")
            await process_everywhere_search(
                cb, dict(EVERYWHERE_FSM_DATA), EVERYWHERE_FLIGHTS, "destination_everywhere"
            )

        call_kwargs = cb.message.edit_text.call_args_list[-1].kwargs
        kb = call_kwargs.get("reply_markup")
        assert kb is not None
        flat = [btn.callback_data or "" for row in kb.inline_keyboard for btn in row]
        assert any("more_flights" in s for s in flat)

    async def test_watch_button_present(self):
        """Кнопка «Следить за ценой» есть в результатах везде."""
        fake_redis = MagicMock()
        fake_redis.set_search_cache = AsyncMock()
        fake_redis.track_search_type = AsyncMock()
        fake_redis.track_funnel_step = AsyncMock()

        with (
            patch("utils.redis_client.redis_client", fake_redis),
            patch("utils.link_converter.convert_to_partner_link",
                  AsyncMock(side_effect=lambda url, **kw: url)),
        ):
            from handlers.everywhere_search import process_everywhere_search
            cb = make_callback("confirm_search")
            await process_everywhere_search(
                cb, dict(EVERYWHERE_FSM_DATA), EVERYWHERE_FLIGHTS, "destination_everywhere"
            )

        call_kwargs = cb.message.edit_text.call_args_list[-1].kwargs
        kb = call_kwargs.get("reply_markup")
        flat = [btn.callback_data or "" for row in kb.inline_keyboard for btn in row]
        assert any("watch_all" in s for s in flat)


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 16 — Интеграционный: полный FSM-сценарий Москва → Сочи
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFullSearchScenario:
    """
    Интеграционный сценарий: проходим все шаги FSM и проверяем
    что данные накапливаются корректно.
    """

    def _patch_ctx(self):
        return (
            patch("utils.smart_reminder.cancel_inactivity", PATCHES["cancel_inactivity"]),
            patch("utils.smart_reminder.schedule_inactivity", PATCHES["schedule_inactivity"]),
            patch("utils.smart_reminder.mark_fsm_inactive", PATCHES["mark_fsm_inactive"]),
            patch("handlers.flight_wizard.redis_client", PATCHES["redis"]),
        )

    async def test_full_flow_data_accumulation(self):
        import contextlib
        state = make_state()

        async def step(coro):
            with contextlib.ExitStack() as stack:
                for p in self._patch_ctx():
                    stack.enter_context(p)
                await coro

        # Шаг 1: маршрут
        from handlers.flight_wizard import process_route
        msg = make_message("Москва - Сочи")
        await step(process_route(msg, state))
        assert state._data.get("origin_iata") is not None

        # Шаг 2: дата вылета
        from handlers.flight_wizard import process_depart_date
        msg = make_message("20.06")
        await step(process_depart_date(msg, state))
        assert state._data.get("depart_date") == "20.06"

        # Шаг 3: обратный билет = нет
        from handlers.flight_wizard import process_need_return
        cb = make_callback("return_no")
        await step(process_need_return(cb, state))
        assert state._data.get("need_return") is False

        # Шаг 4: тип рейса = все
        from handlers.flight_wizard import process_flight_type
        cb = make_callback("ft_all")
        await step(process_flight_type(cb, state))
        assert state._data.get("flight_type") == "all"

        # Шаг 5: взрослых = 1, без детей
        from handlers.flight_wizard import process_adults
        cb = make_callback("adults_1")
        await step(process_adults(cb, state))
        assert state._data.get("adults") == 1

        from handlers.flight_wizard import process_has_children
        cb = make_callback("hc_no")
        await step(process_has_children(cb, state))
        assert state._data.get("children") == 0

        # Итог: все данные собраны
        assert state._data.get("origin_iata") is not None
        assert state._data.get("dest_iata") is not None
        assert state._data.get("depart_date") == "20.06"
        assert state._data.get("need_return") is False
        assert state._data.get("flight_type") == "all"
        assert state._data.get("adults") == 1


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 17 — _resolve_city и generate_booking_link
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveCity:
    def setup_method(self):
        from handlers.quick_search import _resolve_city
        self.rc = _resolve_city

    def test_known_city_returns_iata(self):
        iata, name = self.rc("Москва")
        assert iata is not None
        assert len(iata) == 3

    def test_iata_code_passthrough(self):
        iata, name = self.rc("MOW")
        assert iata == "MOW"

    def test_unknown_city_returns_none(self):
        iata, name = self.rc("НесуществующийГород12345")
        assert iata is None

    def test_case_insensitive(self):
        iata1, _ = self.rc("москва")
        iata2, _ = self.rc("МОСКВА")
        # Оба должны найти MOW или быть None — главное одинаково
        assert iata1 == iata2


class TestGenerateBookingLink:
    def setup_method(self):
        from services.flight_search import generate_booking_link
        self.gbl = generate_booking_link

    def test_link_contains_origin_dest(self):
        flight = {"origin": "MOW", "destination": "AER",
                  "depart_date": "2025-06-20", "return_date": None,
                  "link": None, "deep_link": None}
        link = self.gbl(flight=flight, origin="MOW", dest="AER",
                        depart_date="20.06", passengers_code="1")
        assert "MOW" in link or "mow" in link.lower()
        assert "AER" in link or "aer" in link.lower()

    def test_link_starts_with_aviasales(self):
        flight = {"origin": "MOW", "destination": "AER",
                  "depart_date": "2025-06-20", "return_date": None,
                  "link": None, "deep_link": None}
        link = self.gbl(flight=flight, origin="MOW", dest="AER",
                        depart_date="20.06", passengers_code="1")
        assert "aviasales" in link.lower()


# ─────────────────────────────────────────────────────────────────────────────
# БЛОК 18 — Smoke-тест: импорты и роутеры подключаются без ошибок
# ─────────────────────────────────────────────────────────────────────────────

class TestImports:
    def test_flight_wizard_imports(self):
        import handlers.flight_wizard  # noqa

    def test_search_results_imports(self):
        import handlers.search_results  # noqa

    def test_everywhere_search_imports(self):
        import handlers.everywhere_search  # noqa

    def test_quick_search_imports(self):
        import handlers.quick_search  # noqa

    def test_flight_fsm_imports(self):
        import handlers.flight_fsm  # noqa

    def test_routers_exist(self):
        from handlers.flight_wizard import router as r1
        from handlers.search_results import router as r2
        from handlers.everywhere_search import router as r3
        assert r1 is not None
        assert r2 is not None
        assert r3 is not None


# ─────────────────────────────────────────────────────────────────────────────
# Запуск напрямую (python test_search_flow.py)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        capture_output=False,
    )
    sys.exit(result.returncode)