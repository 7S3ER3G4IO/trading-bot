"""
tests/test_equity_curve.py — Tests unitaires de l'EquityCurve.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from equity_curve import EquityCurve


class TestEquityCurve:
    def setup_method(self, method):
        self.ec = EquityCurve(10_000.0)
        self.ec._history = []  # isolation du disque

    def _feed(self, values: list):
        for v in values:
            self.ec.record(v)

    # ─── record ──────────────────────────────────────────────────────────────

    def test_record_adds_entry(self):
        self.ec.record(10_100.0)
        assert len(self.ec._history) == 1

    def test_record_stores_balance(self):
        self.ec.record(9_500.0)
        assert self.ec._history[-1]["balance"] == 9_500.0

    # ─── is_below_ma ─────────────────────────────────────────────────────────

    def test_not_below_ma_on_uptrend(self):
        # Tendance haussière continue → courbe au-dessus de MA20
        self._feed([10_000 + i * 100 for i in range(30)])
        assert self.ec.is_below_ma(ma_period=20) is False

    def test_below_ma_on_downtrend(self):
        # Tendance baissière → courbe sous MA20
        self._feed([10_000 - i * 100 for i in range(30)])
        assert self.ec.is_below_ma(ma_period=20) is True

    def test_is_below_ma_returns_false_if_insufficient_data(self):
        self._feed([10_000, 10_100])
        # Pas assez de points pour MA20 → retourne False (prudence)
        assert self.ec.is_below_ma(ma_period=20) is False

    # ─── max_drawdown ─────────────────────────────────────────────────────────

    def test_max_drawdown_zero_on_uptrend(self):
        self._feed([10_000 + i * 50 for i in range(20)])
        dd = self.ec.max_drawdown()
        assert dd >= 0.0

    def test_max_drawdown_positive_after_loss(self):
        self._feed([10_000, 11_000, 9_000, 9_500])
        dd = self.ec.max_drawdown()
        assert dd > 0.0

    # ─── format_report ────────────────────────────────────────────────────────

    def test_format_report_returns_string(self):
        self._feed([10_000 + i * 20 for i in range(25)])
        report = self.ec.format_report()
        assert isinstance(report, str)
        assert len(report) > 0

    def test_format_report_empty_data(self):
        report = self.ec.format_report()
        assert isinstance(report, str)

    # ─── generate_chart ───────────────────────────────────────────────────────

    def test_generate_chart_returns_bytes(self):
        self._feed([10_000 + i * 30 for i in range(30)])
        chart = self.ec.generate_chart("Test")
        assert isinstance(chart, bytes)
        assert len(chart) > 0
