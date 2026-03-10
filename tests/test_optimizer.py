"""
tests/test_optimizer.py — Tests unitaires de l'optimizer (sans réseau).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import pandas as pd
import pytest

from optimizer import (
    precompute, vectorized_backtest, _default_params,
    hyperopt_symbol, FEE_RATE, INITIAL_BALANCE, PARAMS_FILE,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _make_df(n: int = 500, trend: str = "bull") -> pd.DataFrame:
    """Génère un DataFrame OHLCV synthétique avec tendance."""
    rng = np.random.default_rng(42)
    idx = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    base = 1800.0
    prices = [base]
    for _ in range(n - 1):
        drift = 0.0002 if trend == "bull" else -0.0002
        prices.append(prices[-1] * (1 + drift + rng.normal(0, 0.001)))
    close = np.array(prices)
    high  = close * 1.002
    low   = close * 0.998
    df = pd.DataFrame({
        "open":   close * 0.9995,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": rng.uniform(100, 1000, n),
    }, index=idx)
    return df


# ─── Tests precompute ────────────────────────────────────────────────────────

class TestPrecompute:
    def test_returns_dataframe(self):
        df = precompute(_make_df())
        assert isinstance(df, pd.DataFrame)

    def test_has_required_columns(self):
        df = precompute(_make_df())
        for col in ["ema_fast", "ema_slow", "rsi", "atr", "adx", "vol_ma"]:
            assert col in df.columns, f"Colonne manquante : {col}"

    def test_no_nan_after_dropna(self):
        df = precompute(_make_df(700))
        assert not df.isnull().any().any()

    def test_length_reasonable(self):
        raw = _make_df(500)
        df  = precompute(raw)
        # dropna() retire les premières lignes — on doit en garder au moins 200
        assert len(df) >= 200


# ─── Tests vectorized_backtest ───────────────────────────────────────────────

class TestVectorizedBacktest:
    def test_returns_tuple(self):
        df     = precompute(_make_df())
        params = _default_params()
        result = vectorized_backtest(df, params)
        assert isinstance(result, tuple)
        assert len(result) == 6  # (n_trades, wr, pnl_net, max_dd, sharpe, sortino)

    def test_wr_between_0_and_100(self):
        df     = precompute(_make_df())
        params = _default_params()
        n_trades, wr, pnl_net, max_dd, sharpe, sortino = vectorized_backtest(df, params)
        if n_trades > 0:
            assert 0.0 <= wr <= 100.0

    def test_max_dd_non_negative(self):
        df     = precompute(_make_df())
        params = _default_params()
        n_trades, wr, pnl_net, max_dd, sharpe, sortino = vectorized_backtest(df, params)
        assert max_dd >= 0.0

    def test_fee_rate_is_zero(self):
        """Capital.com CFD — pas de commission séparée."""
        assert FEE_RATE == 0.0


# ─── Tests _default_params ───────────────────────────────────────────────────

class TestDefaultParams:
    def test_returns_dict(self):
        assert isinstance(_default_params(), dict)

    def test_has_required_score_key(self):
        assert "required_score" in _default_params()

    def test_has_tp_multiplier(self):
        assert "tp_multiplier" in _default_params()

    def test_required_score_positive(self):
        p = _default_params()
        assert p.get("required_score", 1) >= 1


# ─── Tests hyperopt_symbol (mock data, sans réseau) ──────────────────────────

class TestHyperoptSymbol:
    def test_returns_dict_with_mock_data(self):
        df_pre = precompute(_make_df(600))
        result = hyperopt_symbol("GOLD", days=3, tf="5m", n_trials=5, df_pre=df_pre)
        assert isinstance(result, dict)
        assert "required_score" in result

    def test_tp_multiplier_positive(self):
        df_pre = precompute(_make_df(600))
        result = hyperopt_symbol("EURUSD", days=3, tf="5m", n_trials=3, df_pre=df_pre)
        assert result.get("tp_multiplier", 1.0) > 0
