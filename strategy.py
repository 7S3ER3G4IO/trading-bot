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

MIN_RANGE_PCT    = 0.05   # Range min = 0.05% du prix (assoupli de 0.08%)
MAX_RANGE_PCT    = 6.0    # Range max = 6.0% du prix
                          # (GOLD ~2-5%, forex ~0.5-1.5%, indices ~1-3%)
MIN_SCORE        = 1      # Score minimum sur 7 (assoupli : 1 confirmation suffit)
ADX_MIN          = 15     # ADX assoupli de 18 à 15 (capte plus de tendances)
ATR_PERIOD       = 14
REQUIRED_SCORE   = MIN_SCORE  # Alias pour compatibilité

# Jours de trading autorisés (Lundi=0 ... Dimanche=6)
# Tous les jours ouvrables : les filtres de session et de range filtrent déjà assez
ALLOWED_WEEKDAYS = {0, 1, 2, 3, 4}  # Lundi → Vendredi


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
        """Retourne True uniquement pendant les fenêtres de breakout ET les bons jours."""
        now = datetime.now(timezone.utc)
        # Filtre Lundi/Vendredi : ces jours ont trop de bruit et de faux signaux
        if now.weekday() not in ALLOWED_WEEKDAYS:
            logger.debug(
                f"🗓️  Jour filtré : {now.strftime('%A')} (Lundi & Vendredi exclus)"
            )
            return False
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

    def compute_session_vwap(self, df: pd.DataFrame) -> float:
        """
        Calcule le VWAP depuis l'ouverture de la session courante.
        Filtre les bougies pré-session et session uniquement.
        Retourne 0.0 si insufficient data.
        """
        try:
            if hasattr(df.index, "hour"):
                # Garde uniquement les bougies depuis le début de la session active
                session_bars = df[
                    df.index.map(lambda ts: _bar_in_presession(ts.hour, ts.minute) >= 0
                                 or _bar_session_idx(ts.hour, ts.minute) >= 0)
                ]
                if len(session_bars) >= 3:
                    typical = (session_bars["high"] + session_bars["low"] + session_bars["close"]) / 3
                    vwap = (typical * session_bars["volume"]).sum() / session_bars["volume"].sum()
                    return float(vwap)
            # Fallback : VWAP sur les 20 dernières bougies
            recent = df.tail(20)
            typical = (recent["high"] + recent["low"] + recent["close"]) / 3
            vol = recent["volume"]
            return float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else 0.0
        except Exception:
            return 0.0

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
            logger.info(f"😴 Range trop petit ({range_pct:.3f}%) < {MIN_RANGE_PCT}% — marché calme, skip")
            return SIGNAL_HOLD, 0, []

        if range_pct > MAX_RANGE_PCT:
            logger.info(f"⚠️  Range trop grand ({range_pct:.3f}%) — spread excessif / news, skip")
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

        # C3 — Momentum Candle : corps > 60% de l'ATR (bougie conviction forte)
        # Standard institutionnel : la bougie de breakout doit être grande
        atr_val     = float(curr.get("atr", range_size or 1))
        candle_body = abs(float(curr["close"]) - float(curr["open"]))
        momentum_ok = (atr_val > 0 and candle_body >= 0.6 * atr_val) or \
                      (range_size > 0 and (candle_body / range_size) >= 0.3)
        if momentum_ok:
            confirmations.append(f"Momentum {candle_body:.5f} (ATR {atr_val:.5f})")

        # ── UPGRADE : Filtre VWAP (direction par rapport à la valeur équitable) ──
        # BUY  : prix doit être AU-DESSUS du VWAP de session
        # SELL : prix doit être EN-DESSOUS du VWAP de session
        # Un trade contre le VWAP a statistiquement 40% moins de chance de réussir.
        vwap = self.compute_session_vwap(df)
        if vwap > 0:
            vwap_ok = (sig == SIGNAL_BUY  and last_close > vwap) or \
                      (sig == SIGNAL_SELL and last_close < vwap)
            if vwap_ok:
                confirmations.append(f"VWAP✓ ({vwap:.5f})")
            else:
                logger.debug(
                    f"⚠️  VWAP contra-tendance {sig} | prix={last_close:.5f} vs VWAP={vwap:.5f}"
                )
                # Ne bloque pas mais ne compte pas comme confirmation

        # ── UPGRADE : Filtre Wick (bougie à longue mèche = fakeout probable) ──
        # La mèche dans la direction du breakout ne doit pas dépasser 40%
        # du range total de la bougie. Une longue mèche = rejet de prix.
        candle_range = float(curr["high"]) - float(curr["low"])
        if candle_range > 0:
            if sig == SIGNAL_BUY:
                upper_wick = float(curr["high"]) - float(curr["close"])
                wick_pct   = upper_wick / candle_range
            else:
                lower_wick = float(curr["close"]) - float(curr["low"])
                wick_pct   = lower_wick / candle_range

            if wick_pct > 0.40:
                logger.info(
                    f"🕯️  Wick filter {sig} : mèche {wick_pct:.0%} > 40% — fakeout probable, skip"
                )
                return SIGNAL_HOLD, 0, []
            elif wick_pct < 0.20:
                confirmations.append(f"Wick✓ ({wick_pct:.0%})")

        # \u2500\u2500 UPGRADE : Orderflow Imbalance (OFI) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Mesure la pression acheteur/vendeur sur les 3 dernières bougies.
        # BUY favori si les 3 bougies sont bullish (close > open)
        # SELL favori si les 3 bougies sont bearish (close < open)
        try:
            last3 = df.tail(3)
            if len(last3) >= 3:
                bull_bars = (last3["close"] > last3["open"]).sum()
                bear_bars = (last3["close"] < last3["open"]).sum()
                ofi_ok = (sig == SIGNAL_BUY  and bull_bars >= 2) or \
                         (sig == SIGNAL_SELL and bear_bars >= 2)
                if ofi_ok:
                    ofi_label = f"{'↑'*bull_bars if sig == SIGNAL_BUY else '↓'*bear_bars}"
                    confirmations.append(f"OFI✓ {ofi_label}")
        except Exception:
            pass

        # \u2500\u2500 UPGRADE : London Squeeze Filter \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Range asiatique (0h-7h UTC) très compressée = London Squeeze probable
        # Les vrais breakouts sont plus puissants après un squeeze asiatique.
        # Confirmation +1 si range_asiatique < 35% de la moyenne 20j.
        try:
            if hasattr(df.index, "hour"):
                asian_bars = df[df.index.map(lambda ts: 0 <= ts.hour < 7)]
                if len(asian_bars) >= 4:
                    asian_range = float(asian_bars["high"].max() - asian_bars["low"].min())
                    avg_range_20 = float((df["high"] - df["low"]).tail(20 * 12).mean())  # 12 bougies/h
                    if avg_range_20 > 0 and asian_range < avg_range_20 * 0.35:
                        confirmations.append(f"Squeeze✓ ({asian_range:.5f})")
                        logger.debug(f"🔵 London Squeeze détecté — range asiat. {asian_range:.5f} < 35% avg")
        except Exception:
            pass

        score = len(confirmations)


        if score < req_score:
            logger.info(f"❌ Score {score}/{req_score} insuffisant sur {symbol} — confirmations: {confirmations}")
            return SIGNAL_HOLD, score, confirmations

        rng_info = f"Range {range_pct:.2f}% | {high_r:.5f}–{low_r:.5f}"
        if sig == SIGNAL_BUY:
            logger.info(f"🟢 BREAKOUT BUY  | {rng_info} | Score {score}/3 | {confirmations}")
        else:
            logger.info(f"🔴 BREAKOUT SELL | {rng_info} | Score {score}/3 | {confirmations}")

        return sig, score, [rng_info] + confirmations

    def get_sl_tp(self, sig: str, entry: float, range_info: dict, rr: float = 2.0) -> dict:
        """SL = autre extrémité du range. TP = entrée ± rr × taille.
        R:R par défaut porté à 2.0 (contre 1.8 avant) pour capturer plus de gains.
        """
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
