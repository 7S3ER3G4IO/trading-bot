"""
tests/test_protection_model.py — Tests unitaires de ProtectionModel.
"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from protection_model import ProtectionModel


def make_pm():
    """Create a fresh ProtectionModel with no file state."""
    # Patch PROTECTION_FILE to a non-existent temp path so no state loads
    with patch("protection_model.PROTECTION_FILE", "/tmp/test_prot_model_empty.json"):
        if os.path.exists("/tmp/test_prot_model_empty.json"):
            os.unlink("/tmp/test_prot_model_empty.json")
        pm = ProtectionModel()
    return pm


class TestProtectionModel:
    def setup_method(self):
        self.pm = make_pm()

    def test_initial_state(self):
        assert not self.pm.is_blocked("GOLD")
        assert not self.pm.is_blocked("EURUSD")

    def test_block_after_3_consecutive_losses(self):
        self.pm.on_trade_closed("GOLD", -20)
        self.pm.on_trade_closed("GOLD", -15)
        assert not self.pm.is_blocked("GOLD")   # 2 losses only
        self.pm.on_trade_closed("GOLD", -10)
        assert self.pm.is_blocked("GOLD")        # 3 → blocked

    def test_win_resets_consecutive_counter(self):
        self.pm.on_trade_closed("GOLD", -20)
        self.pm.on_trade_closed("GOLD", -15)
        self.pm.on_trade_closed("GOLD", +50)  # win resets streak
        # After win: streak = 0
        # Then 2 more losses → not blocked (would need 3 consecutive)
        self.pm.on_trade_closed("GOLD", -10)
        self.pm.on_trade_closed("GOLD", -10)
        assert not self.pm.is_blocked("GOLD")

    def test_reset_unblocks(self):
        for _ in range(3):
            self.pm.on_trade_closed("GOLD", -20)
        assert self.pm.is_blocked("GOLD")
        self.pm.reset("GOLD")
        assert not self.pm.is_blocked("GOLD")

    def test_instruments_independent(self):
        for _ in range(3):
            self.pm.on_trade_closed("EURUSD", -10)
        assert self.pm.is_blocked("EURUSD")
        assert not self.pm.is_blocked("GOLD")

    def test_format_status(self):
        for _ in range(3):
            self.pm.on_trade_closed("GOLD", -10)
        status = self.pm.format_status()
        assert isinstance(status, str)
