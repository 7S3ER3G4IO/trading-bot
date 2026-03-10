"""
strategy_smc.py — Stratégie Smart Money Concepts (SMC)

Réplique l'approche de Station X :
  ✅ Break of Structure (BOS / Change of Character CHoCH)
  ✅ Order Blocks (OB) — zones d'ordres institutionnels
  ✅ Fair Value Gaps (FVG) — déséquilibres de liquidité
  ✅ Liquidity Sweeps (SSL/BSL) — chasse aux stops
  ✅ Scalping 5m avec SL tight et BE rapide
  ✅ Marchés : GOLD, EURUSD, USDJPY (Capital.com CFD)
"""

import pandas as pd
import numpy as np
import ta
from loguru import logger
from datetime import datetime, timezone

# ─── Paramètres SMC ───────────────────────────────────────────────────────────
SWING_LOOKBACK   = 15    # Plus de bougies = swings plus significatifs
FVG_MIN_SIZE     = 0.0008  # 0.08% min — filtre les micro-FVGs de bruit
OB_MIN_MOVE      = 2.5   # Impulsion forte requise après OB (2.5× la taille)
LIQUIDITY_PIPS   = 0.001  # 0.1% au-delà d'un swing = sweep validé
SMC_REQUIRED     = 3     # 3/4 confirmations requises (signaux haute qualité uniquement)
ATR_PERIOD       = 14
ATR_SL_MULT      = 0.3   # SL = 0.3 ATR (ultra tight pour scalping 5m)
TP1_RATIO        = 2.0   # TP1 = 2.0 × SL — R:R 1:2 au minimum
TP2_RATIO        = 4.0   # TP2 = 4.0 × SL — R:R 1:4
AVOID_HOURS_UTC  = list(range(22, 24)) + list(range(0, 2))  # Évite nuit profonde

SIGNAL_BUY  = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_HOLD = "HOLD"


class StrategySMC:
    """
    Stratégie SMC — Smart Money Concepts.
    Détecte les zones d'ordres institutionnels et surfe sur les mouvements.
    """

    # ─── Indicateurs de base ──────────────────────────────────────────────────

    def compute_indicators(self, df: pd.DataFrame, df_htf: pd.DataFrame = None) -> pd.DataFrame:
        """Calcule ATR + EMAs de base pour contexte."""
        c, h, l = df["close"], df["high"], df["low"]

        # ATR pour SL dynamique
        df["atr"] = ta.volatility.AverageTrueRange(h, l, c, ATR_PERIOD).average_true_range()

        # EMA pour contexte de tendance HTF
        df["ema21"] = ta.trend.ema_indicator(c, 21)
        df["ema50"] = ta.trend.ema_indicator(c, 50)

        # RSI pour détect​er extrêmes (filtre optionnel)
        df["rsi"] = ta.momentum.rsi(c, 14)

        # Contexte HTF (1h ou 4h) — trend principal
        if df_htf is not None and len(df_htf) >= 21:
            htf_ema = ta.trend.ema_indicator(df_htf["close"], 21)
            df["htf_bias"] = "BULL" if float(htf_ema.iloc[-1]) < float(df_htf["close"].iloc[-1]) else "BEAR"
        else:
            df["htf_bias"] = "NEUTRAL"

        return df

    def get_atr(self, df: pd.DataFrame) -> float:
        return float(df["atr"].iloc[-1]) if "atr" in df.columns else 0.0

    # ─── Swing Highs / Lows ───────────────────────────────────────────────────

    def find_swings(self, df: pd.DataFrame, n: int = SWING_LOOKBACK) -> tuple:
        """
        Détecte les swing highs et swing lows récents.
        Retourne (swing_highs[], swing_lows[]) — listes d'indices.
        """
        highs, lows = [], []
        for i in range(n, len(df) - 1):
            # Swing High : plus haut que les n bougies avant et après
            if df["high"].iloc[i] == df["high"].iloc[i-n:i+1].max():
                highs.append(i)
            # Swing Low : plus bas que les n bougies avant et après
            if df["low"].iloc[i] == df["low"].iloc[i-n:i+1].min():
                lows.append(i)
        return highs, lows

    # ─── Break of Structure (BOS) ─────────────────────────────────────────────

    def detect_bos(self, df: pd.DataFrame) -> str:
        """
        Détecte le Break of Structure sur les dernières bougies.
        BOS BULL : prix casse un swing high récent → tendance haussière
        BOS BEAR : prix casse un swing low récent → tendance baissière
        """
        if len(df) < SWING_LOOKBACK * 2 + 5:
            return "NEUTRAL"

        highs, lows = self.find_swings(df, SWING_LOOKBACK)
        if not highs or not lows:
            return "NEUTRAL"

        current_close = float(df["close"].iloc[-1])

        # Dernier swing high et low significatifs (3 derniers)
        last_swing_high = float(df["high"].iloc[max(highs[-3:])])
        last_swing_low  = float(df["low"].iloc[min(lows[-3:])])

        # Exiger une marge de 0.05% au-delà du swing pour éviter les faux BOS
        margin = current_close * 0.0005
        if current_close > last_swing_high + margin:
            return "BULL"   # BOS haussier
        elif current_close < last_swing_low - margin:
            return "BEAR"   # BOS baissier
        return "NEUTRAL"

    # ─── Order Blocks (OB) ────────────────────────────────────────────────────

    def find_order_blocks(self, df: pd.DataFrame, bos: str) -> list:
        """
        Trouve les Order Blocks valides dans le contexte BOS actuel.

        Bullish OB (si BOS=BULL) :
          Dernière bougie ROUGE avant un mouvement impulsif haussier.
          Prix dans la zone de l'OB = zone d'entrée BUY.

        Bearish OB (si BOS=BEAR) :
          Dernière bougie VERTE avant un mouvement impulsif baissier.
          Prix dans la zone de l'OB = zone d'entrée SELL.
        """
        obs = []
        if len(df) < 10:
            return obs

        current_price = float(df["close"].iloc[-1])
        atr = self.get_atr(df)

        for i in range(5, min(50, len(df) - 2)):
            idx = len(df) - 1 - i
            candle  = df.iloc[idx]
            move_after = df.iloc[idx + 1: idx + 4]

            if move_after.empty:
                continue

            ob_size = abs(float(candle["high"]) - float(candle["low"]))
            if ob_size < atr * 0.3:
                continue  # OB trop petit = bruit

            # BULLISH OB : bougie rouge suivie d'impulsion haussière
            if bos == "BULL" and candle["close"] < candle["open"]:
                impulse = float(move_after["high"].max()) - float(candle["high"])
                if impulse >= ob_size * OB_MIN_MOVE:
                    ob_high = float(candle["open"])
                    ob_low  = float(candle["low"])
                    # Prix doit être DANS la zone OB pour valider
                    if ob_low <= current_price <= ob_high * 1.002:
                        obs.append({
                            "type": "BULL", "high": ob_high, "low": ob_low,
                            "idx": idx, "size": ob_size
                        })

            # BEARISH OB : bougie verte suivie d'impulsion baissière
            elif bos == "BEAR" and candle["close"] > candle["open"]:
                impulse = float(candle["low"]) - float(move_after["low"].min())
                if impulse >= ob_size * OB_MIN_MOVE:
                    ob_high = float(candle["high"])
                    ob_low  = float(candle["close"])
                    if ob_low * 0.998 <= current_price <= ob_high:
                        obs.append({
                            "type": "BEAR", "high": ob_high, "low": ob_low,
                            "idx": idx, "size": ob_size
                        })

        return obs

    # ─── Fair Value Gap (FVG) ────────────────────────────────────────────────

    def find_fvg(self, df: pd.DataFrame, bos: str) -> list:
        """
        Détecte les Fair Value Gaps (déséquilibres de liquidité).

        Bullish FVG : high[i-2] < low[i] → gap entre bougie i-2 et i
        Bearish FVG : low[i-2] > high[i] → gap entre bougie i-2 et i

        Le prix qui revient dans un FVG = zone d'entrée haute probabilité.
        """
        fvgs = []
        if len(df) < 5:
            return fvgs

        current_price = float(df["close"].iloc[-1])

        for i in range(2, min(30, len(df))):
            idx = len(df) - 1 - i
            if idx < 2:
                break

            c1 = df.iloc[idx - 2]  # Bougie [i-2]
            c3 = df.iloc[idx]       # Bougie [i]

            # BULLISH FVG
            if bos == "BULL":
                gap_low  = float(c1["high"])
                gap_high = float(c3["low"])
                gap_size = gap_high - gap_low

                if gap_size / float(c1["close"]) >= FVG_MIN_SIZE:
                    # Prix revient dans le FVG = signal BUY
                    if gap_low <= current_price <= gap_high:
                        fvgs.append({
                            "type": "BULL", "high": gap_high,
                            "low": gap_low, "idx": idx
                        })

            # BEARISH FVG
            elif bos == "BEAR":
                gap_high = float(c1["low"])
                gap_low  = float(c3["high"])
                gap_size = gap_high - gap_low

                if gap_size / float(c1["close"]) >= FVG_MIN_SIZE:
                    # Prix revient dans le FVG = signal SELL
                    if gap_low <= current_price <= gap_high:
                        fvgs.append({
                            "type": "BEAR", "high": gap_high,
                            "low": gap_low, "idx": idx
                        })

        return fvgs

    # ─── Liquidity Sweep ─────────────────────────────────────────────────────

    def detect_liquidity_sweep(self, df: pd.DataFrame) -> str:
        """
        Chasse aux stops (liquidity sweep) :
        - Prix perce un swing high puis revient sous → SELL (BSL swept → reversal)
        - Prix perce un swing low puis revient au-dessus → BUY (SSL swept → reversal)

        C'est le signal le plus puissant du SMC.
        """
        if len(df) < SWING_LOOKBACK + 5:
            return "NONE"

        highs, lows = self.find_swings(df, SWING_LOOKBACK)
        if not highs or not lows:
            return "NONE"

        curr      = df.iloc[-1]
        prev      = df.iloc[-2]
        curr_close = float(curr["close"])

        # BSL Sweep : mèche au-dessus du swing high puis close en-dessous → SELL
        if highs:
            last_sh = float(df["high"].iloc[highs[-1]])
            if float(prev["high"]) > last_sh and curr_close < last_sh:
                return "BSL_SWEPT"   # → vendre le rebond baissier

        # SSL Sweep : mèche sous le swing low puis close au-dessus → BUY
        if lows:
            last_sl = float(df["low"].iloc[lows[-1]])
            if float(prev["low"]) < last_sl and curr_close > last_sl:
                return "SSL_SWEPT"   # → acheter le rebond haussier

        return "NONE"

    # ─── Filtre session ───────────────────────────────────────────────────────

    def is_session_ok(self) -> bool:
        hour = datetime.now(timezone.utc).hour
        return hour not in AVOID_HOURS_UTC

    # ─── SIGNAL PRINCIPAL ────────────────────────────────────────────────────

    def get_signal(self, df: pd.DataFrame, symbol: str = None) -> tuple:
        """
        Retourne (signal, score, confirmations).

        Logique SMC inspirée de Station X :
        1. Identifier le contexte BOS (Bull/Bear)
        2. Trouver Order Blocks dans la tendance
        3. Trouver FVGs dans la tendance
        4. Détecter Liquidity Sweeps
        5. Signal si ≥ 2 confirmations SMC alignées
        """
        if len(df) < 60:
            return SIGNAL_HOLD, 0, []

        if not self.is_session_ok():
            return SIGNAL_HOLD, 0, []

        bos   = self.detect_bos(df)
        sweep = self.detect_liquidity_sweep(df)
        obs   = self.find_order_blocks(df, bos)
        fvgs  = self.find_fvg(df, bos)

        confirmations_buy  = []
        confirmations_sell = []

        # ── Confirmations BUY ─────────────────────────────────────────────────
        if bos == "BULL":
            confirmations_buy.append(f"📈 BOS Haussier (prix > dernier swing high)")
        if sweep == "SSL_SWEPT":
            confirmations_buy.append(f"💧 SSL Sweep — liquidités basses chassées → rebond")
        if any(ob["type"] == "BULL" for ob in obs):
            confirmations_buy.append(f"📦 Order Block haussier validé (prix dans la zone OB)")
        if any(fvg["type"] == "BULL" for fvg in fvgs):
            confirmations_buy.append(f"⚡ Fair Value Gap haussier (déséquilibre rempli)")

        # ── Confirmations SELL ────────────────────────────────────────────────
        if bos == "BEAR":
            confirmations_sell.append(f"📉 BOS Baissier (prix < dernier swing low)")
        if sweep == "BSL_SWEPT":
            confirmations_sell.append(f"💧 BSL Sweep — liquidités hautes chassées → reversal")
        if any(ob["type"] == "BEAR" for ob in obs):
            confirmations_sell.append(f"📦 Order Block baissier validé (prix dans la zone OB)")
        if any(fvg["type"] == "BEAR" for fvg in fvgs):
            confirmations_sell.append(f"⚡ Fair Value Gap baissier (déséquilibre rempli)")

        buy_score  = len(confirmations_buy)
        sell_score = len(confirmations_sell)

        logger.debug(
            f"SMC | BOS={bos} | Sweep={sweep} | OBs={len(obs)} | FVGs={len(fvgs)} | "
            f"BUY={buy_score} SELL={sell_score}"
        )

        if buy_score >= SMC_REQUIRED and buy_score > sell_score:
            logger.info(f"🟢 SMC BUY {buy_score}/4 | {' + '.join(confirmations_buy)}")
            return SIGNAL_BUY, buy_score, confirmations_buy

        if sell_score >= SMC_REQUIRED and sell_score > buy_score:
            logger.info(f"🔴 SMC SELL {sell_score}/4 | {' + '.join(confirmations_sell)}")
            return SIGNAL_SELL, sell_score, confirmations_sell

        return SIGNAL_HOLD, max(buy_score, sell_score), []
