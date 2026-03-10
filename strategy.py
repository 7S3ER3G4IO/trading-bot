"""
strategy.py — London/NY Open Breakout Strategy

Logique corrigée :
  - Le range pré-session est calculé sur les bougies AVANT l'ouverture :
      London : bougies 06h00-07h45 UTC
      NY     : bougies 12h00-13h15 UTC
  - Pendant la session (08h00-10h00 / 13h30-16h00), on surveille si le prix
    casse le haut ou le bas de ce range
  - Score de confirmation : ADX + Volume + Momentum bougie
  - SL = autre extrémité du range | TP = entrée ± R:R × range

Sessions actives :
  London open  : 08h00 → 10h00 UTC
  NY open      : 13h30 → 16h00 UTC
"""

import os
import pandas as pd
import ta
from loguru import logger
from datetime import datetime, timezone

# ─── Constantes ───────────────────────────────────────────────────────────────
SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"

# Sessions de trading (UTC, en minutes depuis minuit)
SESSIONS_UTC = [
    (8 * 60,      10 * 60),   # London open  : 08h00 → 10h00
    (13 * 60 + 30, 16 * 60),  # NY open       : 13h30 → 16h00
]

# Plages pré-session pour calculer le range (UTC, en minutes)
PRE_SESSIONS_UTC = [
    (6 * 60,       7 * 60 + 45),  # Pré-London : 06h00 → 07h45
    (12 * 60,      13 * 60 + 15), # Pré-NY     : 12h00 → 13h15
]

MIN_RANGE_PCT    = 0.08   # Range min = 0.08% du prix
MIN_SCORE        = 2      # Score minimum sur 3
ADX_MIN          = 18
ATR_PERIOD       = 14
REQUIRED_SCORE   = MIN_SCORE  # Alias pour compatibilité


def _bar_session_idx(h: int, m: int) -> int:
    """Retourne l'index de session active (0=London, 1=NY) ou -1 si hors session."""
    t = h * 60 + m
    for i, (start, end) in enumerate(SESSIONS_UTC):
        if start <= t <= end:
            return i
    return -1


def _bar_in_presession(h: int, m: int) -> int:
    """Retourne l'index de pré-session (0=pré-London, 1=pré-NY) ou -1."""
    t = h * 60 + m
    for i, (start, end) in enumerate(PRE_SESSIONS_UTC):
        if start <= t <= end:
            return i
    return -1


class Strategy:

    def compute_indicators(self, df: pd.DataFrame, df_htf: pd.DataFrame = None) -> pd.DataFrame:
        """Calcule les indicateurs : ATR, ADX, Volume MA, EMA200."""
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]

        df["atr"] = ta.volatility.AverageTrueRange(
            high, low, close, window=ATR_PERIOD
        ).average_true_range()

        adx_ind  = ta.trend.ADXIndicator(high, low, close, window=14)
        df["adx"] = adx_ind.adx()

        df["vol_ma"]       = volume.rolling(20).mean()
        df["ema200"]       = ta.trend.EMAIndicator(close, window=200).ema_indicator()
        df["ema200_slope"] = df["ema200"].pct_change(periods=5, fill_method=None)
        df["ema9"]         = ta.trend.EMAIndicator(close, window=9).ema_indicator()
        df["ema21"]        = ta.trend.EMAIndicator(close, window=21).ema_indicator()
        df["rsi"]          = ta.momentum.RSIIndicator(close, window=14).rsi()

        return df.dropna()

    def is_session_ok(self) -> bool:
        """Retourne True uniquement pendant les fenêtres de breakout."""
        now = datetime.now(timezone.utc)
        return _bar_session_idx(now.hour, now.minute) >= 0

    def compute_session_range(self, df: pd.DataFrame) -> dict:
        """
        Calcule le range pré-session à partir des bougies DE PRÉ-SESSION.
        Si le DataFrame contient des timestamps, filtre sur les bonnes heures.
        Sinon (pas de timezone info), prend les RANGE_BARS dernières bougies.
        """
        last_close = float(df.iloc[-1]["close"])

        # Essai avec filtrage timestamp (backtesting)
        if hasattr(df.index, "hour"):
            presession_bars = df[
                df.index.map(lambda ts: _bar_in_presession(ts.hour, ts.minute) >= 0)
            ]
            if len(presession_bars) >= 3:
                high_range = float(presession_bars["high"].max())
                low_range  = float(presession_bars["low"].min())
                range_size = high_range - low_range
                range_pct  = (range_size / last_close * 100) if last_close > 0 else 0
                return {"high": high_range, "low": low_range, "size": range_size, "pct": range_pct}

        # Fallback : 6 dernières bougies
        recent     = df.tail(6)
        high_range = float(recent["high"].max())
        low_range  = float(recent["low"].min())
        range_size = high_range - low_range
        range_pct  = (range_size / last_close * 100) if last_close > 0 else 0
        return {"high": high_range, "low": low_range, "size": range_size, "pct": range_pct}

    def market_regime(self, df: pd.DataFrame, slope_threshold: float = 0.0001) -> str:
        if "ema200_slope" not in df.columns or len(df) < 2:
            return "RANGE"
        slope = float(df.iloc[-1]["ema200_slope"])
        if slope > slope_threshold: return "BULL"
        if slope < -slope_threshold: return "BEAR"
        return "RANGE"

    def get_signal(
        self,
        df: pd.DataFrame,
        symbol: str = None,
        min_score_override: int = None,
        futures_mode: bool = False,
    ) -> tuple:
        """
        Signal breakout London/NY.
        - futures_mode=True  → skip filtre session (permet le backtest 24h)
        - futures_mode=False → vérifie is_session_ok() en live
        """
        req_score = min_score_override if min_score_override is not None else MIN_SCORE

        if len(df) < 30:
            return SIGNAL_HOLD, 0, []

        # Filtre session (skip en futures_mode pour backtests)
        if not futures_mode and not self.is_session_ok():
            return SIGNAL_HOLD, 0, []

        curr = df.iloc[-1]

        # ── Range pré-session ────────────────────────────────────────────────
        sr = self.compute_session_range(df)
        range_size = sr["size"]
        range_pct  = sr["pct"]

        if range_pct < MIN_RANGE_PCT:
            logger.debug(f"😴 Range trop petit ({range_pct:.3f}%) — marché calme")
            return SIGNAL_HOLD, 0, []

        last_close = float(curr["close"])
        high_r     = sr["high"]
        low_r      = sr["low"]

        # ── Détection du breakout ────────────────────────────────────────────
        # Le prix doit clôturer AU-DELÀ du range (pas seulement le toucher)
        BREAKOUT_MARGIN = range_size * 0.1  # Doit casser de 10% du range
        broke_up   = last_close > high_r + BREAKOUT_MARGIN
        broke_down = last_close < low_r  - BREAKOUT_MARGIN

        if not broke_up and not broke_down:
            return SIGNAL_HOLD, 0, []

        sig = SIGNAL_BUY if broke_up else SIGNAL_SELL

        # ── Confirmations (score 0-3) ────────────────────────────────────────
        confirmations = []

        # C1 — ADX (force de tendance en formation)
        adx_val = float(curr.get("adx", 0))
        if adx_val > ADX_MIN:
            confirmations.append(f"ADX {adx_val:.0f}")

        # C2 — Volume > MA20
        vol    = float(curr.get("volume", 0))
        vol_ma = float(curr.get("vol_ma", vol + 1))
        if vol > vol_ma:
            confirmations.append("Volume✓")

        # C3 — Momentum : corps de bougie > 30% du range
        candle_body = abs(float(curr["close"]) - float(curr["open"]))
        if range_size > 0 and (candle_body / range_size) >= 0.3:
            confirmations.append(f"Momentum {candle_body/range_size:.0%}")

        score = len(confirmations)

        if score < req_score:
            return SIGNAL_HOLD, score, confirmations

        rng_info = f"Range {range_pct:.2f}% | {high_r:.5f}–{low_r:.5f}"
        if sig == SIGNAL_BUY:
            logger.info(f"🟢 BREAKOUT BUY  | {rng_info} | Score {score}/3 | {confirmations}")
        else:
            logger.info(f"🔴 BREAKOUT SELL | {rng_info} | Score {score}/3 | {confirmations}")

        return sig, score, [rng_info] + confirmations

    def get_sl_tp(self, sig: str, entry: float, range_info: dict, rr: float = 1.8) -> dict:
        """SL = autre extrémité du range. TP = entrée ± rr × taille."""
        if sig == SIGNAL_BUY:
            sl = range_info["low"] - range_info["size"] * 0.1   # Buffer 10%
            tp = entry + range_info["size"] * rr
        else:
            sl = range_info["high"] + range_info["size"] * 0.1
            tp = entry - range_info["size"] * rr
        return {"sl": sl, "tp": tp, "range": range_info}

    def get_atr(self, df: pd.DataFrame) -> float:
        if "atr" in df.columns and len(df) > 0:
            return float(df.iloc[-1]["atr"])
        return 0.0
