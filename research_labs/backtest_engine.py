#!/usr/bin/env python3
"""
backtest_engine.py — Crash-Test Historique Autonome

MODULE AUTONOME : télécharge 3 ans de données, les injecte dans le pipeline
Strategy existant, simule l'exécution avec frictions réalistes, et génère
un tear sheet quantitatif complet.

RÈGLES D'OR :
  - NE modifie AUCUN des 43 moteurs
  - Mock Telegram / Web3 / tous appels réseau réels
  - Frictions : 0.1% fees, 0.05% slippage, 50ms latence simulée

Usage :
    python3 backtest_engine.py                       # BTC + ETH, 3 ans
    python3 backtest_engine.py --symbol BTCUSD       # BTC seul
    python3 backtest_engine.py --years 1             # 1 an seulement
"""

import os
import sys
import time
import math
import argparse
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from loguru import logger

import numpy as np
import pandas as pd

# ─── Strategy (import DIRECT — on utilise le même moteur, zéro modification) ─
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from brokers.capital_client import (
    ASSET_PROFILES, ASSET_CLASS_MAP, RISK_BY_CLASS,
    FRIDAY_KILLSWITCH_HOUR, FRIDAY_KILLSWITCH_MINUTE,
    get_asset_class, get_risk_params,
)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

# Frictions (NON NÉGOCIABLES)
MAKER_TAKER_FEE  = 0.001     # 0.1% par trade (entrée + sortie = 0.2% round trip)
SLIPPAGE_PCT     = 0.0005    # 0.05% slippage sur chaque ordre marché
LATENCY_MS       = 50        # 50ms de latence simulée

# Capital initial & risque
INITIAL_CAPITAL  = 10_000.0  # 10K€ de capital initial
RISK_PER_TRADE   = 0.005     # 0.5% du capital par trade
MAX_OPEN_TRADES  = 10        # Max positions ouvertes (élargi pour 50 actifs)
# R:R et Time Stop sont maintenant DYNAMIQUES par classe d'actif
# (voir RISK_BY_CLASS dans capital_client.py)

# ─── YF_TICKER_MAP : traduction broker → Yahoo Finance ───────────────────────
YF_TICKER_MAP = {
    # Forex majeurs
    "EURUSD": "EURUSD=X", "USDJPY": "JPY=X",   "GBPUSD": "GBPUSD=X",
    "GBPJPY": "GBPJPY=X", "EURJPY": "EURJPY=X", "USDCHF": "CHF=X",
    "AUDNZD": "AUDNZD=X", "AUDJPY": "AUDJPY=X", "NZDJPY": "NZDJPY=X",
    "EURCHF": "EURCHF=X", "CHFJPY": "CHFJPY=X",
    # Forex Daily
    "AUDUSD": "AUDUSD=X", "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X",
    "EURAUD": "EURAUD=X", "GBPAUD": "GBPAUD=X", "AUDCAD": "AUDCAD=X",
    "GBPCAD": "GBPCAD=X", "GBPCHF": "GBPCHF=X", "CADCHF": "CADCHF=X",
    # Commodités
    "GOLD": "GC=F", "SILVER": "SI=F", "OIL_CRUDE": "CL=F",
    "OIL_BRENT": "BZ=F", "COPPER": "HG=F", "NATURALGAS": "NG=F",
    # Indices
    "US500": "^GSPC", "US100": "^NDX", "US30": "^DJI",
    "DE40": "^GDAXI", "FR40": "^FCHI", "UK100": "^FTSE",
    "J225": "^N225", "AU200": "^AXJO",
    # Crypto
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "BNBUSD": "BNB-USD",
    "XRPUSD": "XRP-USD", "SOLUSD": "SOL-USD", "AVAXUSD": "AVAX-USD",
    # Stocks US
    "AAPL": "AAPL", "TSLA": "TSLA", "NVDA": "NVDA", "MSFT": "MSFT",
    "META": "META", "GOOGL": "GOOGL", "AMZN": "AMZN", "AMD": "AMD",
}

# Auto-generate BACKTEST_INSTRUMENTS from ASSET_PROFILES
BACKTEST_INSTRUMENTS = {}
for _sym in ASSET_PROFILES:
    _ticker = YF_TICKER_MAP.get(_sym)
    if _ticker:
        BACKTEST_INSTRUMENTS[_sym] = {"ticker": _ticker, "profile_key": _sym}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA DOWNLOAD
# ═══════════════════════════════════════════════════════════════════════════════

def download_historical_data(ticker: str, years: int = 3,
                              interval: str = "1h") -> Optional[pd.DataFrame]:
    """
    Télécharge les données OHLCV historiques via yfinance.
    Fallback vers des données synthétiques si yfinance n'est pas disponible.
    """
    try:
        import yfinance as yf
        end = datetime.now()
        start = end - timedelta(days=365 * years)
        logger.info(f"📥 Téléchargement {ticker} ({start.date()} → {end.date()})...")

        # yfinance a une limite de 730 jours pour 1h — on download par chunks
        all_dfs = []
        chunk_days = 720
        current_end = end
        while current_end > start:
            current_start = max(start, current_end - timedelta(days=chunk_days))
            df_chunk = yf.download(
                ticker, start=current_start, end=current_end,
                interval=interval, progress=False, auto_adjust=True,
            )
            if df_chunk is not None and not df_chunk.empty:
                all_dfs.append(df_chunk)
            current_end = current_start - timedelta(hours=1)

        if not all_dfs:
            logger.warning(f"⚠️ Pas de données yfinance pour {ticker}")
            return None

        df = pd.concat(all_dfs)
        df = df[~df.index.duplicated(keep='first')]
        df.sort_index(inplace=True)

        # Normaliser les colonnes (yfinance v1.2+ uses MultiIndex)
        if isinstance(df.columns, pd.MultiIndex):
            # MultiIndex: ('Close', 'BTC-USD') → 'close'
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        # Deduplicate columns (yfinance can return duplicates)
        df = df.loc[:, ~df.columns.duplicated()]
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                logger.error(f"❌ Colonne manquante: {col}")
                return None

        # Timezone
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")

        logger.info(
            f"✅ {ticker}: {len(df)} bougies ({df.index[0].date()} → {df.index[-1].date()})"
        )
        return df

    except ImportError:
        logger.warning("⚠️ yfinance non installé — génération de données synthétiques")
        return _generate_synthetic_data(ticker, years, interval)
    except Exception as e:
        logger.error(f"❌ Download {ticker}: {e}")
        return _generate_synthetic_data(ticker, years, interval)


def _generate_synthetic_data(ticker: str, years: int, interval: str) -> pd.DataFrame:
    """Génère des données synthétiques réalistes si yfinance n'est pas disponible."""
    logger.info(f"🔧 Génération de données synthétiques pour {ticker} ({years} ans)...")

    periods_per_day = 24 if interval == "1h" else 288
    total_periods = periods_per_day * 365 * years
    dates = pd.date_range(
        end=datetime.now(tz=timezone.utc),
        periods=total_periods,
        freq="1h" if interval == "1h" else "5min",
    )

    # Prix de base selon l'instrument
    base_prices = {"BTC-USD": 30000, "ETH-USD": 2000}
    base = base_prices.get(ticker, 1000)

    # Random walk avec volatilité réaliste (crypto 1H ≈ 0.3% per candle)
    np.random.seed(hash(ticker) % 2**31)
    returns = np.random.normal(0, 0.003, total_periods)  # 0.3% vol par bougie
    # Ajouter des régimes de marché (tendances)
    trend = np.cumsum(np.random.normal(0, 0.0003, total_periods))
    prices = base * np.exp(np.cumsum(returns) + trend)

    # OHLCV synthétique
    high_noise = np.abs(np.random.normal(0, 0.0015, total_periods))
    low_noise = np.abs(np.random.normal(0, 0.0015, total_periods))

    df = pd.DataFrame({
        "open": prices * (1 + np.random.normal(0, 0.001, total_periods)),
        "high": prices * (1 + high_noise),
        "low": prices * (1 - low_noise),
        "close": prices,
        "volume": np.random.lognormal(10, 1, total_periods),
    }, index=dates)

    # Assurer high >= max(open, close) et low <= min(open, close)
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)

    logger.info(f"✅ Synthétique {ticker}: {len(df)} bougies")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# FRICTION SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class FrictionModel:
    """Simule les frictions de marché réalistes."""
    fee_rate: float = MAKER_TAKER_FEE
    slippage_pct: float = SLIPPAGE_PCT
    latency_ms: float = LATENCY_MS

    def apply_entry_friction(self, price: float, direction: str) -> float:
        """Applique slippage + fees à l'entrée."""
        slip = price * self.slippage_pct
        if direction == "BUY":
            return price + slip  # On achète plus cher
        else:
            return price - slip  # On vend moins cher

    def apply_exit_friction(self, price: float, direction: str) -> float:
        """Applique slippage à la sortie (fees calculées séparément sur PnL)."""
        slip = price * self.slippage_pct
        if direction == "BUY":
            return price - slip  # On vend moins cher à la sortie
        else:
            return price + slip  # On rachète plus cher

    def compute_fees(self, size: float, entry_price: float,
                     exit_price: float) -> float:
        """Frais totaux (entrée + sortie)."""
        return (size * entry_price * self.fee_rate +
                size * exit_price * self.fee_rate)


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestTrade:
    """Représente un trade simulé."""
    instrument: str
    direction: str
    entry_price: float        # Prix d'entrée APRÈS friction
    raw_entry: float          # Prix d'entrée AVANT friction
    sl: float
    tp: float
    size: float
    entry_time: datetime
    exit_price: float = 0.0
    exit_time: Optional[datetime] = None
    pnl_gross: float = 0.0    # PnL brut (avant fees)
    pnl_net: float = 0.0      # PnL net (après fees + slippage)
    fees: float = 0.0
    exit_reason: str = ""      # "TP", "SL", "TIME_STOP", "TRAILING"
    max_favorable: float = 0.0  # MFE (Max Favorable Excursion)
    max_adverse: float = 0.0    # MAE (Max Adverse Excursion)
    risk_r: float = 0.0        # Risque en R multiples


class PositionTracker:
    """Gère les positions ouvertes et fermées."""

    def __init__(self, initial_capital: float, friction: FrictionModel):
        self.capital = initial_capital
        self.initial_capital = initial_capital
        self.peak_capital = initial_capital
        self.friction = friction
        self.open_trades: Dict[str, BacktestTrade] = {}
        self.closed_trades: List[BacktestTrade] = []
        self.equity_curve: List[Tuple[datetime, float]] = []

    def can_open(self, instrument: str) -> bool:
        return (instrument not in self.open_trades and
                len(self.open_trades) < MAX_OPEN_TRADES)

    def open_trade(self, instrument: str, direction: str,
                   raw_entry: float, sl: float, tp: float,
                   timestamp: datetime) -> Optional[BacktestTrade]:
        """Ouvre un trade avec frictions appliquées."""
        if not self.can_open(instrument):
            return None

        # M38 Convexity Gate : enforce R:R >= rr_min (dynamique par asset class)
        risk = abs(raw_entry - sl)
        if risk <= 0:
            return None
        reward = abs(tp - raw_entry)
        rr = reward / risk
        # Get dynamic R:R minimum per asset class
        risk_params = get_risk_params(instrument)
        rr_min = risk_params["rr_min"]
        # Auto-adjust TP to meet minimum R:R (same as live bot M38)
        if rr < rr_min:
            if direction == "BUY":
                tp = raw_entry + risk * rr_min
            else:
                tp = raw_entry - risk * rr_min

        # Frictions d'entrée
        entry = self.friction.apply_entry_friction(raw_entry, direction)

        # Position sizing (0.5% du capital)
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return None
        risk_amt = self.capital * RISK_PER_TRADE
        size = risk_amt / sl_dist

        trade = BacktestTrade(
            instrument=instrument,
            direction=direction,
            entry_price=entry,
            raw_entry=raw_entry,
            sl=sl,
            tp=tp,
            size=size,
            entry_time=timestamp,
            risk_r=sl_dist,
        )
        self.open_trades[instrument] = trade
        logger.debug(
            f"📈 TRADE OPEN {direction} {instrument} @ {entry:.2f} "
            f"SL={sl:.2f} TP={tp:.2f} size={size:.4f}"
        )
        return trade

    def update_trades(self, prices: Dict[str, Tuple[float, float, float, float]],
                      timestamp: datetime):
        """
        Met à jour les trades ouverts avec les prix OHLCV de la bougie courante.
        prices: {instrument: (open, high, low, close)}
        """
        to_close = []

        for instrument, trade in self.open_trades.items():
            if instrument not in prices:
                continue

            o, h, l, c = prices[instrument]

            # MFE / MAE
            if trade.direction == "BUY":
                trade.max_favorable = max(trade.max_favorable, h - trade.entry_price)
                trade.max_adverse = max(trade.max_adverse, trade.entry_price - l)
                # SL touché ?
                if l <= trade.sl:
                    exit_price = self.friction.apply_exit_friction(trade.sl, "BUY")
                    to_close.append((instrument, exit_price, "SL", timestamp))
                # TP touché ?
                elif h >= trade.tp:
                    exit_price = self.friction.apply_exit_friction(trade.tp, "BUY")
                    to_close.append((instrument, exit_price, "TP", timestamp))
            else:  # SELL
                trade.max_favorable = max(trade.max_favorable, trade.entry_price - l)
                trade.max_adverse = max(trade.max_adverse, h - trade.entry_price)
                # SL touché ?
                if h >= trade.sl:
                    exit_price = self.friction.apply_exit_friction(trade.sl, "SELL")
                    to_close.append((instrument, exit_price, "SL", timestamp))
                # TP touché ?
                elif l <= trade.tp:
                    exit_price = self.friction.apply_exit_friction(trade.tp, "SELL")
                    to_close.append((instrument, exit_price, "TP", timestamp))

            # M40 Time Stop (dynamique par classe d'actif)
            if instrument not in [x[0] for x in to_close]:
                age_hours = (timestamp - trade.entry_time).total_seconds() / 3600
                risk_params = get_risk_params(instrument)
                max_hold_h = risk_params["time_stop_h"]
                if age_hours > max_hold_h:
                    exit_price = self.friction.apply_exit_friction(c, trade.direction)
                    to_close.append((instrument, exit_price, "TIME_STOP", timestamp))

            # FRIDAY KILL-SWITCH: close TRADFI before weekend
            if instrument not in [x[0] for x in to_close]:
                cls = get_asset_class(instrument)
                if cls == "TRADFI" and hasattr(timestamp, 'weekday'):
                    if (timestamp.weekday() == 4 and
                        (timestamp.hour > FRIDAY_KILLSWITCH_HOUR or
                         (timestamp.hour == FRIDAY_KILLSWITCH_HOUR and
                          timestamp.minute >= FRIDAY_KILLSWITCH_MINUTE))):
                        exit_price = self.friction.apply_exit_friction(c, trade.direction)
                        to_close.append((instrument, exit_price, "FRIDAY_KILL", timestamp))

        # Fermer les trades
        for instrument, exit_price, reason, ts in to_close:
            self._close_trade(instrument, exit_price, reason, ts)

    def _close_trade(self, instrument: str, exit_price: float,
                     reason: str, timestamp: datetime):
        trade = self.open_trades.pop(instrument, None)
        if trade is None:
            return

        trade.exit_price = exit_price
        trade.exit_time = timestamp
        trade.exit_reason = reason

        # PnL brut
        if trade.direction == "BUY":
            trade.pnl_gross = (exit_price - trade.entry_price) * trade.size
        else:
            trade.pnl_gross = (trade.entry_price - exit_price) * trade.size

        # Fees
        trade.fees = self.friction.compute_fees(
            trade.size, trade.entry_price, exit_price
        )

        # PnL net
        trade.pnl_net = trade.pnl_gross - trade.fees

        # Mise à jour du capital
        self.capital += trade.pnl_net
        self.peak_capital = max(self.peak_capital, self.capital)

        self.closed_trades.append(trade)
        self.equity_curve.append((timestamp, self.capital))
        icon = "✅" if trade.pnl_net > 0 else "❌"
        logger.info(
            f"{icon} TRADE CLOSE {trade.direction} {instrument} | "
            f"{reason} | PnL={trade.pnl_net:+.2f}€ fees={trade.fees:.2f}€ | "
            f"Capital={self.capital:,.0f}€"
        )

    def get_equity_at(self, timestamp: datetime) -> float:
        """Capital + PnL non réalisé des positions ouvertes."""
        unrealized = sum(
            t.max_favorable if t.pnl_gross > 0 else -t.max_adverse
            for t in self.open_trades.values()
        )
        return self.capital + unrealized


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTEST ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Moteur de backtest autonome.
    Wrapper qui injecte les données historiques dans le pipeline Strategy existant.
    """

    def __init__(self, years: int = 3, symbols: List[str] = None):
        self.years = years
        self.symbols = symbols or list(BACKTEST_INSTRUMENTS.keys())
        self.strategy = Strategy()
        self.friction = FrictionModel()
        self.tracker = PositionTracker(INITIAL_CAPITAL, self.friction)
        self._total_signals = 0
        self._filtered_signals = 0

    def run(self) -> dict:
        """Exécute le backtest complet."""
        logger.info("=" * 70)
        logger.info("🏁 BACKTEST ENGINE — CRASH-TEST HISTORIQUE")
        logger.info(f"   Capital initial : {INITIAL_CAPITAL:,.0f}€")
        logger.info(f"   Période : {self.years} ans")
        logger.info(f"   Instruments : {self.symbols}")
        logger.info(f"   Frictions : fees={MAKER_TAKER_FEE*100:.1f}% "
                     f"slip={SLIPPAGE_PCT*100:.2f}% lat={LATENCY_MS}ms")
        logger.info("=" * 70)

        # 1. Télécharger les données
        data = {}
        for symbol in self.symbols:
            info = BACKTEST_INSTRUMENTS.get(symbol, {})
            ticker = info.get("ticker", symbol)
            df = download_historical_data(ticker, self.years)
            if df is not None and len(df) > 300:
                data[symbol] = df
            else:
                logger.warning(f"⚠️ {symbol}: données insuffisantes — exclu")

        if not data:
            logger.error("❌ Aucune donnée disponible — abandon")
            return {}

        # 2. Exécuter la simulation
        t_start = time.time()

        for symbol, df_full in data.items():
            self._simulate_instrument(symbol, df_full)

        elapsed = time.time() - t_start

        # 3. Fermer les positions restantes au dernier prix
        for symbol in list(self.tracker.open_trades.keys()):
            if symbol in data:
                last_close = float(data[symbol].iloc[-1]["close"])
                exit_price = self.friction.apply_exit_friction(
                    last_close, self.tracker.open_trades[symbol].direction
                )
                self.tracker._close_trade(
                    symbol, exit_price, "BACKTEST_END",
                    data[symbol].index[-1]
                )

        # 4. Générer le tear sheet
        results = self._compute_tear_sheet(elapsed)
        self._print_tear_sheet(results)
        return results

    def _simulate_instrument(self, symbol: str, df_full: pd.DataFrame):
        """
        Simule un instrument en parcourant les bougies historiques.
        OPTIMISÉ : compute_indicators() UNE SEULE FOIS sur le dataset complet,
        puis itération rapide sur les barres.
        """
        profile_key = BACKTEST_INSTRUMENTS[symbol]["profile_key"]
        profile = ASSET_PROFILES.get(profile_key, {})
        lookback = 300  # Bougies de warmup pour les indicateurs
        total_bars = len(df_full)

        logger.info(f"📊 Simulation {symbol}: {total_bars} bougies, "
                     f"profil={profile.get('strat', 'BK')}")

        # ═══ PRE-COMPUTE: tous les indicateurs en une seule passe ═══
        logger.info(f"  ⚙️ Calcul des indicateurs sur {total_bars} bougies...")
        df_with_indicators = self.strategy.compute_indicators(df_full.copy())
        logger.info(f"  ✅ Indicateurs calculés: {len(df_with_indicators)} barres valides")

        if len(df_with_indicators) < lookback:
            logger.warning(f"⚠️ {symbol}: pas assez de données après compute → skip")
            return

        for i in range(lookback, len(df_with_indicators)):
            try:
                # Fenêtre glissante sur le dataframe pré-calculé
                window = df_with_indicators.iloc[max(0, i - lookback):i + 1]

                if len(window) < 30:
                    continue

                # Obtenir le signal
                sig, score, confirmations = self.strategy.get_signal(
                    window, symbol=symbol, asset_profile=profile
                )
                self._total_signals += 1

                # Prix courant
                curr = window.iloc[-1]
                c = float(curr["close"])
                h = float(curr["high"])
                l = float(curr["low"])
                ts = window.index[-1]
                if not isinstance(ts, datetime):
                    ts = pd.Timestamp(ts)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")

                # Mettre à jour les trades ouverts
                prices = {symbol: (float(curr["open"]), h, l, c)}
                self.tracker.update_trades(prices, ts)

                # Nouveau signal ?
                if sig in (SIGNAL_BUY, SIGNAL_SELL):
                    self._filtered_signals += 1

                    # Calculer SL/TP via le profil (identique au bot live)
                    atr = float(curr.get("atr", 0))
                    if atr <= 0:
                        continue

                    tp1_mult = profile.get("tp1", 1.5)
                    sl_buf = profile.get("sl_buffer", 0.12)
                    strat = profile.get("strat", "BK")

                    if strat == "BK":
                        range_lb = profile.get("range_lb", 4)
                        sr = self.strategy.compute_session_range(
                            window, range_lookback=range_lb
                        )
                        rng = sr["size"]
                        if rng <= 0:
                            continue

                        if sig == SIGNAL_BUY:
                            sl = sr["low"] - rng * sl_buf
                            tp = c + rng * tp1_mult
                        else:
                            sl = sr["high"] + rng * sl_buf
                            tp = c - rng * tp1_mult
                    elif strat == "MR":
                        sl_mult = profile.get("sl_buffer", 1.0)
                        if sig == SIGNAL_BUY:
                            sl = c - atr * sl_mult
                            tp = c + atr * tp1_mult
                        else:
                            sl = c + atr * sl_mult
                            tp = c - atr * tp1_mult
                    else:  # TF
                        sl_mult = profile.get("sl_buffer", 1.0)
                        if sig == SIGNAL_BUY:
                            sl = c - atr * sl_mult
                            tp = c + atr * tp1_mult
                        else:
                            sl = c + atr * sl_mult
                            tp = c - atr * tp1_mult

                    # Ouvrir le trade (M38 Convexity gate inclus)
                    self.tracker.open_trade(
                        symbol, sig, c, sl, tp, ts
                    )

            except Exception as e:
                logger.debug(f"Backtest bar {i} {symbol}: {e}")
                continue

            # Progress report
            if i > 0 and i % 5000 == 0:
                pct = i / len(df_with_indicators) * 100
                trades = len(self.tracker.closed_trades)
                logger.info(
                    f"  ⏳ {symbol}: {pct:.0f}% ({i}/{len(df_with_indicators)}) | "
                    f"{trades} trades fermés | capital={self.tracker.capital:,.0f}€"
                )

    # ═══════════════════════════════════════════════════════════════════════
    # TEAR SHEET
    # ═══════════════════════════════════════════════════════════════════════

    def _compute_tear_sheet(self, elapsed_s: float) -> dict:
        """Calcule toutes les métriques de performance."""
        trades = self.tracker.closed_trades
        n = len(trades)

        if n == 0:
            return {"total_trades": 0, "elapsed_s": elapsed_s}

        pnls = [t.pnl_net for t in trades]
        gross_pnls = [t.pnl_gross for t in trades]
        wins = [t for t in trades if t.pnl_net > 0]
        losses = [t for t in trades if t.pnl_net <= 0]

        # Basic stats
        total_pnl = sum(pnls)
        total_pnl_gross = sum(gross_pnls)
        total_fees = sum(t.fees for t in trades)
        win_rate = len(wins) / n * 100 if n > 0 else 0

        # Average R:R
        avg_win = np.mean([t.pnl_net for t in wins]) if wins else 0
        avg_loss = abs(np.mean([t.pnl_net for t in losses])) if losses else 1
        avg_rr = avg_win / avg_loss if avg_loss > 0 else 0

        # Profit Factor
        gross_profit = sum(t.pnl_net for t in wins)
        gross_loss = abs(sum(t.pnl_net for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        # Max Drawdown
        equity = [self.tracker.initial_capital]
        for t in trades:
            equity.append(equity[-1] + t.pnl_net)
        equity = np.array(equity)
        peak = np.maximum.accumulate(equity)
        drawdowns = (equity - peak) / peak * 100
        max_dd = float(np.min(drawdowns))

        # Sharpe Ratio (annualisé)
        if len(pnls) > 1:
            returns = np.array(pnls) / self.tracker.initial_capital
            # Ratio trades per year ≈ n / years
            trades_per_year = n / max(self.years, 1)
            sharpe = (np.mean(returns) / np.std(returns) *
                      np.sqrt(trades_per_year)) if np.std(returns) > 0 else 0
        else:
            sharpe = 0

        # Sortino Ratio (downside deviation only)
        if len(pnls) > 1:
            returns = np.array(pnls) / self.tracker.initial_capital
            downside_returns = returns[returns < 0]
            if len(downside_returns) > 0:
                downside_dev = np.std(downside_returns)
                trades_per_year = n / max(self.years, 1)
                sortino = (np.mean(returns) / downside_dev *
                           np.sqrt(trades_per_year)) if downside_dev > 0 else 0
            else:
                sortino = float('inf')
        else:
            sortino = 0

        # Calmar Ratio
        calmar = (total_pnl / self.tracker.initial_capital * 100 /
                  abs(max_dd)) if max_dd != 0 else 0

        # Trade duration stats
        durations = [
            (t.exit_time - t.entry_time).total_seconds() / 3600
            for t in trades if t.exit_time
        ]
        avg_duration = np.mean(durations) if durations else 0

        # Exit reason breakdown
        exit_reasons = {}
        for t in trades:
            exit_reasons[t.exit_reason] = exit_reasons.get(t.exit_reason, 0) + 1

        # Best/Worst trade
        best_trade = max(trades, key=lambda t: t.pnl_net)
        worst_trade = min(trades, key=lambda t: t.pnl_net)

        # Consecutive wins/losses
        max_consec_wins = max_consec_losses = consec = 0
        last_was_win = None
        for t in trades:
            is_win = t.pnl_net > 0
            if is_win == last_was_win:
                consec += 1
            else:
                consec = 1
            last_was_win = is_win
            if is_win:
                max_consec_wins = max(max_consec_wins, consec)
            else:
                max_consec_losses = max(max_consec_losses, consec)

        # Monthly returns
        monthly_pnl = {}
        for t in trades:
            if t.exit_time:
                key = t.exit_time.strftime("%Y-%m")
                monthly_pnl[key] = monthly_pnl.get(key, 0) + t.pnl_net

        return {
            # Headline
            "total_trades": n,
            "total_pnl_net": round(total_pnl, 2),
            "total_pnl_gross": round(total_pnl_gross, 2),
            "total_fees": round(total_fees, 2),
            "final_capital": round(self.tracker.capital, 2),
            "return_pct": round((self.tracker.capital - INITIAL_CAPITAL) /
                                INITIAL_CAPITAL * 100, 2),

            # Win/Loss
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 1),
            "avg_rr": round(avg_rr, 2),
            "profit_factor": round(profit_factor, 2),

            # Risk
            "max_drawdown_pct": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "calmar_ratio": round(calmar, 2),

            # Trade details
            "avg_duration_hours": round(avg_duration, 1),
            "best_trade": round(best_trade.pnl_net, 2),
            "worst_trade": round(worst_trade.pnl_net, 2),
            "max_consec_wins": max_consec_wins,
            "max_consec_losses": max_consec_losses,

            # Exit reasons
            "exit_reasons": exit_reasons,

            # Monthly
            "monthly_pnl": monthly_pnl,
            "profitable_months": sum(1 for v in monthly_pnl.values() if v > 0),
            "total_months": len(monthly_pnl),

            # Meta
            "signals_generated": self._total_signals,
            "signals_traded": self._filtered_signals,
            "elapsed_seconds": round(elapsed_s, 1),
            "friction_model": {
                "fees": f"{MAKER_TAKER_FEE*100:.1f}%",
                "slippage": f"{SLIPPAGE_PCT*100:.2f}%",
                "latency": f"{LATENCY_MS}ms",
            },
        }

    def _print_tear_sheet(self, r: dict):
        """Affiche le tear sheet dans le terminal."""
        if r.get("total_trades", 0) == 0:
            print("\n❌ AUCUN TRADE EXÉCUTÉ — vérifier les paramètres\n")
            return

        print("\n")
        print("═" * 70)
        print("  💀 BACKTEST TEAR SHEET — CRASH-TEST HISTORIQUE")
        print("═" * 70)

        # Section 1: Performance
        pnl = r["total_pnl_net"]
        icon = "🟢" if pnl > 0 else "🔴"
        print(f"""
  {icon} PERFORMANCE
  ────────────────────────────────────────────────
  Capital initial     : {INITIAL_CAPITAL:>12,.0f} €
  Capital final       : {r['final_capital']:>12,.0f} €
  PnL Net (après fee) : {pnl:>+12,.2f} €
  PnL Brut            : {r['total_pnl_gross']:>+12,.2f} €
  Total Frais payés   : {r['total_fees']:>12,.2f} €
  Return %            : {r['return_pct']:>+11.2f} %
""")

        # Section 2: Win/Loss
        print(f"""  📊 WIN/LOSS ANALYSIS
  ────────────────────────────────────────────────
  Total Trades        : {r['total_trades']:>8}
  Wins / Losses       : {r['wins']:>5} W / {r['losses']:>5} L
  Win Rate            : {r['win_rate']:>7.1f} %
  Avg R:R             : {r['avg_rr']:>7.2f} x
  Profit Factor       : {r['profit_factor']:>7.2f}
  Max Consec Wins     : {r['max_consec_wins']:>8}
  Max Consec Losses   : {r['max_consec_losses']:>8}
""")

        # Section 3: Risk (LE PLUS IMPORTANT)
        dd = r["max_drawdown_pct"]
        dd_icon = "✅" if dd > -10 else ("⚠️" if dd > -20 else "🔴")
        print(f"""  🛡️ RISK METRICS {dd_icon}
  ────────────────────────────────────────────────
  MAX DRAWDOWN        : {dd:>+7.2f} %  ← MÉTRIQUE CLÉ
  Sharpe Ratio        : {r['sharpe_ratio']:>7.2f}
  Sortino Ratio       : {r['sortino_ratio']:>7.2f}
  Calmar Ratio        : {r['calmar_ratio']:>7.2f}
""")

        # Section 4: Trade Details
        print(f"""  ⏱️ TRADE DETAILS
  ────────────────────────────────────────────────
  Durée moyenne       : {r['avg_duration_hours']:>7.1f} heures
  Meilleur trade      : {r['best_trade']:>+12,.2f} €
  Pire trade          : {r['worst_trade']:>+12,.2f} €
""")

        # Section 5: Exit Reasons
        print("  🚪 EXIT REASONS")
        print("  ────────────────────────────────────────────────")
        for reason, count in sorted(r.get("exit_reasons", {}).items(),
                                     key=lambda x: -x[1]):
            pct = count / r["total_trades"] * 100
            print(f"  {reason:20s} : {count:>5} ({pct:.1f}%)")

        # Section 6: Monthly Summary
        monthly = r.get("monthly_pnl", {})
        if monthly:
            print(f"""
  📅 MONTHLY PERFORMANCE
  ────────────────────────────────────────────────
  Mois rentables      : {r['profitable_months']}/{r['total_months']}""")
            for month, pnl_m in sorted(monthly.items())[-12:]:  # Last 12 months
                bar = "█" * max(1, min(30, int(abs(pnl_m) / 50)))
                icon = "🟢" if pnl_m > 0 else "🔴"
                print(f"  {month} : {icon} {pnl_m:>+8,.0f}€ {bar}")

        # Section 7: Friction Impact
        print(f"""
  💰 FRICTION IMPACT
  ────────────────────────────────────────────────
  Fees                : {r['friction_model']['fees']} per trade
  Slippage            : {r['friction_model']['slippage']} per order
  Latence simulée     : {r['friction_model']['latency']}
  Coût total friction : {r['total_fees']:>+12,.2f} €
  Impact sur PnL      : {r['total_pnl_gross'] - r['total_pnl_net']:>+12,.2f} €
""")

        # Verdict
        print("═" * 70)
        if pnl > 0 and dd > -15 and r["sharpe_ratio"] > 0.5:
            print("  ✅ VERDICT : RENTABLE — Bot validé pour le marché réel")
        elif pnl > 0 and dd > -25:
            print("  ⚠️ VERDICT : RENTABLE MAIS RISQUÉ — Drawdown à surveiller")
        elif pnl > 0:
            print("  ⚠️ VERDICT : RENTABLE MAIS DRAWDOWN EXCESSIF — Revoir le sizing")
        else:
            print("  🔴 VERDICT : NON RENTABLE — Optimisation requise")
        print("═" * 70)

        print(f"\n  ⏱️ Backtest exécuté en {r['elapsed_seconds']:.1f}s | "
              f"{r['signals_generated']} signaux analysés | "
              f"{r['signals_traded']} trades tentés\n")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="🏁 Backtest Engine — Crash-Test Historique"
    )
    parser.add_argument(
        "--symbol", "-s", type=str, default=None,
        help="Instrument à tester (BTCUSD, ETHUSD). Default: tous"
    )
    parser.add_argument(
        "--years", "-y", type=int, default=3,
        help="Nombre d'années de données historiques (default: 3)"
    )
    parser.add_argument(
        "--initial-capital", "-c", type=float, default=INITIAL_CAPITAL,
        help=f"Capital initial (default: {INITIAL_CAPITAL})"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Activer les logs détaillés"
    )
    args = parser.parse_args()

    # Configurer le logging
    logger.remove()
    if args.verbose:
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.add(sys.stderr, level="INFO",
                    format="<level>{message}</level>")

    # Configuration
    symbols = [args.symbol] if args.symbol else None
    engine = BacktestEngine(years=args.years, symbols=symbols)
    engine.tracker.capital = args.initial_capital
    engine.tracker.initial_capital = args.initial_capital
    engine.tracker.peak_capital = args.initial_capital
    engine.run()


if __name__ == "__main__":
    main()
