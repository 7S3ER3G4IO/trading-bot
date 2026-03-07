"""
tests/test_risk_manager.py — Tests unitaires du gestionnaire de risque.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from risk_manager import RiskManager
from strategy import SIGNAL_BUY, SIGNAL_SELL


class TestRiskManager:
    def setup_method(self, method):
        self.rm = RiskManager(10_000.0)

    # ─── calculate_levels ───────────────────────────────────────────────────

    def test_buy_levels_tp_above_entry(self):
        levels = self.rm.calculate_levels(50000, atr=500, side=SIGNAL_BUY)
        assert levels["tp1"] > 50000
        assert levels["tp2"] > levels["tp1"]
        assert levels["tp3"] > levels["tp2"]

    def test_buy_levels_sl_below_entry(self):
        levels = self.rm.calculate_levels(50000, atr=500, side=SIGNAL_BUY)
        assert levels["sl"] < 50000

    def test_sell_levels_tp_below_entry(self):
        levels = self.rm.calculate_levels(50000, atr=500, side=SIGNAL_SELL)
        assert levels["tp1"] < 50000
        assert levels["tp2"] < levels["tp1"]
        assert levels["tp3"] < levels["tp2"]

    def test_sell_levels_sl_above_entry(self):
        levels = self.rm.calculate_levels(50000, atr=500, side=SIGNAL_SELL)
        assert levels["sl"] > 50000

    def test_be_equals_entry(self):
        """Break Even doit être au niveau du prix d'entrée."""
        levels = self.rm.calculate_levels(50000, atr=500, side=SIGNAL_BUY)
        assert levels["be"] == 50000

    # ─── position_size ───────────────────────────────────────────────────────

    def test_position_size_positive(self):
        size = self.rm.position_size(10_000, 50000, 49000)
        assert size > 0

    def test_position_size_respects_risk_percent(self):
        """La perte max si SL touché doit ≈ 1% du capital."""
        capital = 10_000
        price   = 50_000
        sl      = 49_000  # 2% de SL
        size    = self.rm.position_size(capital, price, sl)
        potential_loss = abs(price - sl) * size
        # Doit être proche de 1% de 10000 = 100 USDT
        assert 10 < potential_loss < 300

    def test_position_size_zero_if_sl_equals_price(self):
        """Si SL == price, aucune position ne doit s'ouvrir."""
        size = self.rm.position_size(10_000, 50000, 50000)
        assert size == 0

    def test_position_size_zero_if_no_capital(self):
        size = self.rm.position_size(0, 50000, 49000)
        assert size == 0

    # ─── can_open_trade ──────────────────────────────────────────────────────

    def test_can_open_trade_initially_true(self):
        rm = RiskManager(10_000.0)
        assert rm.can_open_trade(10_000) is True

    def test_cannot_open_trade_with_low_balance(self):
        """Pas de trade si solde < 200 USDT."""
        rm = RiskManager(10_000.0)
        result = rm.can_open_trade(100)
        assert result is False

    def test_can_open_trade_respects_max_trades(self):
        """Ne peut pas ouvrir plus de MAX_TRADES simultanément."""
        rm = RiskManager(10_000.0)
        for _ in range(10):  # Simuler l'ouverture de nombreux trades
            rm.on_trade_opened()
        result = rm.can_open_trade(10_000)
        assert result is False

    # ─── daily_pnl ─────────────────────────────────────────────────────────

    def test_reset_daily(self):
        rm = RiskManager(10_000.0)
        rm.on_trade_opened()
        rm.reset_daily(11_000)
        assert rm.can_open_trade(11_000) is True
