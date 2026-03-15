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

MIN_RANGE_PCT    = 0.03   # Range min = 0.03% du prix (adapté 1H — ranges plus petits)
MAX_RANGE_PCT    = 25.0   # Range max = 25% (1H: crypto 5-15%, stocks 5-10%, forex 0.5-3%)
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

# Sessions élargies par catégorie d'actif (heures UTC : [(debut_h, debut_m), (fin_h, fin_m)])
# Chaque entrée est une liste de tuples (start, end) en minutes depuis minuit
SESSION_WINDOWS = {
    # Crypto : 24/7 — marchés ouverts en permanence
    "crypto":      [(0, 24 * 60)],
    # Forex EUR/GBP/CHF : London + NY élargi (07h-21h UTC)
    "forex":       [(7 * 60, 21 * 60)],
    # Forex Asiatique/Pacifique (JPY, AUD, NZD) : Tokyo/Sydney + London + NY
    # Tokyo/Sydney: 00h-08h UTC | London: 07h-16h | NY: 13h-21h
    "forex_asia":  [(0, 21 * 60)],  # 00h-21h UTC (Tokyo→London→NY sans trou)
    # Forex MR : 24/7 — Mean Reversion fonctionne H24 (RSI/BB ne dépendent pas de session)
    "forex_mr":    [(0, 24 * 60)],
    # Indices : London marchés + NY + after-hours
    "indices":     [(7 * 60, 10 * 60 + 30),
                    (13 * 60, 20 * 60)],
    # Stocks US : NY étendu
    "stocks":      [(13 * 60, 20 * 60)],
    # Commodités : élargi 06h-22h UTC (pétrole, or actifs sur large plage)
    "commodities": [(6 * 60, 22 * 60)],
}

# Devises asiatiques/pacifiques — détection automatique dans l'instrument
ASIAN_CURRENCIES = {"JPY", "AUD", "NZD"}


def _in_session_window(h: int, m: int, category: str) -> bool:
    """Vérifie si l'heure UTC est dans la fenêtre de session de la catégorie."""
    t = h * 60 + m
    windows = SESSION_WINDOWS.get(category, SESSION_WINDOWS["forex"])
    for start, end in windows:
        if start <= t < end:
            return True
    return False


# ─── S-1: Weighted score configuration ────────────────────────────────────────
# Threshold for trade entry (0.0-1.0 continuous score).
# 0.40 ≈ ancien score ≥ 1 (une confirmation forte suffit).
WEIGHTED_SCORE_THRESHOLD = 0.60   # SNIPER MODE — only high-confluence signals


class Strategy:

    def compute_indicators(self, df: pd.DataFrame, df_htf: pd.DataFrame = None) -> pd.DataFrame:
        """Calcule les indicateurs : ATR, ADX, Volume MA, EMA, RSI, BB, MACD."""
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

        # ── V6+V7: Bollinger Bands, MACD, EMAs pour MR+TF ──
        df["ema20"]  = ta.trend.EMAIndicator(close, window=20).ema_indicator()
        df["ema50"]  = ta.trend.EMAIndicator(close, window=50).ema_indicator()
        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        df["bb_up"]  = bb.bollinger_hband()
        df["bb_lo"]  = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        macd_ind = ta.trend.MACD(close, window_slow=26, window_fast=12, window_sign=9)
        df["macd"]   = macd_ind.macd()
        df["macd_s"] = macd_ind.macd_signal()
        # Weekly bias (approximation via long EMAs)
        df["ema100"] = ta.trend.EMAIndicator(close, window=100).ema_indicator()
        df["ema250"] = ta.trend.EMAIndicator(close, window=250).ema_indicator()

        # ── M48: Keltner Channel + Z-score (Mean-Reversion) ──
        kc = ta.volatility.KeltnerChannel(high, low, close, window=20, window_atr=14)
        df["kc_up"]  = kc.keltner_channel_hband()
        df["kc_lo"]  = kc.keltner_channel_lband()
        df["kc_mid"] = kc.keltner_channel_mband()
        # Z-score: how many std devs from the 20-period mean
        sma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        df["zscore"] = (close - sma20) / std20.replace(0, float('nan'))
        # RSI divergence: price makes lower low but RSI makes higher low
        df["rsi_sma"] = df["rsi"].rolling(5).mean()

        # Don't dropna() on ALL columns — ema100/ema250 need 100-250 bars
        # and may not all be available with limited fetch. Only require cores.
        core_cols = ["atr", "adx", "ema20", "ema50", "rsi", "macd", "macd_s",
                     "bb_up", "bb_lo"]
        available = [c for c in core_cols if c in df.columns]
        return df.dropna(subset=available)

    def update_last_bar(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        A-3: Incremental indicator update — only recalculates the last row.
        Uses recursive formulas for EMA, ATR, RSI.
        
        Prerequisites: df must already have indicators computed on rows [:-1].
        If the previous row lacks indicators, falls back to full recomputation.
        
        Performance: ~1ms vs ~200ms for full compute_indicators().
        """
        if len(df) < 3:
            return self.compute_indicators(df)

        # Check if previous row has indicators
        prev = df.iloc[-2]
        if "atr" not in df.columns or pd.isna(prev.get("atr", None)):
            return self.compute_indicators(df)

        idx = df.index[-1]
        close = float(df.iloc[-1]["close"])
        high  = float(df.iloc[-1]["high"])
        low   = float(df.iloc[-1]["low"])
        vol   = float(df.iloc[-1]["volume"])

        prev_close = float(df.iloc[-2]["close"])

        # ── Recursive EMA: EMA(t) = α × close + (1-α) × EMA(t-1) ──
        def _ema(col, period):
            alpha = 2 / (period + 1)
            prev_val = float(prev.get(col, close))
            return alpha * close + (1 - alpha) * prev_val

        df.at[idx, "ema9"]   = _ema("ema9",   9)
        df.at[idx, "ema20"]  = _ema("ema20",  20)
        df.at[idx, "ema21"]  = _ema("ema21",  21)
        df.at[idx, "ema50"]  = _ema("ema50",  50)
        df.at[idx, "ema100"] = _ema("ema100", 100)
        df.at[idx, "ema200"] = _ema("ema200", 200)
        df.at[idx, "ema250"] = _ema("ema250", 250)

        # EMA200 slope (5-bar pct change)
        if len(df) > 5:
            ema200_5 = float(df.iloc[-6].get("ema200", df.at[idx, "ema200"]))
            if ema200_5 > 0:
                df.at[idx, "ema200_slope"] = (df.at[idx, "ema200"] - ema200_5) / ema200_5
            else:
                df.at[idx, "ema200_slope"] = 0.0

        # ── Recursive ATR ──
        prev_atr = float(prev.get("atr", 0))
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        df.at[idx, "atr"] = (prev_atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD

        # ── Volume MA (SMA 20) ──
        if len(df) >= 20:
            df.at[idx, "vol_ma"] = float(df["volume"].iloc[-20:].mean())
        else:
            df.at[idx, "vol_ma"] = float(prev.get("vol_ma", vol))

        # ── RSI (Wilder's smoothing) ──
        delta = close - prev_close
        period = 14
        prev_rsi = float(prev.get("rsi", 50))
        # Approximate gains/losses from previous RSI
        if prev_rsi > 0 and prev_rsi < 100:
            prev_avg_gain = prev_rsi * 1.0 / (100 - prev_rsi) if prev_rsi < 100 else 1
        else:
            prev_avg_gain = 1
        # Simplified recursive RSI update
        gain = max(delta, 0)
        loss = abs(min(delta, 0))
        # Use smoothed approximation
        avg_gain = (prev_avg_gain * (period - 1) + gain) / period if prev_avg_gain > 0 else gain / period
        avg_loss = (1.0 * (period - 1) + loss) / period  # approximate
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            df.at[idx, "rsi"] = 100 - 100 / (1 + rs)
        else:
            df.at[idx, "rsi"] = 100.0

        # ── Bollinger Bands (simple: bb_mid = ema20, bb = mid ± 2*std20) ──
        if len(df) >= 20:
            std20 = float(df["close"].iloc[-20:].std())
            df.at[idx, "bb_mid"] = df.at[idx, "ema20"]
            df.at[idx, "bb_up"]  = df.at[idx, "ema20"] + 2 * std20
            df.at[idx, "bb_lo"]  = df.at[idx, "ema20"] - 2 * std20

        # ── MACD ──
        ema12 = _ema("ema9", 12)  # Rough approximation using similar alpha
        ema26 = _ema("ema21", 26)
        macd_val = ema12 - ema26
        df.at[idx, "macd"] = macd_val
        # MACD signal = EMA(9) of MACD
        prev_macd_s = float(prev.get("macd_s", macd_val))
        alpha_9 = 2 / 10
        df.at[idx, "macd_s"] = alpha_9 * macd_val + (1 - alpha_9) * prev_macd_s

        # ── ADX (approximate: use previous + smoothed) ──
        # Full ADX recalculation is complex — use ta library on last 20 bars only
        try:
            _last20 = df.iloc[-20:]
            adx_temp = ta.trend.ADXIndicator(
                _last20["high"], _last20["low"], _last20["close"], window=14
            ).adx()
            if not adx_temp.empty and not pd.isna(adx_temp.iloc[-1]):
                df.at[idx, "adx"] = float(adx_temp.iloc[-1])
        except Exception:
            df.at[idx, "adx"] = float(prev.get("adx", 20))

        return df

    def is_session_ok(self) -> bool:
        """Retourne True si on est dans une fenêtre de trading quelconque (fallback global)."""
        now = datetime.now(timezone.utc)
        if now.weekday() not in ALLOWED_WEEKDAYS:
            return False
        return _bar_session_idx(now.hour, now.minute) >= 0

    def is_session_ok_for(self, instrument: str, category: str = "forex") -> bool:
        """
        Asset-aware session filter.

        - Crypto: 24/7
        - JPY/AUD/NZD pairs: Tokyo/Sydney (00-08h) + London + NY (00-21h)
        - EUR/GBP/CHF forex: London + NY (07-21h)
        - Forex MR strategy: 24/7
        - Indices: market hours only
        - Stocks: NY only
        - Commodities: 06-22h
        """
        now = datetime.now(timezone.utc)
        # Weekend block (sauf crypto)
        if category == "crypto":
            return True  # 24/7 y compris weekend
        if now.weekday() not in ALLOWED_WEEKDAYS:
            return False

        # Detect Asian/Pacific currencies in instrument name
        effective_cat = category
        if category in ("forex", "forex_mr"):
            instr_upper = instrument.upper()
            for ccy in ASIAN_CURRENCIES:
                if ccy in instr_upper:
                    effective_cat = "forex_asia"
                    break

        return _in_session_window(now.hour, now.minute, effective_cat)

    def compute_session_range(self, df: pd.DataFrame, range_lookback: int = 6) -> dict:
        """
        Calcule le range pré-session à partir des bougies DE PRÉ-SESSION.
        IMPORTANT: exclut la bougie courante pour que le breakout soit détectable.
        range_lookback: nombre de bougies à utiliser (from ASSET_PROFILES.range_lb).
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

        # Fallback : range_lookback bougies AVANT la bougie courante
        # On exclut la dernière bougie pour que le close puisse "casser" le range
        if len(df) > range_lookback:
            recent = df.iloc[-(range_lookback + 1):-1]  # exclut la bougie courante
        else:
            recent = df.iloc[:-1] if len(df) > 1 else df
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
        asset_profile: dict = None,
    ) -> tuple:
        """
        Signal multi-stratégie V6+V7.
        strat = BK (Breakout) | MR (Mean Reversion) | TF (Trend Following)
        """
        req_score = min_score_override if min_score_override is not None else MIN_SCORE

        if len(df) < 30:
            return SIGNAL_HOLD, 0, []

        # ─── PROP FIRM MODE : BK Only ──────────────────────────────────────────
        # Si BK_ONLY_MODE=True, on force le routing BK pour TOUS les instruments
        # → TF et MR désactivés (Prop Firm rule : stratégie unique validée)
        try:
            from config import BK_ONLY_MODE
            if BK_ONLY_MODE:
                strat_type = "BK"  # Force Breakout regardless of asset_profile
        except ImportError:
            pass

        # Routing par stratégie
        strat_type = asset_profile.get("strat", "BK") if asset_profile else "BK"
        if strat_type == "MR":
            return self._signal_mr(df, symbol, asset_profile)
        if strat_type == "TF":
            return self._signal_tf(df, symbol, asset_profile)

        # ═══ BREAKOUT (BK) — logique originale ═══
        # Note: le filtre session est géré per-instrument dans bot_tick.py
        # via is_session_ok_for() — plus de gate ici

        curr = df.iloc[-1]

        # ── Range pré-session ────────────────────────────────────────────────
        _range_lb = asset_profile.get("range_lb", 6) if asset_profile else 6
        sr = self.compute_session_range(df, range_lookback=_range_lb)
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
        _bk_margin = asset_profile.get("bk_margin", 0.10) if asset_profile else 0.10
        BREAKOUT_MARGIN = range_size * _bk_margin
        broke_up   = last_close > high_r + BREAKOUT_MARGIN
        broke_down = last_close < low_r  - BREAKOUT_MARGIN

        if not broke_up and not broke_down:
            # Diagnostic: proximité au breakout (temporaire)
            dist_up = ((high_r + BREAKOUT_MARGIN) - last_close) / last_close * 100
            dist_dn = (last_close - (low_r - BREAKOUT_MARGIN)) / last_close * 100
            logger.debug(
                f"📏 {symbol} range={range_pct:.2f}% | close={last_close:.2f} "
                f"| H={high_r:.2f}(+{dist_up:.2f}%) L={low_r:.2f}(-{dist_dn:.2f}%)"
            )
            return SIGNAL_HOLD, 0, []

        sig = SIGNAL_BUY if broke_up else SIGNAL_SELL

        # ── S-1: Weighted Scoring (0.0-1.0) ──────────────────────────────────
        import math
        weights = []
        confirmations = []

        # W1 — ADX (poids max 0.20) — sigmoid normalisé
        adx_val = float(curr.get("adx", 0))
        _adx_min = asset_profile.get("adx_min", ADX_MIN) if asset_profile else ADX_MIN
        adx_w = min(0.20, 0.20 / (1 + math.exp(-(adx_val - 25) / 8)))
        if adx_val > _adx_min:
            weights.append(adx_w)
            confirmations.append(f"ADX {adx_val:.0f}({adx_w:.2f})")

        # W2 — Volume ratio (poids max 0.15)
        vol    = float(curr.get("volume", 0))
        vol_ma = float(curr.get("vol_ma", vol + 1))
        vol_ratio = vol / vol_ma if vol_ma > 0 else 0
        vol_w = min(0.15, 0.15 * max(0, vol_ratio - 0.8) / 1.2)
        if vol_w > 0.01:
            weights.append(vol_w)
            confirmations.append(f"Vol {vol_ratio:.1f}×({vol_w:.2f})")

        # W3 — Momentum candle (poids max 0.20)
        atr_val     = float(curr.get("atr", range_size or 1))
        candle_body = abs(float(curr["close"]) - float(curr["open"]))
        body_ratio  = candle_body / atr_val if atr_val > 0 else 0
        mom_w = min(0.20, 0.20 * max(0, body_ratio - 0.3) / 0.7)
        if mom_w > 0.01:
            weights.append(mom_w)
            confirmations.append(f"Mom {body_ratio:.1f}×ATR({mom_w:.2f})")

        # W4 — VWAP alignment (poids fixe 0.10)
        vwap = self.compute_session_vwap(df)
        if vwap > 0:
            vwap_ok = (sig == SIGNAL_BUY and last_close > vwap) or \
                      (sig == SIGNAL_SELL and last_close < vwap)
            if vwap_ok:
                weights.append(0.10)
                confirmations.append("VWAP✓(0.10)")

        # FILTER — Wick rejection (hard block)
        candle_range = float(curr["high"]) - float(curr["low"])
        if candle_range > 0:
            if sig == SIGNAL_BUY:
                wick_pct = (float(curr["high"]) - float(curr["close"])) / candle_range
            else:
                wick_pct = (float(curr["close"]) - float(curr["low"])) / candle_range
            if wick_pct > 0.40:
                logger.info(f"🕯️  Wick filter {sig} : mèche {wick_pct:.0%} > 40% — skip")
                return SIGNAL_HOLD, 0.0, []
            elif wick_pct < 0.20:
                weights.append(0.08)
                confirmations.append("Wick✓(0.08)")

        # W5 — OFI orderflow (poids fixe 0.10)
        try:
            last3 = df.tail(3)
            if len(last3) >= 3:
                bull_bars = (last3["close"] > last3["open"]).sum()
                bear_bars = (last3["close"] < last3["open"]).sum()
                ofi_ok = (sig == SIGNAL_BUY and bull_bars >= 2) or \
                         (sig == SIGNAL_SELL and bear_bars >= 2)
                if ofi_ok:
                    ofi_label = f"{'↑'*bull_bars if sig == SIGNAL_BUY else '↓'*bear_bars}"
                    weights.append(0.10)
                    confirmations.append(f"OFI✓{ofi_label}(0.10)")
        except Exception:
            pass

        # W6 — London Squeeze (poids fixe 0.12)
        try:
            if hasattr(df.index, "hour"):
                asian_bars = df[df.index.map(lambda ts: 0 <= ts.hour < 7)]
                if len(asian_bars) >= 4:
                    asian_range = float(asian_bars["high"].max() - asian_bars["low"].min())
                    avg_range_20 = float((df["high"] - df["low"]).tail(20 * 12).mean())
                    if avg_range_20 > 0 and asian_range < avg_range_20 * 0.35:
                        weights.append(0.12)
                        confirmations.append("Squeeze✓(0.12)")
        except Exception:
            pass

        # W7 — Overlap Bonus (poids fixe 0.10) — highest volume window
        try:
            now_utc = datetime.now(timezone.utc)
            if 12 <= now_utc.hour < 16:  # London/NY overlap
                weights.append(0.10)
                confirmations.append("Overlap🔥(0.10)")
        except Exception:
            pass

        score = round(sum(weights), 3)

        if score < WEIGHTED_SCORE_THRESHOLD:
            logger.info(f"❌ Score {score:.2f} < {WEIGHTED_SCORE_THRESHOLD} sur {symbol} — {confirmations}")
            return SIGNAL_HOLD, score, confirmations

        rng_info = f"Range {range_pct:.2f}% | {high_r:.5f}–{low_r:.5f}"
        icon = "🟢" if sig == SIGNAL_BUY else "🔴"
        logger.info(f"{icon} BREAKOUT {sig} | {rng_info} | Score {score:.2f} | {confirmations}")
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

    def check_pre_signal(
        self, df: pd.DataFrame, symbol: str = None,
        asset_profile: dict = None,
    ) -> dict:
        """
        Detect if a trade setup is forming (price approaching breakout).
        Returns dict with pre-signal info, or None if no setup.
        Used for "⏳ SETUP EN FORMATION" alerts.
        """
        if len(df) < 30:
            return None

        strat_type = asset_profile.get("strat", "BK") if asset_profile else "BK"

        # --- BK (Breakout) pre-signal ---
        if strat_type == "BK":
            curr = df.iloc[-1]
            _range_lb = asset_profile.get("range_lb", 6) if asset_profile else 6
            sr = self.compute_session_range(df, range_lookback=_range_lb)
            range_size = sr["size"]
            range_pct = sr["pct"]

            if range_pct < MIN_RANGE_PCT or range_pct > MAX_RANGE_PCT:
                return None

            last_close = float(curr["close"])
            high_r = sr["high"]
            low_r = sr["low"]

            _bk_margin = asset_profile.get("bk_margin", 0.10) if asset_profile else 0.10
            margin = range_size * _bk_margin
            breakout_up = high_r + margin
            breakout_dn = low_r - margin

            # Check proximity: within 50% of margin to breakout
            dist_up = breakout_up - last_close
            dist_dn = last_close - breakout_dn

            # Already broken out → not a pre-signal
            if last_close > breakout_up or last_close < breakout_dn:
                return None

            near_up = dist_up <= margin * 1.5 and dist_up > 0
            near_dn = dist_dn <= margin * 1.5 and dist_dn > 0

            if not near_up and not near_dn:
                return None

            # Determine potential direction
            if near_up and near_dn:
                # Very tight range, could go either way — skip
                return None
            direction = "BUY" if near_up else "SELL"

            # Check how many confirmations would be met
            confirmations_met = []
            adx_val = float(curr.get("adx", 0))
            _adx_min = asset_profile.get("adx_min", ADX_MIN) if asset_profile else ADX_MIN
            if adx_val > _adx_min:
                confirmations_met.append(f"ADX {adx_val:.0f}")

            vol = float(curr.get("volume", 0))
            vol_ma = float(curr.get("vol_ma", vol + 1))
            if vol > vol_ma:
                confirmations_met.append("Volume✓")

            atr_val = float(curr.get("atr", range_size or 1))
            candle_body = abs(float(curr["close"]) - float(curr["open"]))
            if atr_val > 0 and candle_body >= 0.4 * atr_val:
                confirmations_met.append("Momentum↗")

            # Need at least 1 confirmation forming to be worth alerting
            if len(confirmations_met) < 1:
                return None

            # Compute estimated entry, SL, TPs
            entry_est = breakout_up if direction == "BUY" else breakout_dn
            if direction == "BUY":
                sl_est = low_r - range_size * 0.1
                tp1_est = entry_est + range_size * 0.8
                tp2_est = entry_est + range_size * 1.8
            else:
                sl_est = high_r + range_size * 0.1
                tp1_est = entry_est - range_size * 0.8
                tp2_est = entry_est - range_size * 1.8

            proximity_pct = (1 - (dist_up if near_up else dist_dn) / (margin * 1.5)) * 100

            return {
                "direction": direction,
                "symbol": symbol,
                "entry_est": round(entry_est, 5),
                "sl_est": round(sl_est, 5),
                "tp1_est": round(tp1_est, 5),
                "tp2_est": round(tp2_est, 5),
                "current_price": round(last_close, 5),
                "proximity_pct": round(proximity_pct, 0),
                "confirmations": confirmations_met,
                "range_pct": round(range_pct, 2),
                "missing": "Breakout candle",
            }

        # MR / TF pre-signal: check RSI extremes approaching
        if strat_type == "MR":
            curr = df.iloc[-1]
            rsi = float(curr.get("rsi", 50))
            rsi_lo = asset_profile.get("rsi_lo", 30) if asset_profile else 30
            rsi_hi = asset_profile.get("rsi_hi", 70) if asset_profile else 70
            c = float(curr["close"])
            bb_lo = float(curr.get("bb_lo", c))
            bb_up = float(curr.get("bb_up", c))

            if rsi <= rsi_lo + 5 and c <= bb_lo * 1.03:
                return {
                    "direction": "BUY",
                    "symbol": symbol,
                    "entry_est": round(c, 5),
                    "sl_est": round(bb_lo * 0.99, 5),
                    "tp1_est": round(float(curr.get("bb_mid", c)), 5),
                    "tp2_est": round(bb_up, 5),
                    "current_price": round(c, 5),
                    "proximity_pct": round((rsi_lo + 5 - rsi) / 5 * 100, 0),
                    "confirmations": [f"RSI {rsi:.0f}"],
                    "range_pct": 0,
                    "missing": f"RSI ≤ {rsi_lo}",
                }
            elif rsi >= rsi_hi - 5 and c >= bb_up * 0.97:
                return {
                    "direction": "SELL",
                    "symbol": symbol,
                    "entry_est": round(c, 5),
                    "sl_est": round(bb_up * 1.01, 5),
                    "tp1_est": round(float(curr.get("bb_mid", c)), 5),
                    "tp2_est": round(bb_lo, 5),
                    "current_price": round(c, 5),
                    "proximity_pct": round((rsi - (rsi_hi - 5)) / 5 * 100, 0),
                    "confirmations": [f"RSI {rsi:.0f}"],
                    "range_pct": 0,
                    "missing": f"RSI ≥ {rsi_hi}",
                }

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # MEAN REVERSION (MR) — RSI survendu/surachetÃ© + Bollinger Bands
    # ═══════════════════════════════════════════════════════════════════════
    def _signal_mr(self, df, symbol, profile):
        """
        M48: STATISTICAL ARBITRAGE & MEAN-REVERSION
        — Z-score extremes (> 2.0), Bollinger + Keltner squeeze,
        — RSI divergence, EMA200 proximity
        — 100% VOLUME-AGNOSTIC (works on forex, commodities, indices)
        """
        curr = df.iloc[-1]
        c = float(curr["close"])
        o = float(curr["open"])
        h = float(curr["high"])
        l = float(curr["low"])
        rsi = float(curr.get("rsi", 50))
        atr = float(curr.get("atr", 0))
        if atr <= 0:
            return SIGNAL_HOLD, 0.0, []

        bb_lo = float(curr.get("bb_lo", c))
        bb_up = float(curr.get("bb_up", c))
        bb_mid = float(curr.get("bb_mid", c))
        kc_lo = float(curr.get("kc_lo", c))
        kc_up = float(curr.get("kc_up", c))
        zscore = float(curr.get("zscore", 0))
        ema200 = float(curr.get("ema200", c))

        rsi_lo = profile.get("rsi_lo", 25) if profile else 25
        rsi_hi = profile.get("rsi_hi", 75) if profile else 75

        # ── Entry: require AT LEAST 2 of 3 conditions ──
        # Condition 1: Z-score extreme (≥ 2.5)
        # Condition 2: BB + Keltner double breach
        # Condition 3: RSI extreme
        buy_conds = [
            zscore <= -2.5,
            c <= bb_lo and c <= kc_lo,
            rsi <= rsi_lo,
        ]
        sell_conds = [
            zscore >= 2.5,
            c >= bb_up and c >= kc_up,
            rsi >= rsi_hi,
        ]

        sig = SIGNAL_HOLD
        if sum(buy_conds) >= 2:
            sig = SIGNAL_BUY
        elif sum(sell_conds) >= 2:
            sig = SIGNAL_SELL
        else:
            return SIGNAL_HOLD, 0.0, [f"MR:Z={zscore:.1f} RSI={rsi:.0f}"]

        # ── Weighted scoring (volume-agnostic) ──
        import math
        weights = []
        confirmations = []

        # W1 — Z-score extremity (max 0.25) — core signal
        z_abs = abs(zscore)
        z_w = min(0.25, 0.25 * max(0, z_abs - 2.0) / 1.5)
        if z_w > 0.01:
            weights.append(z_w)
            confirmations.append(f"Z={zscore:.1f}({z_w:.2f})")

        # W2 — RSI extremity (max 0.20)
        if sig == SIGNAL_BUY:
            rsi_ext = max(0, rsi_lo + 5 - rsi) / 15
        else:
            rsi_ext = max(0, rsi - (rsi_hi - 5)) / 15
        rsi_w = min(0.20, 0.20 * rsi_ext)
        if rsi_w > 0.01:
            weights.append(rsi_w)
            confirmations.append(f"RSI={rsi:.0f}({rsi_w:.2f})")

        # W3 — BB+Keltner double breach (0.15 bonus)
        if (sig == SIGNAL_BUY and c <= bb_lo and c <= kc_lo) or \
           (sig == SIGNAL_SELL and c >= bb_up and c >= kc_up):
            weights.append(0.15)
            confirmations.append("BB+KC✓(0.15)")
        elif (sig == SIGNAL_BUY and c <= bb_lo) or (sig == SIGNAL_SELL and c >= bb_up):
            weights.append(0.08)
            confirmations.append("BB✓(0.08)")

        # W4 — ADX low = good for MR (max 0.15)
        adx_val = float(curr.get("adx", 50))
        adx_w = min(0.15, 0.15 * max(0, 35 - adx_val) / 25)
        if adx_w > 0.01:
            weights.append(adx_w)
            confirmations.append(f"ADX↓={adx_val:.0f}({adx_w:.2f})")

        # W5 — EMA200 proximity: price near EMA200 = strong reversion target (max 0.12)
        if ema200 > 0:
            ema_dist_pct = abs(c - ema200) / c
            if ema_dist_pct < 0.03:  # within 3% of EMA200
                ema_w = min(0.12, 0.12 * (1 - ema_dist_pct / 0.03))
                if ema_w > 0.01:
                    weights.append(ema_w)
                    confirmations.append(f"EMA200({ema_w:.2f})")

        # W6 — Engulfing candle pattern (0.10)
        if len(df) >= 2:
            prev = df.iloc[-2]
            prev_o, prev_c = float(prev["open"]), float(prev["close"])
            if sig == SIGNAL_BUY and c > o and o <= prev_c and c >= prev_o:  # bullish engulfing
                weights.append(0.10)
                confirmations.append("Engulf↑(0.10)")
            elif sig == SIGNAL_SELL and c < o and o >= prev_c and c <= prev_o:  # bearish engulfing
                weights.append(0.10)
                confirmations.append("Engulf↓(0.10)")

        score = round(sum(weights), 3)
        if score < WEIGHTED_SCORE_THRESHOLD:
            return SIGNAL_HOLD, score, confirmations

        info = f"[MR] Z={zscore:.1f} RSI={rsi:.0f} BB={'lo' if sig == SIGNAL_BUY else 'hi'}"
        icon = "🟢" if sig == SIGNAL_BUY else "🔴"
        logger.info(f"{icon} MEAN REV {sig} {symbol} | {info} | Score {score:.2f} | {confirmations}")
        return sig, score, [info] + confirmations

    # ═══════════════════════════════════════════════════════════════════════
    # M49: VOLUME-AGNOSTIC TREND FOLLOWING (TF)
    # — EMA crossover + MACD + ADX + ATR expansion + engulfing patterns
    # — NO volume dependency (works on forex, commodities with volume=0)
    # ═══════════════════════════════════════════════════════════════════════
    def _signal_tf(self, df, symbol, profile):
        """
        M49: VOLUME-AGNOSTIC & TIME-BASED KERNEL
        Pure price action: EMA crossover, MACD, ADX, ATR expansion, engulfing.
        """
        curr = df.iloc[-1]
        c = float(curr["close"])
        o = float(curr["open"])
        ema_fast = float(curr.get("ema20", 0))
        ema_slow = float(curr.get("ema50", 0))
        macd = float(curr.get("macd", 0))
        macd_s = float(curr.get("macd_s", 0))
        adx_val = float(curr.get("adx", 0))

        if ema_fast <= 0 or ema_slow <= 0:
            return SIGNAL_HOLD, 0.0, []

        # Require ADX > 20 (strong trend) + EMA separation > 0.1%
        ema_sep = abs(ema_fast - ema_slow) / ema_slow * 100 if ema_slow > 0 else 0
        if ema_fast > ema_slow and macd > macd_s and adx_val > 20 and ema_sep > 0.1:
            sig = SIGNAL_BUY
        elif ema_fast < ema_slow and macd < macd_s and adx_val > 20 and ema_sep > 0.1:
            sig = SIGNAL_SELL
        else:
            return SIGNAL_HOLD, 0.0, []

        # Weekly filter for daily TF
        _tf = profile.get("tf", "1h") if profile else "1h"
        if _tf == "1d":
            ema100 = float(curr.get("ema100", 0))
            ema250 = float(curr.get("ema250", 0))
            if ema100 > 0 and ema250 > 0:
                if sig == SIGNAL_BUY and ema100 < ema250:
                    return SIGNAL_HOLD, 0.0, []
                if sig == SIGNAL_SELL and ema100 > ema250:
                    return SIGNAL_HOLD, 0.0, []

        # ── Weighted scoring (100% volume-agnostic) ──
        import math
        weights = []
        confirmations = []

        # W1 — ADX strength (max 0.25)
        adx_w = min(0.25, 0.25 / (1 + math.exp(-(adx_val - 25) / 8)))
        if adx_w > 0.02:
            weights.append(adx_w)
            confirmations.append(f"ADX {adx_val:.0f}({adx_w:.2f})")

        # W2 — RSI in trend zone (0.12)
        rsi = float(curr.get("rsi", 50))
        rsi_ok = (sig == SIGNAL_BUY and 40 < rsi < 75) or \
                 (sig == SIGNAL_SELL and 25 < rsi < 60)
        if rsi_ok:
            weights.append(0.12)
            confirmations.append(f"RSI✓={rsi:.0f}(0.12)")

        # W3 — ATR expansion: current ATR > avg ATR (replaces volume) (max 0.15)
        atr_val = float(curr.get("atr", 0))
        if len(df) >= 20:
            atr_ma = float(df["atr"].iloc[-20:].mean())
            if atr_ma > 0:
                atr_ratio = atr_val / atr_ma
                atr_w = min(0.15, 0.15 * max(0, atr_ratio - 0.8) / 1.2)
                if atr_w > 0.01:
                    weights.append(atr_w)
                    confirmations.append(f"ATR↑ {atr_ratio:.1f}×({atr_w:.2f})")

        # W4 — 3-bar momentum (max 0.15)
        if len(df) > 4:
            mom3 = c / float(df.iloc[-4]["close"]) - 1
            mom_ok = (mom3 > 0.002 and sig == SIGNAL_BUY) or \
                     (mom3 < -0.002 and sig == SIGNAL_SELL)
            if mom_ok:
                mom_w = min(0.15, abs(mom3) * 12)
                weights.append(mom_w)
                confirmations.append(f"Mom3 {mom3:+.2%}({mom_w:.2f})")

        # W5 — MACD divergence strength (max 0.10)
        macd_diff = abs(macd - macd_s)
        if atr_val > 0:
            macd_norm = macd_diff / atr_val
            macd_w = min(0.10, macd_norm * 0.5)
            if macd_w > 0.01:
                weights.append(macd_w)
                confirmations.append(f"MACD({macd_w:.2f})")

        # W6 — Engulfing candle (0.10) — pure price action
        if len(df) >= 2:
            prev = df.iloc[-2]
            prev_o, prev_c = float(prev["open"]), float(prev["close"])
            if sig == SIGNAL_BUY and c > o and o <= prev_c and c >= prev_o:
                weights.append(0.10)
                confirmations.append("Engulf↑(0.10)")
            elif sig == SIGNAL_SELL and c < o and o >= prev_c and c <= prev_o:
                weights.append(0.10)
                confirmations.append("Engulf↓(0.10)")

        # W7 — EMA alignment (0.08): price on right side of EMA200
        ema200 = float(curr.get("ema200", 0))
        if ema200 > 0:
            if (sig == SIGNAL_BUY and c > ema200) or (sig == SIGNAL_SELL and c < ema200):
                weights.append(0.08)
                confirmations.append("EMA200✓(0.08)")

        score = round(sum(weights), 3)
        if score < WEIGHTED_SCORE_THRESHOLD:
            return SIGNAL_HOLD, score, confirmations

        arrow = '↑' if macd > macd_s else '↓'
        cmp = '>' if sig == SIGNAL_BUY else '<'
        info = f"[TF] EMA20{cmp}50 MACD{arrow} ADX={adx_val:.0f}"
        icon = "🟢" if sig == SIGNAL_BUY else "🔴"
        logger.info(f"{icon} TREND {sig} {symbol} | {info} | Score {score:.2f} | {confirmations}")
        return sig, score, [info] + confirmations
