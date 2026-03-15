"""tests/test_slippage_tracker.py — Tests unitaires SlippageTracker"""
import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("TESTING", "1")

from slippage_tracker import SlippageTracker, _get_pip_size


class TestPipSize:
    def test_jpy_pair(self):
        assert _get_pip_size("USDJPY") == 0.01
        assert _get_pip_size("GBPJPY") == 0.01

    def test_gold(self):
        assert _get_pip_size("GOLD") == 0.1
        assert _get_pip_size("XAUUSD") == 0.1

    def test_crypto(self):
        assert _get_pip_size("BTCUSD") == 1.0
        assert _get_pip_size("ETHUSD") == 1.0

    def test_indices(self):
        assert _get_pip_size("DE40") == 1.0
        assert _get_pip_size("US500") == 1.0
        assert _get_pip_size("UK100") == 1.0

    def test_forex_default(self):
        assert _get_pip_size("EURUSD") == 0.0001
        assert _get_pip_size("GBPUSD") == 0.0001


class TestSlippageRecord:
    def test_buy_positive_slippage(self):
        """BUY: actual > expected → slippage positif (défavorable)"""
        t = SlippageTracker()
        slip = t.record("EURUSD", expected_price=1.08500, actual_price=1.08503, direction="BUY")
        assert slip == pytest.approx(0.3, abs=0.05)

    def test_sell_positive_slippage(self):
        """SELL: actual < expected → slippage positif (défavorable)"""
        t = SlippageTracker()
        slip = t.record("EURUSD", expected_price=1.08500, actual_price=1.08497, direction="SELL")
        assert slip == pytest.approx(0.3, abs=0.05)

    def test_zero_slippage(self):
        t = SlippageTracker()
        slip = t.record("EURUSD", expected_price=1.08500, actual_price=1.08500, direction="BUY")
        assert slip == 0.0

    def test_history_appended(self):
        t = SlippageTracker()
        t.record("EURUSD", 1.085, 1.08503, "BUY")
        t.record("GBPUSD", 1.265, 1.265, "SELL")
        assert len(t._history) == 2


class TestAvgSlippage:
    def test_avg_all(self):
        t = SlippageTracker()
        t.record("EURUSD", 1.085, 1.0855, "BUY")   # +5 pips
        t.record("EURUSD", 1.090, 1.0905, "BUY")   # +5 pips
        avg = t.avg_slippage()
        assert avg == pytest.approx(5.0, abs=0.1)

    def test_avg_by_instrument(self):
        t = SlippageTracker()
        t.record("EURUSD", 1.085, 1.0855, "BUY")   # +5 pips
        t.record("GBPUSD", 1.265, 1.265, "SELL")   # 0 pips
        avg_eu = t.avg_slippage("EURUSD")
        avg_gbp = t.avg_slippage("GBPUSD")
        assert avg_eu == pytest.approx(5.0, abs=0.1)
        assert avg_gbp == 0.0

    def test_avg_empty(self):
        t = SlippageTracker()
        assert t.avg_slippage() == 0.0


class TestDiscordAlert:
    def test_no_alert_below_threshold(self):
        t = SlippageTracker()
        for _ in range(5):
            t.record("EURUSD", 1.085, 1.08501, "BUY")  # ~1 pip
        with patch("requests.post") as mock_post:
            sent = t.check_discord_alert(window=5, threshold_pips=3.0)
        assert sent is False
        mock_post.assert_not_called()

    def test_no_alert_insufficient_history(self):
        t = SlippageTracker()
        t.record("EURUSD", 1.085, 1.0860, "BUY")  # 10 pips (1 seul trade)
        with patch("requests.post") as mock_post:
            sent = t.check_discord_alert(window=5, threshold_pips=3.0)
        assert sent is False

    def test_alert_sent_with_webhook(self):
        t = SlippageTracker()
        for _ in range(5):
            t.record("EURUSD", 1.085, 1.0860, "BUY")  # 10 pips chacun

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("requests.post", return_value=mock_resp) as mock_post:
            with patch.dict(os.environ, {"DISCORD_MONITORING_WEBHOOK": "https://discord.com/api/webhooks/test"}):
                sent = t.check_discord_alert(window=5, threshold_pips=3.0)

        assert sent is True
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "content" in call_kwargs.kwargs.get("json", {})
        assert "SLIPPAGE ALERT" in call_kwargs.kwargs["json"]["content"]

    def test_no_webhook_configured(self):
        t = SlippageTracker()
        for _ in range(5):
            t.record("EURUSD", 1.085, 1.0860, "BUY")  # 10 pips

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("DISCORD_MONITORING_WEBHOOK", None)
            sent = t.check_discord_alert(window=5, threshold_pips=3.0)
        assert sent is False


class TestSummary:
    def test_summary_no_history(self):
        t = SlippageTracker()
        s = t.summary()
        assert "Aucun" in s

    def test_summary_with_history(self):
        t = SlippageTracker()
        t.record("GOLD", 2300.0, 2300.5, "BUY")   # +5 pips (0.5/0.1)
        t.record("EURUSD", 1.085, 1.085, "SELL")   # 0 pips
        s = t.summary()
        assert "Tracker" in s
        assert "pips" in s
