"""
test_multi_search.py
====================
Тесты для составного (мульти-сегментного) поиска авиабилетов.

Покрывает:
  - Вспомогательные функции (_resolve_city, _validate_date, _build_multi_link,
    _segments_summary, _build_pax_code, _build_pax_desc)
  - FSM-шаги: ввод города вылета (ms_origin), прибытия (ms_dest), даты (ms_date)
  - Кнопки управления: ms_add_segment, ms_done_segments
  - Пассажиры: ms_adults, ms_has_children, ms_children, ms_infants
  - Экран подтверждения: _show_multi_summary, ms_edit_pax
  - Финальный шаг: ms_confirm (генерация ссылки)
  - Интеграционный сценарий: полный маршрут из 2 сегментов

Запуск:
    pytest test_multi_search.py -v

Не требует Redis и реальных API — все зависимости мокируются.
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Инициализация словаря городов (без сети, через MANUAL_ALIASES)
# ─────────────────────────────────────────────────────────────────────────────

def _bootstrap_cities():
    from utils.cities_loader import CITY_TO_IATA, IATA_TO_CITY, MANUAL_ALIASES, _normalize_name
    if CITY_TO_IATA:
        return
    for alias, iata in MANUAL_ALIASES.items():
        CITY_TO_IATA[_normalize_name(alias)] = iata
        if iata not in IATA_TO_CITY:
            IATA_TO_CITY[iata] = alias.capitalize()
    # IATA-коды сами на себя (MOW → MOW)
    for iata in list(IATA_TO_CITY.keys()):
        CITY_TO_IATA[iata.lower()] = iata

_bootstrap_cities()


# ─────────────────────────────────────────────────────────────────────────────
# Хелперы-моки
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
    state.get_data  = AsyncMock(side_effect=lambda: dict(_data))
    state.update_data = AsyncMock(side_effect=lambda **kw: _data.update(kw))
    state.set_state   = AsyncMock(side_effect=lambda s: _data.__setitem__("__state__", str(s)))
    state.clear       = AsyncMock(side_effect=lambda: _data.clear())
    state._data = _data
    return state


# ─────────────────────────────────────────────────────────────────────────────
# Глобальные патчи
# ─────────────────────────────────────────────────────────────────────────────

PATCHES = {}


def setup_patches():
    fake_redis = MagicMock()
    fake_redis.track_search_type = AsyncMock()
    fake_redis.track_funnel_step = AsyncMock()
    PATCHES["redis"] = fake_redis

    PATCHES["cancel_inactivity"]   = MagicMock()
    PATCHES["schedule_inactivity"] = MagicMock()
    PATCHES["mark_fsm_inactive"]   = MagicMock()
    PATCHES["convert_to_partner_link"] = AsyncMock(side_effect=lambda url, **kw: url)


setup_patches()

# Общий контекст патчей для FSM-тестов
def _std_patches():
    return (
        patch("utils.smart_reminder.cancel_inactivity",   PATCHES["cancel_inactivity"]),
        patch("utils.smart_reminder.schedule_inactivity", PATCHES["schedule_inactivity"]),
        patch("utils.smart_reminder.mark_fsm_inactive",   PATCHES["mark_fsm_inactive"]),
        patch("handlers.multi_search.redis_client",       PATCHES["redis"]),
        patch("handlers.multi_search.convert_to_partner_link",
              PATCHES["convert_to_partner_link"]),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Образцовые данные
# ─────────────────────────────────────────────────────────────────────────────

SEG_MOW_IST = {
    "origin_iata": "MOW", "origin_name": "Москва",
    "dest_iata":   "IST", "dest_name":   "Стамбул",
    "date": "10.03",
}
SEG_IST_AER = {
    "origin_iata": "IST", "origin_name": "Стамбул",
    "dest_iata":   "AER", "dest_name":   "Сочи",
    "date": "20.03",
}
SEG_AER_LED = {
    "origin_iata": "AER", "origin_name": "Сочи",
    "dest_iata":   "LED", "dest_name":   "Санкт-Петербург",
    "date": "25.03",
}

TWO_SEGS  = [SEG_MOW_IST, SEG_IST_AER]
THREE_SEGS = [SEG_MOW_IST, SEG_IST_AER, SEG_AER_LED]


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 1 — _resolve_city
# ═════════════════════════════════════════════════════════════════════════════

class TestResolveCity:
    def setup_method(self):
        from handlers.multi_search import _resolve_city
        self.rc = _resolve_city

    def test_known_city_returns_iata(self):
        iata, name = self.rc("Москва")
        assert iata == "MOW"
        assert name is not None

    def test_known_city_lowercase(self):
        iata, name = self.rc("москва")
        assert iata == "MOW"

    def test_known_city_sochi(self):
        iata, name = self.rc("Сочи")
        assert iata == "AER"

    def test_iata_passthrough(self):
        """3-буквенный IATA-код распознаётся напрямую."""
        iata, name = self.rc("MOW")
        assert iata == "MOW"

    def test_unknown_city_returns_none(self):
        iata, name = self.rc("НесуществующийГород9999")
        assert iata is None
        assert name is None

    def test_empty_string_returns_none(self):
        iata, name = self.rc("")
        assert iata is None

    def test_spb_alias(self):
        """Алиас «спб» → LED."""
        iata, _ = self.rc("спб")
        assert iata == "LED"


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 2 — _validate_date
# ═════════════════════════════════════════════════════════════════════════════

class TestValidateDate:
    def setup_method(self):
        from handlers.multi_search import _validate_date
        self.vd = _validate_date

    def test_valid_date(self):          assert self.vd("15.03") is True
    def test_valid_first_of_month(self): assert self.vd("01.01") is True
    def test_valid_last_day(self):       assert self.vd("31.12") is True
    def test_invalid_month_13(self):     assert self.vd("01.13") is False
    def test_invalid_day_0(self):        assert self.vd("00.05") is False
    def test_invalid_format_slash(self): assert self.vd("15/03") is False
    def test_letters(self):              assert self.vd("ab.cd") is False
    def test_empty(self):                assert self.vd("") is False
    def test_too_long(self):             assert self.vd("150.03") is False


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 3 — _build_pax_code и _build_pax_desc
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildPaxCode:
    def setup_method(self):
        from handlers.multi_search import _build_pax_code
        self.bpc = _build_pax_code

    def test_adults_only(self):
        assert self.bpc(1) == "1"
        assert self.bpc(3) == "3"

    def test_adults_and_children(self):
        assert self.bpc(2, 1) == "21"

    def test_adults_children_infants(self):
        assert self.bpc(2, 1, 1) == "211"

    def test_minimum_one_adult(self):
        """adults=0 → приводится к 1."""
        code = self.bpc(0)
        assert code.startswith("1")

    def test_total_capped_at_9(self):
        """Сумма всех пассажиров не превышает 9."""
        code = self.bpc(5, 3, 2)
        total = sum(int(c) for c in code)
        assert total <= 9

    def test_nine_adults_no_children(self):
        assert self.bpc(9) == "9"

    def test_no_trailing_zeros(self):
        """Нули в конце не добавляются."""
        code = self.bpc(2, 0, 0)
        assert code == "2"

    def test_children_but_no_infants(self):
        """Младенцев нет — третья цифра не добавляется."""
        code = self.bpc(2, 2, 0)
        assert code == "22"


class TestBuildPaxDesc:
    def setup_method(self):
        from handlers.multi_search import _build_pax_desc
        self.bpd = _build_pax_desc

    def test_adults_only(self):
        assert "взр" in self.bpd(1)
        assert "дет" not in self.bpd(1)

    def test_with_children(self):
        desc = self.bpd(2, 1)
        assert "дет" in desc

    def test_with_infants(self):
        desc = self.bpd(2, 0, 1)
        assert "мл" in desc

    def test_all_categories(self):
        desc = self.bpd(2, 1, 1)
        assert "взр" in desc
        assert "дет" in desc
        assert "мл" in desc


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 4 — _build_multi_link
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildMultiLink:
    def setup_method(self):
        from handlers.multi_search import _build_multi_link
        self.bml = _build_multi_link

    def test_two_segments_connected(self):
        """MOW→IST 10.03, IST→AER 20.03 — связанный маршрут."""
        link = self.bml(TWO_SEGS, "1")
        assert "aviasales" in link.lower()
        assert "MOW" in link
        assert "IST" in link
        assert "AER" in link
        # Даты: 10.03 → "1003", 20.03 → "2003"
        assert "1003" in link
        assert "2003" in link

    def test_three_segments_connected(self):
        link = self.bml(THREE_SEGS, "1")
        assert "MOW" in link
        assert "IST" in link
        assert "AER" in link
        assert "LED" in link

    def test_pax_code_appended(self):
        """Код пассажиров в конце params."""
        link = self.bml(TWO_SEGS, "211")
        params = link.split("params=")[1]
        assert params.endswith("211")

    def test_disconnected_segments_include_origin(self):
        """Несвязанный сегмент (другой город вылета) — origin добавляется явно."""
        segs = [
            SEG_MOW_IST,
            # Вылет из AER, хотя предыдущий dest=IST
            {"origin_iata": "AER", "origin_name": "Сочи",
             "dest_iata": "LED", "dest_name": "СПб", "date": "15.03"},
        ]
        link = self.bml(segs, "1")
        params = link.split("params=")[1]
        # AER должен появиться как явный origin второго сегмента
        assert "AER" in params

    def test_empty_segments_returns_base_url(self):
        link = self.bml([], "1")
        assert "aviasales" in link.lower()

    def test_single_adult_link_format(self):
        link = self.bml(TWO_SEGS, "1")
        assert link.startswith("https://www.aviasales.ru/?params=")

    def test_two_adults_link(self):
        link = self.bml(TWO_SEGS, "2")
        params = link.split("params=")[1]
        assert params.endswith("2")


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 5 — _segments_summary
# ═════════════════════════════════════════════════════════════════════════════

class TestSegmentsSummary:
    def setup_method(self):
        from handlers.multi_search import _segments_summary
        self.ss = _segments_summary

    def test_two_segments(self):
        text = self.ss(TWO_SEGS)
        assert "Москва" in text
        assert "Стамбул" in text
        assert "Сочи" in text
        assert "10.03" in text
        assert "20.03" in text

    def test_numbered_lines(self):
        text = self.ss(TWO_SEGS)
        assert "1." in text
        assert "2." in text

    def test_three_segments(self):
        text = self.ss(THREE_SEGS)
        assert "3." in text

    def test_empty_segments(self):
        text = self.ss([])
        assert text == ""

    def test_arrow_separator(self):
        """Каждая строка содержит стрелку →."""
        for line in self.ss(TWO_SEGS).splitlines():
            assert "→" in line


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 6 — FSM: ms_origin (ввод города вылета)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsOrigin:
    async def _run(self, text: str, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_origin
            msg   = make_message(text)
            state = make_state(state_data or {"segments": []})
            await ms_origin(msg, state)
            return msg, state

    async def test_known_city_saves_iata(self):
        _, state = await self._run("Москва")
        assert state._data.get("_cur_origin_iata") == "MOW"
        assert state._data.get("_cur_origin_name") is not None

    async def test_known_city_transitions_to_dest(self):
        _, state = await self._run("Москва")
        assert "segment_dest" in str(state._data.get("__state__", ""))

    async def test_unknown_city_sends_error(self):
        msg, state = await self._run("НесуществующийГород123")
        msg.answer.assert_called()
        text = str(msg.answer.call_args_list[-1])
        assert "❌" in text or "❓" in text

    async def test_unknown_city_does_not_save_iata(self):
        _, state = await self._run("НесуществующийГород123")
        assert state._data.get("_cur_origin_iata") is None

    async def test_known_city_asks_for_dest(self):
        """После успешного ввода origin бот просит город прибытия."""
        msg, _ = await self._run("Москва")
        msg.answer.assert_called()
        answer_text = str(msg.answer.call_args_list[-1])
        assert "прибытия" in answer_text.lower() or "город" in answer_text.lower()

    async def test_iata_code_accepted(self):
        """IATA-код принимается напрямую."""
        _, state = await self._run("MOW")
        assert state._data.get("_cur_origin_iata") == "MOW"


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 7 — FSM: ms_dest (ввод города прибытия)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsDest:
    async def _run(self, text: str, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_dest
            msg   = make_message(text)
            state = make_state(state_data or {
                "segments": [],
                "_cur_origin_iata": "MOW",
                "_cur_origin_name": "Москва",
            })
            await ms_dest(msg, state)
            return msg, state

    async def test_known_city_saves_iata(self):
        # Сочи (AER) — гарантированно есть в MANUAL_ALIASES (без API)
        _, state = await self._run("Сочи", {
            "segments": [],
            "_cur_origin_iata": "MOW",
            "_cur_origin_name": "Москва",
        })
        assert state._data.get("_cur_dest_iata") == "AER"

    async def test_known_city_transitions_to_date(self):
        _, state = await self._run("Сочи")
        assert "segment_date" in str(state._data.get("__state__", ""))

    async def test_same_as_origin_blocked(self):
        """Город прибытия не может совпадать с вылетом."""
        msg, state = await self._run(
            "Москва",
            {"segments": [], "_cur_origin_iata": "MOW", "_cur_origin_name": "Москва"},
        )
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])
        assert state._data.get("_cur_dest_iata") is None

    async def test_unknown_city_sends_error(self):
        msg, _ = await self._run("НесуществующийГород456")
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1]) or "❓" in str(msg.answer.call_args_list[-1])

    async def test_valid_dest_asks_for_date(self):
        msg, _ = await self._run("Сочи")
        msg.answer.assert_called()
        text = str(msg.answer.call_args_list[-1])
        assert "дату" in text.lower() or "дд.мм" in text.lower()


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 8 — FSM: ms_date (ввод даты)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsDate:
    def _base_state(self, segments=None):
        return {
            "segments":          segments or [],
            "_cur_origin_iata":  "MOW",
            "_cur_origin_name":  "Москва",
            "_cur_dest_iata":    "AER",
            "_cur_dest_name":    "Сочи",
        }

    async def _run(self, date_text: str, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_date
            msg   = make_message(date_text)
            state = make_state(state_data or self._base_state())
            await ms_date(msg, state)
            return msg, state

    async def test_valid_date_appends_segment(self):
        _, state = await self._run("15.03")
        segs = state._data.get("segments", [])
        assert len(segs) == 1
        assert segs[0]["origin_iata"] == "MOW"
        assert segs[0]["dest_iata"]   == "AER"
        assert segs[0]["date"]        == "15.03"

    async def test_invalid_date_sends_error(self):
        msg, state = await self._run("99.99")
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])
        assert len(state._data.get("segments", [])) == 0

    async def test_invalid_format_blocked(self):
        msg, _ = await self._run("abc")
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])

    async def test_date_before_previous_blocked(self):
        """Дата нового сегмента не может быть раньше предыдущего."""
        existing = [dict(SEG_MOW_IST)]  # date = 10.03
        msg, state = await self._run(
            "05.03",  # раньше 10.03
            self._base_state(segments=existing),
        )
        msg.answer.assert_called()
        assert "❌" in str(msg.answer.call_args_list[-1])
        # Сегмент не добавлен
        assert len(state._data.get("segments", [])) == 1

    async def test_date_same_as_previous_allowed(self):
        """Одинаковая дата разрешена (пересадка в тот же день)."""
        existing = [dict(SEG_MOW_IST)]  # date = 10.03
        _, state = await self._run("10.03", self._base_state(segments=existing))
        assert len(state._data.get("segments", [])) == 2

    async def test_first_segment_shows_add_button(self):
        """После первого сегмента показывается кнопка 'Добавить перелёт'."""
        msg, _ = await self._run("15.03")
        msg.answer.assert_called()
        text = str(msg.answer.call_args_list[-1])
        assert "Добавить" in text or "перелёт" in text.lower()

    async def test_second_segment_shows_done_button(self):
        """После второго сегмента появляется кнопка 'Завершить маршрут'."""
        existing = [dict(SEG_MOW_IST)]
        msg, state = await self._run("20.03", self._base_state(segments=existing))
        msg.answer.assert_called()
        text = str(msg.answer.call_args_list[-1])
        assert "Завершить" in text

    async def test_sixth_segment_triggers_adults(self):
        """При достижении MAX_SEGMENTS=6 переходим к выбору пассажиров."""
        from handlers.multi_search import MAX_SEGMENTS
        five_segs = [
            {"origin_iata": "MOW", "origin_name": "М", "dest_iata": "IST", "dest_name": "И", "date": f"0{i}.03"}
            for i in range(1, MAX_SEGMENTS)
        ]
        _, state = await self._run(
            "15.03",
            self._base_state(segments=five_segs),
        )
        assert "adults" in str(state._data.get("__state__", ""))


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 9 — Кнопки: ms_add_segment и ms_done_segments
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestSegmentButtons:
    async def _run_add(self, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_add_segment
            cb    = make_callback("ms_add_segment")
            state = make_state(state_data or {"segments": list(TWO_SEGS)})
            await ms_add_segment(cb, state)
            return cb, state

    async def _run_done(self, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_done_segments
            cb    = make_callback("ms_done_segments")
            state = make_state(state_data or {"segments": list(TWO_SEGS)})
            await ms_done_segments(cb, state)
            return cb, state

    async def test_add_segment_prefills_origin_from_last_dest(self):
        """ms_add_segment берёт город вылета из последнего dest."""
        _, state = await self._run_add({"segments": list(TWO_SEGS)})
        # Последний dest TWO_SEGS = AER
        assert state._data.get("_cur_origin_iata") == "AER"

    async def test_add_segment_transitions_to_dest(self):
        _, state = await self._run_add({"segments": list(TWO_SEGS)})
        assert "segment_dest" in str(state._data.get("__state__", ""))

    async def test_add_segment_answers_callback(self):
        cb, _ = await self._run_add()
        cb.answer.assert_called()

    async def test_done_with_two_segments_proceeds(self):
        """ms_done_segments с двумя сегментами — переходим к пассажирам."""
        cb, state = await self._run_done({"segments": list(TWO_SEGS)})
        cb.answer.assert_called()
        assert "adults" in str(state._data.get("__state__", ""))

    async def test_done_with_one_segment_blocks(self):
        """Нельзя завершить маршрут при одном сегменте."""
        cb, _ = await self._run_done({"segments": [SEG_MOW_IST]})
        cb.answer.assert_called()
        # show_alert=True — сообщение об ошибке
        last_call = str(cb.answer.call_args_list[-1])
        assert "2" in last_call or "хотя бы" in last_call.lower()

    async def test_done_with_zero_segments_blocks(self):
        cb, _ = await self._run_done({"segments": []})
        cb.answer.assert_called()


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 10 — Пассажиры: ms_adults
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsAdults:
    async def _run(self, n: int, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_adults
            cb    = make_callback(f"ms_adults_{n}")
            state = make_state(state_data or {"segments": list(TWO_SEGS)})
            await ms_adults(cb, state)
            return cb, state

    async def test_adults_saved(self):
        _, state = await self._run(2)
        assert state._data.get("adults") == 2

    async def test_answers_callback(self):
        cb, _ = await self._run(3)
        cb.answer.assert_called()

    async def test_nine_adults_skips_children(self):
        """9 взрослых — сразу переходим к summary (children=0, infants=0)."""
        _, state = await self._run(9)
        assert state._data.get("children") == 0
        assert state._data.get("infants")  == 0
        assert state._data.get("pax_code") == "9"

    async def test_nine_adults_goes_to_confirm(self):
        _, state = await self._run(9)
        assert "confirm" in str(state._data.get("__state__", ""))

    async def test_less_than_nine_asks_children(self):
        """Меньше 9 — переходим к вопросу о детях."""
        _, state = await self._run(2)
        assert "has_children" in str(state._data.get("__state__", ""))

    async def test_adults_1_valid(self):
        _, state = await self._run(1)
        assert state._data.get("adults") == 1


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 11 — Пассажиры: ms_has_children, ms_children, ms_infants
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestPassengerSteps:
    async def _run_hc(self, data: str, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_has_children
            cb    = make_callback(data)
            state = make_state(state_data or {"adults": 2, "segments": list(TWO_SEGS)})
            await ms_has_children(cb, state)
            return cb, state

    async def _run_ch(self, n: int, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_children
            cb    = make_callback(f"ms_ch_{n}")
            state = make_state(state_data or {"adults": 2, "segments": list(TWO_SEGS)})
            await ms_children(cb, state)
            return cb, state

    async def _run_inf(self, n: int, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_infants
            cb    = make_callback(f"ms_inf_{n}")
            state = make_state(state_data or {"adults": 2, "children": 1, "segments": list(TWO_SEGS)})
            await ms_infants(cb, state)
            return cb, state

    # ── has_children ────────────────────────────────────────────

    async def test_hc_no_sets_zero_children(self):
        _, state = await self._run_hc("ms_hc_no")
        assert state._data.get("children") == 0
        assert state._data.get("infants")  == 0

    async def test_hc_no_saves_pax_code(self):
        _, state = await self._run_hc("ms_hc_no")
        assert state._data.get("pax_code") == "2"

    async def test_hc_no_goes_to_confirm(self):
        _, state = await self._run_hc("ms_hc_no")
        assert "confirm" in str(state._data.get("__state__", ""))

    async def test_hc_yes_asks_children_count(self):
        _, state = await self._run_hc("ms_hc_yes")
        assert "children" in str(state._data.get("__state__", ""))

    # ── children ────────────────────────────────────────────────

    async def test_children_saved(self):
        _, state = await self._run_ch(2, {"adults": 2, "segments": list(TWO_SEGS)})
        assert state._data.get("children") == 2

    async def test_children_over_limit_ignored(self):
        """children > 9 - adults → игнорируется, сегменты не меняются."""
        _, state = await self._run_ch(8, {"adults": 2, "segments": list(TWO_SEGS)})
        # children не должен стать 8 (макс: 9-2=7)
        assert state._data.get("children", 0) != 8

    async def test_children_asks_infants_when_capacity(self):
        """Есть место для младенцев — задаём вопрос о них."""
        _, state = await self._run_ch(1, {"adults": 2, "segments": list(TWO_SEGS)})
        assert "infants" in str(state._data.get("__state__", ""))

    async def test_children_fills_capacity_skips_infants(self):
        """Заполнена квота (adults+children=9) — пропускаем шаг младенцев."""
        _, state = await self._run_ch(7, {"adults": 2, "segments": list(TWO_SEGS)})
        assert "confirm" in str(state._data.get("__state__", ""))

    # ── infants ─────────────────────────────────────────────────

    async def test_infants_saved(self):
        _, state = await self._run_inf(1)
        assert state._data.get("infants") == 1

    async def test_infants_builds_pax_code(self):
        _, state = await self._run_inf(1, {"adults": 2, "children": 1, "segments": list(TWO_SEGS)})
        assert state._data.get("pax_code") == "211"

    async def test_infants_over_limit_ignored(self):
        """Младенцев > adults → игнорируется."""
        _, state = await self._run_inf(5, {"adults": 1, "children": 0, "segments": list(TWO_SEGS)})
        assert state._data.get("infants", 0) != 5

    async def test_infants_goes_to_confirm(self):
        _, state = await self._run_inf(1)
        assert "confirm" in str(state._data.get("__state__", ""))


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 12 — ms_edit_pax
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsEditPax:
    async def test_edit_pax_goes_to_adults(self):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_edit_pax
            cb    = make_callback("ms_edit_pax")
            state = make_state({"segments": list(TWO_SEGS), "adults": 2})
            await ms_edit_pax(cb, state)

        cb.answer.assert_called()
        assert "adults" in str(state._data.get("__state__", ""))


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 13 — ms_confirm (финальный шаг — генерация ссылки)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestMsConfirm:
    def _state_data(self, segments=None, pax_code="1", pax_desc="1 взр."):
        return {
            "segments": segments or list(TWO_SEGS),
            "pax_code": pax_code,
            "pax_desc": pax_desc,
        }

    async def _run(self, state_data: dict = None):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            from handlers.multi_search import ms_confirm
            cb    = make_callback("ms_confirm")
            state = make_state(state_data or self._state_data())
            await ms_confirm(cb, state)
            return cb, state

    async def test_clears_state(self):
        _, state = await self._run()
        assert state._data == {}

    async def test_shows_result_message(self):
        cb, _ = await self._run()
        cb.message.edit_text.assert_called()

    async def test_result_contains_aviasales_button(self):
        """Результат содержит кнопку-ссылку на Aviasales."""
        cb, _ = await self._run()
        last_call = cb.message.edit_text.call_args_list[-1]
        kb = last_call.kwargs.get("reply_markup")
        assert kb is not None
        flat_urls = [btn.url or "" for row in kb.inline_keyboard for btn in row]
        assert any("aviasales" in u.lower() for u in flat_urls)

    async def test_result_message_contains_route(self):
        """В тексте результата есть информация о маршруте."""
        cb, _ = await self._run()
        last_text = str(cb.message.edit_text.call_args_list[-1])
        assert "Москва" in last_text or "MOW" in last_text

    async def test_result_message_contains_pax(self):
        cb, _ = await self._run(self._state_data(pax_desc="2 взр., 1 дет."))
        last_text = str(cb.message.edit_text.call_args_list[-1])
        assert "2 взр." in last_text or "взр" in last_text.lower()

    async def test_tracks_search_type(self):
        """Аналитика: track_search_type('multi') вызывается."""
        await self._run()
        # ensure_future запускает track_search_type асинхронно — даём event loop шанс
        await asyncio.sleep(0)
        PATCHES["redis"].track_search_type.assert_called_with("multi")

    async def test_answers_callback(self):
        cb, _ = await self._run()
        cb.answer.assert_called()

    async def test_two_adults_link(self):
        """pax_code='2' → ссылка содержит '2' в params."""
        cb, _ = await self._run(self._state_data(pax_code="2", pax_desc="2 взр."))
        last_text = str(cb.message.edit_text.call_args_list[-1])
        assert "2" in last_text

    async def test_partner_link_called(self):
        """convert_to_partner_link вызывается для получения партнёрской ссылки."""
        await self._run()
        PATCHES["convert_to_partner_link"].assert_called()


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 14 — Интеграционный: полный сценарий 2 сегмента + 2 взрослых
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
class TestFullMultiScenario:
    """
    Полный FSM-сценарий:
      1. Ввод origin сегмента 1 (Москва)
      2. Ввод dest сегмента 1 (Сочи)
      3. Ввод даты сегмента 1 (10.06)
      4. Ввод origin сегмента 2 (Сочи)
      5. Ввод dest сегмента 2 (Питер)
      6. Ввод даты сегмента 2 (20.06)
      7. ms_done_segments
      8. ms_adults_2
      9. ms_hc_no
      10. ms_confirm
    """

    async def _step(self, coro):
        import contextlib
        with contextlib.ExitStack() as stack:
            for p in _std_patches():
                stack.enter_context(p)
            await coro

    async def test_full_two_segment_flow(self):
        state = make_state({"segments": []})

        from handlers.multi_search import (
            ms_origin, ms_dest, ms_date,
            ms_done_segments, ms_adults, ms_has_children, ms_confirm,
        )

        # Шаг 1: origin сегмента 1
        await self._step(ms_origin(make_message("Москва"), state))
        assert state._data.get("_cur_origin_iata") == "MOW"

        # Шаг 2: dest сегмента 1
        await self._step(ms_dest(make_message("Сочи"), state))
        assert state._data.get("_cur_dest_iata") == "AER"

        # Шаг 3: дата сегмента 1
        await self._step(ms_date(make_message("10.06"), state))
        assert len(state._data.get("segments", [])) == 1

        # Шаг 4: origin сегмента 2 (после ms_add_segment origin подставляется авто,
        #         но здесь проверяем ручной ввод)
        await self._step(ms_origin(make_message("Сочи"), state))
        assert state._data.get("_cur_origin_iata") == "AER"

        # Шаг 5: dest сегмента 2
        await self._step(ms_dest(make_message("Санкт-Петербург"), state))
        assert state._data.get("_cur_dest_iata") == "LED"

        # Шаг 6: дата сегмента 2
        await self._step(ms_date(make_message("20.06"), state))
        assert len(state._data.get("segments", [])) == 2

        # Шаг 7: завершение маршрута
        cb_done = make_callback("ms_done_segments")
        await self._step(ms_done_segments(cb_done, state))
        assert "adults" in str(state._data.get("__state__", ""))

        # Шаг 8: 2 взрослых
        cb_adults = make_callback("ms_adults_2")
        await self._step(ms_adults(cb_adults, state))
        assert state._data.get("adults") == 2

        # Шаг 9: без детей
        cb_hc = make_callback("ms_hc_no")
        await self._step(ms_has_children(cb_hc, state))
        assert state._data.get("children") == 0
        assert state._data.get("pax_code") == "2"

        # Шаг 10: подтверждение и генерация ссылки
        cb_confirm = make_callback("ms_confirm")
        await self._step(ms_confirm(cb_confirm, state))
        cb_confirm.message.edit_text.assert_called()

        # Итог: state очищен, ссылка отправлена
        assert state._data == {}
        last_kb = cb_confirm.message.edit_text.call_args_list[-1].kwargs.get("reply_markup")
        assert last_kb is not None
        all_urls = [btn.url or "" for row in last_kb.inline_keyboard for btn in row]
        assert any("aviasales" in u.lower() for u in all_urls)
        # Проверяем что ссылка содержит правильные IATA
        aviasales_url = next(u for u in all_urls if "aviasales" in u.lower())
        assert "MOW" in aviasales_url
        assert "AER" in aviasales_url
        assert "LED" in aviasales_url


# ═════════════════════════════════════════════════════════════════════════════
# БЛОК 15 — Smoke-тест импортов
# ═════════════════════════════════════════════════════════════════════════════

class TestImports:
    def test_multi_search_imports(self):
        import handlers.multi_search  # noqa

    def test_router_exists(self):
        from handlers.multi_search import router
        assert router is not None

    def test_multi_search_fsm_states(self):
        from handlers.multi_search import MultiSearch
        assert hasattr(MultiSearch, "segment_origin")
        assert hasattr(MultiSearch, "segment_dest")
        assert hasattr(MultiSearch, "segment_date")
        assert hasattr(MultiSearch, "adults")
        assert hasattr(MultiSearch, "confirm")

    def test_start_multi_search_callable(self):
        from handlers.multi_search import start_multi_search
        import inspect
        assert inspect.iscoroutinefunction(start_multi_search)

    def test_build_multi_link_importable(self):
        from handlers.multi_search import _build_multi_link
        assert callable(_build_multi_link)

    def test_build_pax_code_importable(self):
        from handlers.multi_search import _build_pax_code
        assert callable(_build_pax_code)


# ─────────────────────────────────────────────────────────────────────────────
# Запуск напрямую
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v", "--tb=short"],
        capture_output=False,
    )
    sys.exit(result.returncode)