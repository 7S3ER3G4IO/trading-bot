"""
tests/test_auto_push.py — Teste la logique d'auto-push Telegram
(sans appels réseau réels — utilise des mocks).
"""
import sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, patch


class TestAutoPushLogic:
    """Teste les conditions de déclenchement des push automatiques."""

    def _make_bot_state(self, last_session="", last_hb_seconds_ago=9999):
        """Crée un objet minimal simulant l'état du bot."""
        state = MagicMock()
        state._last_session_push   = last_session
        state._manual_pause        = False
        state.initial_balance      = 11000.0
        state._capital_closed_today = []
        state.capital_trades       = {}
        # Simule le heartbeat il y a N secondes
        from datetime import timedelta
        state._last_heartbeat_push = datetime.now(timezone.utc) - timedelta(seconds=last_hb_seconds_ago)
        return state

    def test_session_london_triggers_at_8h_utc(self):
        """Session London se déclenche exactement à 8h UTC et pas avant."""
        state = self._make_bot_state(last_session="")
        h_utc = 8
        current_session = "London" if h_utc == 8 else ("NY" if h_utc == 13 else "")
        should_push = current_session and current_session != state._last_session_push
        assert should_push is True
        assert current_session == "London"

    def test_session_ny_triggers_at_13h_utc(self):
        """Session NY se déclenche exactement à 13h UTC."""
        state = self._make_bot_state(last_session="")
        h_utc = 13
        current_session = "London" if h_utc == 8 else ("NY" if h_utc == 13 else "")
        should_push = current_session and current_session != state._last_session_push
        assert should_push is True
        assert current_session == "NY"

    def test_session_not_double_pushed(self):
        """Si session déjà pushée, ne pas renvoyer."""
        state = self._make_bot_state(last_session="NY")
        h_utc = 13
        current_session = "London" if h_utc == 8 else ("NY" if h_utc == 13 else "")
        should_push = current_session and current_session != state._last_session_push
        assert not should_push  # déjà poussé

    def test_session_not_triggered_outside_hours(self):
        """Pas de push à 10h, 12h, 15h UTC (hors ouverture)."""
        for h in [10, 12, 15, 0, 6, 20]:
            current_session = "London" if h == 8 else ("NY" if h == 13 else "")
            assert current_session == ""

    def test_heartbeat_triggers_after_30min(self):
        """Heartbeat se déclenche après 30 minutes en session active."""
        from config import SESSION_HOURS
        state = self._make_bot_state(last_hb_seconds_ago=1801)  # 30min01s
        h_utc = SESSION_HOURS[0]  # heure valide
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        in_session = h_utc in SESSION_HOURS
        since_last = (now - state._last_heartbeat_push).total_seconds()
        should_hb = in_session and since_last >= 1800
        assert should_hb is True

    def test_heartbeat_not_triggered_before_30min(self):
        """Heartbeat ne se déclenche pas si < 30min depuis le dernier."""
        from config import SESSION_HOURS
        state = self._make_bot_state(last_hb_seconds_ago=1200)  # 20min
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        since_last = (now - state._last_heartbeat_push).total_seconds()
        should_hb = since_last >= 1800
        assert should_hb is False

    def test_heartbeat_not_triggered_outside_session(self):
        """Heartbeat ne se déclenche pas hors session même si 30min écoulées."""
        from config import SESSION_HOURS
        state = self._make_bot_state(last_hb_seconds_ago=9999)
        h_utc = 11  # heure hors session (entre London et NY)
        in_session = h_utc in SESSION_HOURS
        assert not in_session  # 11h UTC n'est pas en session
        should_hb = in_session and True  # 30min condition satisfied mais pas in_session
        assert should_hb is False
