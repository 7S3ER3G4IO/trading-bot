"""
tests/test_risk_manager.py — Tests unitaires RiskManager
"""
import pytest
from risk_manager import RiskManager, MAX_PORTFOLIO_RISK, MAX_OPEN_POSITIONS


@pytest.fixture
def rm():
    return RiskManager(initial_balance=100_000.0)


# ─── DD journalier ───────────────────────────────────────────────

class TestDailyDrawdown:
    def test_no_dd_ok(self, rm):
        assert rm.can_open_trade(100_000.0) is True

    def test_dd_at_limit_blocked(self, rm):
        """95 000 = -5% → bloqué par le daily DD."""
        rm.daily_start_balance = 100_000.0
        assert rm.can_open_trade(95_000.0) is False

    def test_small_dd_ok(self, rm):
        """98 000 = -2% → autorisé."""
        rm.daily_start_balance = 100_000.0
        assert rm.can_open_trade(98_000.0) is True

    def test_total_dd_kill_switch(self, rm):
        """DD >= 10% depuis balance initiale → HALT définitif."""
        assert rm.can_open_trade(89_999.0) is False


# ─── Kill Switches ────────────────────────────────────────────────

class TestKillSwitches:
    def test_3_losses_triggers_pause(self, rm):
        rm.record_loss("EURUSD", "forex")
        rm.record_loss("GBPUSD", "forex")
        rm.record_loss("USDJPY", "forex")
        blocked, reason = rm.check_kill_switches(99_000.0)
        assert blocked is True

    def test_2_losses_no_pause(self, rm):
        rm.record_loss("EURUSD", "forex")
        rm.record_loss("GBPUSD", "forex")
        blocked, _ = rm.check_kill_switches(99_500.0)
        assert blocked is False

    def test_category_blocked_after_5_losses(self, rm):
        for _ in range(5):
            rm.record_loss("EURUSD", "forex")
        assert rm.is_category_blocked("forex") is True

    def test_different_category_not_blocked(self, rm):
        for _ in range(5):
            rm.record_loss("EURUSD", "forex")
        assert rm.is_category_blocked("indices") is False


# ─── Portfolio Heat ───────────────────────────────────────────────

class TestPortfolioHeat:
    def test_max_positions_blocked(self, rm):
        open_trades = {f"PAIR{i}": {"direction": "BUY"} for i in range(MAX_OPEN_POSITIONS)}
        ok, _ = rm.portfolio_heat_check("AUDCAD", "BUY", open_trades)
        assert ok is False

    def test_empty_book_ok(self, rm):
        ok, _ = rm.portfolio_heat_check("EURUSD", "BUY", {})
        assert ok is True


# ─── Compteurs ────────────────────────────────────────────────────

class TestCounters:
    def test_reset_daily(self, rm):
        rm.record_loss("EURUSD", "forex")
        rm.reset_daily(99_000.0)
        assert rm.daily_start_balance == 99_000.0
