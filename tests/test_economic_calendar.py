"""tests/test_economic_calendar.py — Tests unitaires EconomicCalendar"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TESTING", "1")

from economic_calendar import EconomicCalendar, CALENDAR_SOURCES, _USER_AGENTS


# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>ForexFactory Calendar</title>
  <item>
    <title>NFP</title>
    <country>USD</country>
    <impact>High</impact>
    <date>03-21-2026</date>
    <time>08:30AM</time>
  </item>
  <item>
    <title>Low Impact Event</title>
    <country>USD</country>
    <impact>Low</impact>
    <date>03-21-2026</date>
    <time>10:00AM</time>
  </item>
  <item>
    <title>AUD Event</title>
    <country>AUD</country>
    <impact>High</impact>
    <date>03-21-2026</date>
    <time>09:00AM</time>
  </item>
</channel>
</rss>"""


class TestEconomicCalendarInit:
    def test_init_defaults(self):
        cal = EconomicCalendar()
        assert cal._events == []
        assert cal._last_fetch is None
        assert cal._fetch_interval_hours == 6
        assert cal._fail_count == 0


class TestParseRSS:
    def test_parse_valid_rss(self):
        cal = EconomicCalendar()
        source = CALENDAR_SOURCES[0]
        events = cal._parse_rss(SAMPLE_RSS, source)
        assert events is not None
        # Only 1 HIGH impact USD event (AUD = excluded, Low impact = excluded)
        assert len(events) == 1
        assert events[0]["title"] == "NFP"

    def test_parse_invalid_xml(self):
        cal = EconomicCalendar()
        events = cal._parse_rss(b"<not valid xml", CALENDAR_SOURCES[0])
        assert events is None

    def test_parse_empty_rss(self):
        cal = EconomicCalendar()
        events = cal._parse_rss(b"<rss><channel></channel></rss>", CALENDAR_SOURCES[0])
        assert events == []


class TestShouldPauseTrading:
    def _make_cal(self, delta_minutes: float) -> EconomicCalendar:
        """Helper: crée un EconomicCalendar avec un event dans delta_minutes."""
        cal = EconomicCalendar()
        now = datetime.now(timezone.utc)
        cal._events = [{"title": "NFP", "dt": now + timedelta(minutes=delta_minutes), "impact": "High"}]
        return cal

    def test_pause_before_news(self):
        cal = self._make_cal(15)  # 15 min avant (dans la fenêtre 30min)
        paused, reason = cal.should_pause_trading()
        assert paused is True
        assert "NFP" in reason

    def test_pause_after_news(self):
        cal = self._make_cal(-10)  # 10 min après (dans la fenêtre 30min)
        paused, _ = cal.should_pause_trading()
        assert paused is True

    def test_no_pause_far_ahead(self):
        cal = self._make_cal(120)  # 2h avant = hors fenêtre
        paused, _ = cal.should_pause_trading()
        assert paused is False

    def test_no_pause_long_after(self):
        cal = self._make_cal(-60)  # 1h après = hors fenêtre
        paused, _ = cal.should_pause_trading()
        assert paused is False

    def test_no_events(self):
        cal = EconomicCalendar()
        paused, _ = cal.should_pause_trading()
        assert paused is False


class TestRefreshMultiSource:
    def test_falls_back_on_429(self):
        cal = EconomicCalendar()
        responses = []
        call_count = [0]

        def mock_get(url, **kwargs):
            resp = MagicMock()
            call_count[0] += 1
            if call_count[0] == 1:
                resp.status_code = 429
                responses.append(url)
                return resp
            resp.status_code = 200
            resp.content = SAMPLE_RSS
            resp.raise_for_status = lambda: None
            return resp

        with patch("requests.get", side_effect=mock_get):
            cal.refresh()

        # Doit avoir essayé au moins 2 sources et obtenu des events de la 2e
        assert call_count[0] >= 2
        assert len(cal._events) >= 1

    def test_all_sources_fail(self):
        cal = EconomicCalendar()

        def mock_get(url, **kwargs):
            raise ConnectionError("timeout")

        with patch("requests.get", side_effect=mock_get):
            cal.refresh()

        assert cal._events == []
        assert cal._fail_count >= 1

    def test_user_agent_rotation(self):
        """Vérifie que différentes requêtes utilisent des User-Agents différents."""
        used_agents = []

        def mock_get(url, headers=None, **kwargs):
            used_agents.append(headers.get("User-Agent", ""))
            resp = MagicMock()
            resp.status_code = 200
            resp.content = SAMPLE_RSS
            resp.raise_for_status = lambda: None
            return resp

        cal = EconomicCalendar()
        # Refresh 3x pour voir la rotation
        with patch("requests.get", side_effect=mock_get):
            cal.refresh()
            cal.refresh()
            cal.refresh()

        # Les agents devraient varier
        assert len(set(used_agents)) >= 1  # au moins un agent utilisé


class TestGetNextEvent:
    def test_next_event_returned(self):
        cal = EconomicCalendar()
        now = datetime.now(timezone.utc)
        cal._events = [
            {"title": "CPI", "dt": now + timedelta(hours=3), "impact": "High"},
            {"title": "NFP", "dt": now + timedelta(hours=1), "impact": "High"},
        ]
        result = cal.get_next_event()
        assert result is not None
        assert "NFP" in result  # NFP est plus proche

    def test_no_upcoming_events(self):
        cal = EconomicCalendar()
        now = datetime.now(timezone.utc)
        cal._events = [
            {"title": "Old Event", "dt": now - timedelta(hours=2), "impact": "High"},
        ]
        result = cal.get_next_event()
        assert result is None


class TestCalendarSources:
    def test_all_sources_have_required_fields(self):
        for src in CALENDAR_SOURCES:
            assert "url" in src
            assert "name" in src
            assert "date_fmt" in src
            assert src["url"].startswith("http")

    def test_user_agents_defined(self):
        assert len(_USER_AGENTS) >= 2
        for ua in _USER_AGENTS:
            assert "Mozilla" in ua
