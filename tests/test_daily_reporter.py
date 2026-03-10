"""
tests/test_daily_reporter.py — Tests unitaires du reporter journalier.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import json
import pytest
from daily_reporter import DailyReporter, CFD_FEE_RATE


class TestDailyReporter:
    def setup_method(self, method):
        # Fichier temporaire pour les tests
        self.reporter = DailyReporter()
        self.reporter._trades = []         # Reset trades journaliers
        self.reporter._weekly_trades = []  # Reset trades hebdo (BUG FIX : évite la lecture du disk)

    # ─── record_trade ───────────────────────────────────────────────────────

    def test_record_trade_adds_entry(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP3", 450.0, 50000, 50900, 0.03)
        assert len(self.reporter._trades) == 1

    def test_record_trade_fees_calculated(self):
        amount = 0.01
        entry  = 50000.0
        self.reporter.record_trade("BTC/USDT", "BUY", "TP1", 150.0, entry, 50450, amount)
        trade = self.reporter._trades[0]
        # CFD Capital.com : pas de commission séparée — CFD_FEE_RATE = 0.0
        expected_fees = round(entry * amount * CFD_FEE_RATE * 2, 4)
        assert abs(trade.fees - expected_fees) < 0.001  # doit être 0.0

    def test_record_trade_pnl_net_less_than_gross(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP1", 150.0, 50000, 50450, 0.01)
        trade = self.reporter._trades[0]
        # CFD_FEE_RATE = 0.0 → pnl_net == pnl_gross (pas de commission)
        assert trade.pnl_net == trade.pnl_gross

    def test_multiple_trades_accumulated(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP1", 150.0, 50000, 50450, 0.01)
        self.reporter.record_trade("ETH/USDT", "SELL", "SL", -100.0, 3000, 2970, 0.1)
        assert len(self.reporter._trades) == 2

    # ─── build_report ────────────────────────────────────────────────────────

    def test_build_report_empty(self):
        report = self.reporter.build_report()
        assert "Aucun trade" in report

    def test_build_report_contains_win_rate(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP1", 100.0, 50000, 50200, 0.02)
        self.reporter.record_trade("ETH/USDT", "BUY", "SL", -50.0, 3000, 2990, 0.1)
        report = self.reporter.build_report()
        assert "1/2" in report   # 1 win / 2 trades

    def test_build_report_shows_fees(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP2", 200.0, 50000, 50400, 0.02)
        report = self.reporter.build_report()
        assert "Frais" in report

    def test_build_report_shows_net_pnl(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP3", 450.0, 50000, 50900, 0.03)
        report = self.reporter.build_report()
        assert "net" in report.lower() or "PnL" in report

    # ─── reset_for_new_day ───────────────────────────────────────────────────

    def test_reset_clears_trades(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP1", 100.0, 50000, 50200, 0.02)
        self.reporter.reset_for_new_day()
        assert len(self.reporter._trades) == 0

    def test_reset_allows_new_report(self):
        self.reporter.mark_report_sent()
        self.reporter.reset_for_new_day()
        assert not self.reporter._report_sent_today

    # ─── weekly report ───────────────────────────────────────────────────────

    def test_weekly_report_empty(self):
        report = self.reporter.build_weekly_report()
        assert "Aucun trade" in report

    def test_weekly_report_shows_best_trade(self):
        self.reporter.record_trade("BTC/USDT", "BUY", "TP3", 600.0, 50000, 51200, 0.04)
        self.reporter.record_trade("ETH/USDT", "BUY", "SL", -80.0, 3000, 2984, 0.1)
        report = self.reporter.build_weekly_report()
        assert "Meilleur" in report
