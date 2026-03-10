"""
tests/test_drift_detector.py — Tests unitaires du DriftDetector.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from drift_detector import DriftDetector


def make_dd():
    """Fresh DriftDetector with no file state (patch DRIFT_FILE)."""
    tmp = "/tmp/test_drift_empty.json"
    if os.path.exists(tmp):
        os.unlink(tmp)
    with patch("drift_detector.DRIFT_FILE", tmp):
        dd = DriftDetector()
    return dd


class TestDriftDetector:
    def setup_method(self):
        self.dd = make_dd()

    def test_record_trade_no_crash(self):
        self.dd.record_trade(50.0, True,  "GOLD")
        self.dd.record_trade(-25.0, False, "GOLD")

    def test_check_drift_returns_dict(self):
        for i in range(20):
            win = (i % 2 == 0)
            self.dd.record_trade(50.0 if win else -25.0, win, "GOLD")
        result = self.dd.check_drift()
        assert isinstance(result, dict)
        assert "drift" in result

    def test_check_drift_no_crash(self):
        self.dd.check_drift()  # should not raise with 0 trades

    def test_format_status(self):
        status = self.dd.format_status()
        assert isinstance(status, str)

    def test_mixed_instruments_no_crash(self):
        self.dd.record_trade(100.0, True,  "GOLD")
        self.dd.record_trade(-30.0, False, "USDJPY")
        self.dd.check_drift()

    def test_needs_reoptimization(self):
        result = self.dd.needs_reoptimization()
        assert isinstance(result, bool)
