"""
imports.py — Tous les imports et stubs fallback pour NEMESIS v2.0
"""

import os
import time
import signal
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta, date
from loguru import logger

from logger import setup_logger
from config import LOOP_INTERVAL_SECONDS, DAILY_REPORT_HOUR_UTC, MAX_OPEN_TRADES, SESSION_HOURS
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
# ─── Telegram — APEX ULTIMATE Signals Publisher ─────────────────────────────
try:
    import telegram_channel as _tgc
    _TG_OK = bool(_tgc.BOT_TOKEN and _tgc.CHAT_ID)
    if _TG_OK:
        logger.info("📣 Telegram APEX ULTIMATE : publisher chargé ✅")
    else:
        logger.info("ℹ️  Telegram : token/chat_id manquants — notifications désactivées")
except ImportError:
    _tgc = None
    _TG_OK = False


class _TelegramRouter:
    """Router simple : dispatch vers telegram_channel.py."""

    def send_trade(self, text: str, **kw) -> None:
        """Envoie un message trade brut (compatibilité)."""
        if _TG_OK and _tgc:
            _tgc._send(text)

    def send_risk(self, text: str, **kw) -> None:
        """Envoie un message de risque/kill-switch."""
        if _TG_OK and _tgc:
            _tgc._send(f"🛡️ {text}")

    def send_to(self, channel: str, text: str, **kw) -> None:
        if _TG_OK and _tgc:
            _tgc._send(text)

    def send_signal(self, instrument, direction, entry, sl, tp1, tp2,
                    score=0.0, confirmations=None, **kw) -> int | None:
        """Retourne le message_id Telegram pour les replies TP1/TP2/TP3."""
        if _TG_OK and _tgc:
            return _tgc.notify_signal(instrument, direction, entry, sl,
                                      tp1, tp2, score, confirmations)
        return None

    def send_tp1(self, instrument, entry, tp1, direction="BUY",
                 sl=0.0, tp2=0.0, pips=None,
                 reply_to_message_id=None, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_tp1(
                instrument, entry, tp1,
                sl=sl, tp2=tp2, direction=direction, pips=pips,
                reply_to_message_id=reply_to_message_id,
            )
        return False

    def send_tp2(self, instrument, entry=0.0, tp2=0.0, direction="BUY",
                 pips=None, reply_to_message_id=None, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_tp2(
                instrument, entry=entry, tp2=tp2, direction=direction,
                pips=pips, reply_to_message_id=reply_to_message_id,
            )
        return False

    def send_trade_closed(self, instrument, result, pnl_usd, pips,
                          direction, hold_hours=0.0,
                          entry=0.0, close_price=0.0,
                          reply_to_message_id=None, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_trade_closed(
                instrument, result, pnl_usd, pips, direction, hold_hours,
                entry=entry, close_price=close_price,
                reply_to_message_id=reply_to_message_id,
            )
        return False

    def send_kill_switch(self, reason, dd_pct, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_kill_switch(reason, dd_pct)
        return False

    def send_daily_recap(self, balance, pnl_day, trades_today, wins,
                         trade_lines=None, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_daily_recap(balance, pnl_day, trades_today,
                                           wins, trade_lines)
        return False

    def send_weekly_recap(self, stats: dict, **kw) -> bool:
        if _TG_OK and _tgc:
            return _tgc.notify_weekly_recap(stats)
        return False

    def send_performance(self, text: str, **kw) -> None:
        """→ Rapport journalier/hebdo vers compte admin perso (pas le canal PRO)."""
        if _TG_OK and _tgc:
            _tgc._send(text, chat_id=_tgc.ADMIN_ID or _tgc.CHAT_ID)

    def send_stats(self, text: str, silent: bool = False, **kw) -> None:
        """→ Stats/leaderboard vers compte admin perso."""
        if _TG_OK and _tgc:
            _tgc._send(text, chat_id=_tgc.ADMIN_ID or _tgc.CHAT_ID)

    def send_dashboard(self, text: str, silent: bool = False, **kw) -> None:
        """→ Dashboard/session update vers compte admin perso."""
        if _TG_OK and _tgc:
            _tgc._send(text, chat_id=_tgc.ADMIN_ID or _tgc.CHAT_ID)


class TelegramNotifier:
    """Publisher Telegram APEX ULTIMATE Signals — branché sur telegram_channel.py."""

    def __init__(self, *a, **kw):
        self.router       = _TelegramRouter() if _TG_OK else None
        self.hub          = None
        self.gamification = None
        if _TG_OK and _tgc:
            logger.info("📣 TelegramNotifier : router APEX ULTIMATE actif")

    def notify_start(self, balance: float, instruments: list, **kw):
        # → Discord #monitoring uniquement (pas le canal Telegram PRO)
        try:
            import psycopg2 as _pg, os as _os
            _db = _os.getenv("DATABASE_URL", "")
            if _db:
                _cn = _pg.connect(_db)
                _cu = _cn.cursor()
                _cu.execute(
                    "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                    ("BOT_START",
                     f"⚡ NEMESIS démarré — Balance: {balance:,.2f}$ | "
                     f"{len(instruments)} instruments | BK 1H 0.35% risque Multi-TP")
                )
                _cn.commit(); _cu.close(); _cn.close()
        except Exception:
            pass

    def notify_error(self, error: str, **kw): pass
    def notify_crash(self, error: str, **kw): pass
    def notify_morning_brief(self, *a, **kw): pass
    def __bool__(self): return _TG_OK


class TelegramBotHandler:
    """No-op stub: Telegram removed."""
    def __init__(self, *a, **kw): pass
    def register_callbacks(self, **kw): pass
    def start_polling(self): pass
    def stop(self): pass

InlineKeyboardMarkup = None
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from market_context import MarketContext
from database import Database
from brokers.capital_client import (
    CAPITAL_INSTRUMENTS,
    INSTRUMENT_NAMES as CAPITAL_NAMES,
    PIP_FACTOR as CAPITAL_PIP,
    ASSET_PROFILES,
    MICRO_TF_PROFILES,
    PRICE_DECIMALS,
)
# CapitalClient remplacé par le stub silencieux (MT5 est le broker actif)
from brokers.capital_stub import CapitalStub as CapitalClient
# telegram_capital removed — SessionTracker stub
class SessionTracker:
    """Stub: was in telegram_capital.py, tracks session start/end."""
    def __init__(self):
        self._count = 0
        self._pnl = 0.0
    def on_trade(self, pnl=0.0):
        self._count += 1
        self._pnl += pnl
    def reset(self):
        self._count = 0
        self._pnl = 0.0
    @property
    def count(self): return self._count
    @property
    def pnl(self): return self._pnl

class _TgcStub:
    """No-op stub for telegram_capital module."""
    def notify_capital_entry(self, *a, **kw): pass
    def notify_capital_close(self, *a, **kw): pass
    def __getattr__(self, name): return lambda *a, **kw: None

import types as _types
tgc = _TgcStub()
# CapitalWebSocket désactivé — MT5 est le broker actif
class CapitalWebSocket:
    """Stub no-op — Capital.com WebSocket désactivé (MT5 actif)."""
    def __init__(self, *a, **kw): self._running = False
    def start(self): pass
    def stop(self): pass
    def ws_ping(self): pass
    def unwatch(self, *a): pass
    def register_breakout_callback(self, *a): pass
    def register_signal_callback(self, *a): pass

# ─── IC Markets MT5 via MetaApi ──────────────────────────────────────────────
try:
    from brokers.mt5_client import MT5Client
    _MT5_OK = True
except ImportError:
    _MT5_OK = False
    class MT5Client:  # stub silencieux — MT5 désactivé
        def __init__(self, *a, **kw): self._ok = False
        @property
        def available(self): return False
        def get_balance(self): return 0.0
        def get_current_price(self, *a): return None
        def fetch_ohlcv(self, *a, **kw): return None
        def place_market_order(self, *a, **kw): return None
        def close_position(self, *a): return False
        def get_open_positions(self): return []
        def search_markets(self, *a, **kw): return []
        def shutdown(self): pass

from ohlcv_cache import OHLCVCache
from trade_executor import TradeExecutor
from ml_scorer import MLScorer
from concurrent.futures import ThreadPoolExecutor
import threading


try:
    from dashboard import (start_dashboard, update_state as dash_update,
                           update_trade_open as dash_open,
                           update_trade_close as dash_close,
                           update_filter as dash_filter)
    _DASHBOARD_OK = True
except ImportError:
    _DASHBOARD_OK = False
    def dash_update(**_): pass
    def dash_open(*_): pass
    def dash_close(*_): pass
    def dash_filter(*_): pass

try:
    from morning_brief import generate_morning_brief
    _MORNING_OK = True
except ImportError:
    _MORNING_OK = False

try:
    from drift_detector import DriftDetector
    _DRIFT_OK = True
except ImportError:
    _DRIFT_OK = False
    class DriftDetector:  # stub silencieux si module absent
        def record_trade(self, *a, **kw): pass
        def check_drift(self): return {"drift": False}
        def format_status(self): return ""

try:
    from protection_model import ProtectionModel
except ImportError:
    class ProtectionModel:  # stub silencieux si module absent
        def is_blocked(self, symbol): return False
        def on_trade_closed(self, symbol, pnl): pass
        def on_rapid_loss(self, symbol, pct): pass
        def format_status(self): return ""

try:
    from mtf_filter import MTFFilter
except ImportError:
    class MTFFilter:  # stub: laisse passer tous les signaux
        def __init__(self, *a, **kw): pass
        def validate_signal(self, symbol, signal): return True

try:
    from hmm_regime import MarketRegimeHMM
    _HMM_OK = True
except ImportError:
    _HMM_OK = False
    class MarketRegimeHMM:  # stub silencieux
        def detect_regime(self, df, symbol=""): return {"name": "RANGING", "regime": 0, "confidence": 0.5}
        def get_signal_adjustment(self, r, sig): return 0
        @property
        def last_regime_name(self): return "—"

try:
    from equity_curve import EquityCurve
except ImportError:
    class EquityCurve:  # stub silencieux
        def __init__(self, *a, **kw): pass
        def record(self, balance): pass
        def is_below_ma(self, *a): return False
        def format_report(self): return ""
        def generate_chart(self, *a): return b""
        def reset_history(self, keep_last: int = 0): pass  # FIX CR-1: manquait dans le stub
        def total_pnl_pct(self): return 0.0               # FIX CR-1: idem

# ─── Sprint Final — Nouveaux modules ────────────────────────────────────────
try:
    from signal_card import generate_signal_card
    _SIGNAL_CARD_OK = True
except ImportError:
    _SIGNAL_CARD_OK = False
    def generate_signal_card(*a, **kw): return None  # type: ignore

try:
    from lstm_predictor import LSTMPredictor
    _LSTM_OK = True
except ImportError:
    _LSTM_OK = False
    class LSTMPredictor:  # stub : laisse passer tout
        def train(self, df): return False
        def should_enter(self, df): return True, 1.0
        def predict(self, df): return 1.0
        def notify_trade_result(self, won): pass
        @property
        def is_ready(self): return False

try:
    from drl_sizer import DRLPositionSizer
    _DRL_OK = True
except ImportError:
    _DRL_OK = False
    class DRLPositionSizer:  # stub : multiplicateur fixe 1.0
        def get_multiplier(self): return 1.0
        def record_trade(self, *a, **kw): pass
        def summary(self): return "DRL non disponible"

try:
    from ab_tester import ABTester
    _AB_OK = True
except ImportError:
    _AB_OK = False
    class ABTester:  # stub neutre
        def get_variant(self, inst): return "A"
        def get_params(self, v): return {}
        def record_result(self, *a, **kw): pass
        def weekly_report(self): return "AB Tester non disponible"
        def global_winner(self): return "A"


try:
    from tradingview_webhook import get_webhook_server
    _WEBHOOK_OK = bool(os.getenv("WEBHOOK_SECRET"))
except ImportError:
    _WEBHOOK_OK = False

TRAILING_ATR_MULT = 1.5     # Trailing stop à 1.5x ATR après TP2

# ─── Forcer l'export des flags _XXX_OK pour `from .imports import *` ────────
# Python exclut les noms _ du wildcard import → on les re-déclare sans _
WEBHOOK_OK   = _WEBHOOK_OK
DASHBOARD_OK = _DASHBOARD_OK
MORNING_OK   = _MORNING_OK

bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt propre en cours...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

# ─── Module 1 : Rate-Limit Guardian ─────────────────────────────────────────
try:
    from rate_limiter import RateLimiter, Priority as RLPriority, get_rate_limiter
    _RATE_LIMITER_OK = True
except ImportError:
    _RATE_LIMITER_OK = False
    class RateLimiter:  # stub transparent
        def acquire(self, *a, **kw): pass
        def on_429(self, *a, **kw): pass
        def stats(self): return {}
    class RLPriority:
        CRITICAL = 0; HIGH = 1; LOW = 2
    def get_rate_limiter(): return RateLimiter()

# ─── Module 2 : Dynamic Blacklist ────────────────────────────────────────────
try:
    from asset_quarantine import AssetQuarantine
    _QUARANTINE_OK = True
except ImportError:
    _QUARANTINE_OK = False
    class AssetQuarantine:  # stub transparent — laisse passer tout
        def __init__(self, *a, **kw): pass
        def is_quarantined(self, instrument): return False
        def record_result(self, instrument, won): pass
        def refresh_from_db(self): pass
        def get_quarantined(self): return []
        def status_summary(self): return "Quarantine: non disponible"

# ─── Module 3 : EoD Reconciliation ──────────────────────────────────────────
try:
    from eod_reconciliation import EoDReconciliation
    _EOD_OK = True
except ImportError:
    _EOD_OK = False
    class EoDReconciliation:  # stub silencieux
        def __init__(self, *a, **kw): pass
        def run(self): pass

# ─── Moteur 1 : Volatility-Adjusted TP/SL ────────────────────────────────────
try:
    from vol_adjuster import VolAdjuster
    _VOL_ADJ_OK = True
except ImportError:
    _VOL_ADJ_OK = False
    class VolAdjuster:  # stub: retourne les valeurs originales
        def adjust(self, df, entry, sl, tp1, direction, risk_pct, balance, **kw):
            return sl, tp1, None
        def format_status(self, df): return "VolAdj: N/A"

# ─── Moteur 2 : Order Book Imbalance Guard ────────────────────────────────────
try:
    from orderbook_guard import OrderBookGuard
    _OB_GUARD_OK = True
except ImportError:
    _OB_GUARD_OK = False
    class OrderBookGuard:  # stub: fail-open
        def __init__(self, *a, **kw): pass
        def check(self, *a, **kw): return True, "stub"
        def stats(self): return {}

# ─── Moteur 3 : Shadow Trading Engine ────────────────────────────────────────
try:
    from shadow_engine import ShadowEngine
    _SHADOW_OK = True
except ImportError:
    _SHADOW_OK = False
    class ShadowEngine:  # stub silencieux
        def __init__(self, *a, **kw): pass
        def on_signal(self, *a, **kw): pass
        def on_real_trade_closed(self, *a, **kw): pass
        def weekly_report(self): return {}
        def format_telegram_report(self): return ""
        def stop(self): pass

# ─── Audit Quantitatif Go-Live ───────────────────────────────────────────────
try:
    from slippage_injector import SlippageInjector
    _SLIPPAGE_OK = True
except ImportError:
    try:
        from slippage_tracker import SlippageInjector  # Tracker réel (slippage_injector.py supprimé)
        _SLIPPAGE_OK = True
    except ImportError:
        _SLIPPAGE_OK = False
        class SlippageInjector:  # stub transparent (pas de dégradation)
            def __init__(self, *a, **kw): pass
            def apply_market_slippage(self, entry, direction, ob_imbalance=0.5): return entry
            def simulate_limit_fill(self, lp, cp, qty, direction="BUY"): return qty, lp
            def compute_adjusted_pnl(self, pnl, *a, **kw): return pnl
            def format_status(self): return "SlippageInjector: N/A"
            def stats(self): return {}

try:
    from latency_tracker import LatencyTracker
    _LATENCY_OK = True
except ImportError:
    _LATENCY_OK = False
    class LatencyTracker:  # stub no-op
        def __init__(self, *a, **kw): pass
        def measure(self, instrument): return _NoOpCtx()
        def start(self, instrument): return 0.0
        def end(self, *a, **kw): return 0.0
        def get_stats(self, *a, **kw): return {}
        def format_report(self): return ""
        def top_slowest(self, n=5): return []
    class _NoOpCtx:
        def __enter__(self): return self
        def __exit__(self, *_): pass

try:
    from golive_checklist import GoLiveChecker, GO_LIVE_SQL_QUERIES
    _GOLIVE_OK = True
except ImportError:
    _GOLIVE_OK = False
    class GoLiveChecker:
        def __init__(self, *a, **kw): pass
        def run_full_check(self): return {"_ready_for_live": False}
        def send_telegram_report(self): pass
        def get_sql_queries(self): return {}
    GO_LIVE_SQL_QUERIES = {}

# ─── Moteurs Quantitatifs Avancés ────────────────────────────────────────────
try:
    from ml_engine import MLEngine
    _ML_OK = True
except ImportError:
    _ML_OK = False
    class MLEngine:
        def __init__(self, *a, **kw): pass
        def predict(self, *a, **kw): return 0.5
        def retrain(self): return False
        def stats(self): return {}

try:
    from alt_data import AltDataEngine
    _ALT_DATA_OK = True
except ImportError:
    _ALT_DATA_OK = False
    class AltDataEngine:
        def __init__(self, *a, **kw): pass
        def get_sentiment(self, *a, **kw): return 0.0
        def should_block_entry(self, *a, **kw): return False, "stub"
        def get_all_scores(self): return {}
        def format_report(self): return ""
        def stop(self): pass

try:
    from pairs_trader import PairsTrader
    _PAIRS_OK = True
except ImportError:
    _PAIRS_OK = False
    class PairsTrader:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def status(self): return {}

try:
    from smart_router import SmartRouter
    _ROUTER_OK = True
except ImportError:
    _ROUTER_OK = False
    class SmartRouter:
        def __init__(self, *a, **kw): pass
        def execute_twap(self, epic, direction, total_size, **kw): return {}
        def execute_single(self, epic, direction, size, **kw): return None
        def stats(self): return {}

try:
    from health_check import HealthCheck
    _HEALTH_OK = True
except ImportError:
    _HEALTH_OK = False
    class HealthCheck:
        def __init__(self, *a, **kw): pass
        def run(self): return {"all_ok": True, "checks": {}}
        def send_telegram_report(self): pass

try:
    from latency_tracker import LatencyTracker
    _LATENCY_OK = True
except ImportError:
    _LATENCY_OK = False
    class LatencyTracker:
        def __init__(self, *a, **kw): pass
        def measure(self, inst): return _NullCtx()
        def start(self, inst): return 0.0
        def end(self, ts, inst, phase=""): return 0.0
        def get_stats(self, inst=None): return {}
        def format_report(self): return ""
        def top_slowest(self, n=5): return []

class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): pass

try:
    from golive_checklist import GoLiveChecker
    _GOLIVE_OK = True
except ImportError:
    _GOLIVE_OK = False
    class GoLiveChecker:
        def __init__(self, *a, **kw): pass
        def run_full_check(self): return {"_ready_for_live": False}
        def send_telegram_report(self): pass
        def get_sql_queries(self): return {}


# ─── Singularité Algorithmique (Moteurs 8-10) ─────────────────────────────────
try:
    from vpin_guard import VPINGuard
    _VPIN_OK = True
except ImportError:
    _VPIN_OK = False
    class VPINGuard:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def is_toxic(self, *a): return False, 0.0, "SAFE"
        def ensure_table(self): pass
        def get_all_scores(self): return {}
        def status(self): return {}

try:
    from hmm_portfolio import HMMPortfolio, REGIME_BULL, REGIME_RANGE, REGIME_CRISIS
    _HMM_OK = True
except ImportError:
    _HMM_OK = False
    REGIME_BULL = "BULL_LOW_VOL"
    REGIME_RANGE = "RANGE_MID_VOL"
    REGIME_CRISIS = "CRISIS_HIGH_VOL"
    class HMMPortfolio:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_current_regime(self): return REGIME_RANGE
        def get_kelly_multiplier(self, *a): return 1.0
        def get_regime_summary(self): return {"regime": REGIME_RANGE, "kelly_mult": 1.0}
        def format_report(self): return ""

try:
    from rl_agent import RLAgent, ACTION_BUY, ACTION_SELL, ACTION_HOLD
    _RL_OK = True
except ImportError:
    _RL_OK = False
    ACTION_BUY, ACTION_SELL, ACTION_HOLD = 2, 0, 1
    class RLAgent:
        def __init__(self, *a, **kw): pass
        def get_action(self, *a): return ACTION_HOLD, 0.5
        def record_transition(self, *a, **kw): pass
        def compute_reward(self, pnl, *a, **kw): return pnl
        def stats(self): return {}
        def stop(self): pass

# ─── Niveau Apex — Moteurs 11-13 ──────────────────────────────────────────────
try:
    from meta_agent import MetaAgent, Decision
    _META_OK = True
except ImportError:
    _META_OK = False
    class Decision:
        def __init__(self, *a, **kw):
            self.approved = True; self.score = 0.5
            self.size_multiplier = 1.0; self.reason = "stub"
    class MetaAgent:
        def __init__(self, *a, **kw): pass
        def decide(self, *a, **kw): return Decision()
        def record_outcome(self, *a, **kw): pass
        def ensure_table(self): pass
        def format_report(self): return ""
        def stats(self): return {}
        def stop(self): pass

try:
    from memory_pool import MemoryPool, MEMORY_POOL
    _MEMPOOL_OK = True
except ImportError:
    _MEMPOOL_OK = False
    MEMORY_POOL = None
    class MemoryPool:
        @classmethod
        def get_instance(cls): return cls()
        def compute_correlation_fast(self, a, b, **kw): return 0.0
        def compute_covariance(self, m, **kw): return None
        def push_signal(self, *a, **kw): pass
        def stats(self): return {}

try:
    from mev_shield import MEVShield
    _MEV_OK = True
except ImportError:
    _MEV_OK = False
    class MEVShield:
        def __init__(self, *a, **kw): pass
        def get_twap_schedule(self, total_size, base_interval=12, **kw):
            n = 3; s = total_size / n
            return [(s, base_interval)] * n
        def inject_decoy_delay(self): return 0.0
        def record_price(self, *a, **kw): pass
        def detect_frontrun(self, *a, **kw): return False
        def stats(self): return {}
        def format_report(self): return ""

# ─── Leviathan Tier — Moteurs 14-16 ──────────────────────────────────────────
try:
    from spatial_arb import SpatialArbEngine
    _SPATIAL_OK = True
except ImportError:
    _SPATIAL_OK = False
    class SpatialArbEngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def stats(self): return {}

try:
    from market_maker import MarketMaker
    _MM_OK = True
except ImportError:
    _MM_OK = False
    class MarketMaker:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_quotes(self, *a): return {}
        def on_fill(self, *a, **kw): pass
        def stats(self): return {}
        def format_report(self): return ""

try:
    from cluster_manager import ClusterManager
    _CLUSTER_OK = True
except ImportError:
    _CLUSTER_OK = False
    class ClusterManager:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def is_primary(self): return True
        def register_leg(self, *a, **kw): pass
        def close_leg(self, *a): pass
        def cluster_status(self): return {"role": "PRIMARY", "state": "RUNNING"}
        def format_report(self): return ""

# ─── Infrastructure Locale — Moteurs 21-22 ────────────────────────────────────
try:
    from network_resilience import NetworkResilience
    _NETRES_OK = True
except ImportError:
    _NETRES_OK = False
    class NetworkResilience:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        @property
        def is_online(self): return True
        def on_reconnect(self, fn): pass
        def on_disconnect(self, fn): pass
        def retry(self, **kw):
            def d(fn): return fn
            return d
        def safe_call(self, fn, *a, fallback=None, **kw):
            try: return fn(*a, **kw)
            except Exception: return fallback
        def wrap_websocket(self, fn): return fn
        def stats(self): return {}

try:
    from sleep_guard import SleepGuard
    _SLEEP_OK = True
except ImportError:
    _SLEEP_OK = False
    class SleepGuard:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def ensure_table(self): pass
        def stats(self): return {}


# ─── Predator Tier — Moteurs 23-25 ────────────────────────────────────────────
try:
    from onchain_gnn import OnChainGNN
    _ONCHAIN_OK = True
except ImportError:
    _ONCHAIN_OK = False
    class OnChainGNN:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_whale_signal(self, inst): return False, 0.0, "stub"
        def get_all_scores(self): return {}
        def stats(self): return {}
        def format_report(self): return ""

try:
    from algo_hunter import AlgoHunter, SIGNAL_PENNY_BUY, SIGNAL_PENNY_SELL, SIGNAL_NONE as AH_NONE
    _ALGOHUNT_OK = True
except ImportError:
    _ALGOHUNT_OK = False
    SIGNAL_PENNY_BUY = "PENNY_JUMP_BUY"
    SIGNAL_PENNY_SELL = "PENNY_JUMP_SELL"
    AH_NONE = "NONE"
    class AlgoHunter:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_hunt_signal(self, inst): return AH_NONE, 0.0, "stub"
        def detect(self, inst): return None
        def on_tick(self, *a, **kw): pass
        def stats(self): return {}
        def format_report(self): return ""

try:
    from vol_surface import VolSurface, Greeks
    _VOLSURF_OK = True
except ImportError:
    _VOLSURF_OK = False
    class Greeks:
        def __init__(self, *a, **kw):
            self.delta = 0; self.gamma = 0; self.theta = 0
            self.vega = 0; self.rho = 0; self.iv = 0
    class VolSurface:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_greeks(self, inst): return Greeks()
        def get_portfolio_greeks(self): return Greeks()
        def scan_anomalies(self): return []
        def get_delta_neutral_signal(self, inst): return "NONE", 0.0, "no_anomaly"
        def stats(self): return {}
        def format_report(self): return ""

# ─── Olympe Tier — Moteurs 26-28 ─────────────────────────────────────────────
try:
    from macro_nlp import MacroNLP
    _MACRO_OK = True
except ImportError:
    _MACRO_OK = False
    class MacroNLP:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_macro_signal(self, inst): return "NONE", 0.0, ""
        def get_current_sentiment(self): return {"sentiment": 0.0, "label": "NEUTRAL", "events": 0}
        def stats(self): return {}
        def format_report(self): return ""

try:
    from swarm_intel import SwarmIntelligence
    _SWARM_OK = True
except ImportError:
    _SWARM_OK = False
    class SwarmIntelligence:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_swarm_signal(self, inst): return False, "", 0.0
        def get_agent_state(self, inst): return {}
        def broadcast_event(self, *a, **kw): pass
        def stats(self): return {}
        def format_report(self): return ""

try:
    from synthetic_router import SyntheticRouter
    _SYNTH_OK = True
except ImportError:
    _SYNTH_OK = False
    class SyntheticRouter:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_optimal_route(self, f, t): return None
        def get_synthetic_cost(self, pair): return 0, 0, False
        def get_triangular_opportunities(self): return []
        def stats(self): return {}
        def format_report(self): return ""

# ─── God Tier — Moteurs 29-31 ────────────────────────────────────────────────
try:
    from tda_engine import TDAEngine, BettiNumbers, ChaosState
    _TDA_OK = True
except ImportError:
    _TDA_OK = False
    class BettiNumbers:
        def __init__(self, *a, **kw): self.b0=1;self.b1=0;self.b2=0
    class ChaosState:
        def __init__(self, *a, **kw): self.lyapunov=0;self.fractal_dim=1.5;self.hurst=0.5;self.regime="UNKNOWN";self.is_chaotic=False
    class TDAEngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_tda_signal(self, inst): return "NONE", 0.0, "UNKNOWN"
        def get_betti(self, inst): return BettiNumbers()
        def get_chaos(self, inst): return ChaosState()
        def stats(self): return {}
        def format_report(self): return ""

try:
    from flash_loan import FlashLoanEngine
    _FLASH_OK = True
except ImportError:
    _FLASH_OK = False
    class FlashLoanEngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_opportunities(self): return []
        def get_best_opportunity(self): return None
        def stats(self): return {}
        def format_report(self): return ""

try:
    from zerocopy_engine import ZeroCopyEngine
    _ZEROCOPY_OK = True
except ImportError:
    _ZEROCOPY_OK = False
    class ZeroCopyEngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def ingest(self, *a, **kw): pass
        def get_ml_input(self, inst, w=50): return __import__('numpy').empty((0,5))
        def get_pipeline(self, inst): return None
        def stats(self): return {}
        def format_report(self): return ""

# ─── Singularity Tier — Moteurs 32-34 ────────────────────────────────────────
try:
    from quantum_tensor import QuantumTensorEngine
    _QUANTUM_OK = True
except ImportError:
    _QUANTUM_OK = False
    class QuantumTensorEngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_quantum_signal(self, inst): return "NONE", 0.0, "no_wave"
        def get_wave_state(self, inst): return {}
        def stats(self): return {}
        def format_report(self): return ""

try:
    from dark_forest_mev import DarkForestMEV
    _DARKFOREST_OK = True
except ImportError:
    _DARKFOREST_OK = False
    class DarkForestMEV:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def submit_private_tx(self, *a, **kw): return None
        def get_protection_status(self): return {}
        def stats(self): return {}
        def format_report(self): return ""

try:
    from hdc_memory import HDCMemory
    _HDC_OK = True
except ImportError:
    _HDC_OK = False
    class HDCMemory:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_hdc_prediction(self, inst): return "NONE", 0.0
        def store_outcome(self, *a, **kw): pass
        def stats(self): return {}
        def format_report(self): return ""

# ─── Consciousness Tier — Moteurs 35-37 ──────────────────────────────────────
try:
    from ast_mutator import SelfRewritingKernel
    _ASTMUT_OK = True
except ImportError:
    _ASTMUT_OK = False
    class SelfRewritingKernel:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def register_mutable(self, *a, **kw): pass
        def mutate_function(self, *a, **kw): return False
        def rollback(self, *a, **kw): return False
        def stats(self): return {}
        def format_report(self): return ""

try:
    from cfr_engine import CFREngine
    _CFR_OK = True
except ImportError:
    _CFR_OK = False
    class CFREngine:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_nash_action(self, inst): return "HOLD", 0.0, "no_data"
        def get_exploitability(self, inst): return 1.0
        def stats(self): return {}
        def format_report(self): return ""

try:
    from virtual_fpga import VirtualFPGA
    _FPGA_OK = True
except ImportError:
    _FPGA_OK = False
    class VirtualFPGA:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_kernel(self, name): return None
        def get_profiler(self): return None
        def stats(self): return {}
        def format_report(self): return ""

# ─── M38 : Convexity & Dynamic Trailing Stop ────────────────────────────────
try:
    from convexity_engine import ConvexityEngine, MIN_RR_RATIO
    _CONVEXITY_OK = True
except ImportError:
    _CONVEXITY_OK = False
    MIN_RR_RATIO = 1.0
    class ConvexityEngine:
        def __init__(self, *a, **kw): pass
        def validate_rr(self, entry, sl, tp, instrument=""): return True, 3.0
        def compute_atr_sl(self, df, entry, direction, instrument=""): return entry
        def compute_atr_tp(self, entry, sl, direction, min_rr=3.0): return entry
        def enforce_minimum_rr(self, entry, sl, tp, direction, instrument=""): return sl, tp
        def register_trade(self, instrument, entry, sl, direction): pass
        def update_trailing(self, instrument, current_price): return None
        def unregister_trade(self, instrument): pass
        def stats(self): return {}

# ─── M39 : Kelly Criterion Kernel (per-engine) ──────────────────────────────
try:
    from kelly_criterion import KellyCriterionKernel
    _KELLY_KERNEL_OK = True
except ImportError:
    _KELLY_KERNEL_OK = False
    class KellyCriterionKernel:
        def __init__(self, *a, **kw): pass
        def record_engine_result(self, engine_id, won, rr_achieved=1.0): pass
        def get_engine_fraction(self, engine_id): return 0.005
        def compute_position_risk(self, engine_id, base_risk=0.005): return base_risk
        def is_engine_dead(self, engine_id): return False
        def get_engine_health(self): return {}
        def stats(self): return {}

# ─── M40 : Time-Based Capitulation & Dead Capital ───────────────────────────
try:
    from time_stop import DeadCapitalDetector
    _DEAD_CAPITAL_OK = True
except ImportError:
    _DEAD_CAPITAL_OK = False
    class DeadCapitalDetector:
        def __init__(self, *a, **kw): pass
        def check_stagnation(self, instrument, current_price, state, max_hold_min=720): return False, ""
        def get_dead_capital_report(self, trades, prices): return []
        def stats(self): return {}

# ─── M41 : Fast Execution Core (HFT) ────────────────────────────────────────
try:
    from fast_exec import FastExecCore
    _FAST_EXEC_OK = True
except ImportError:
    _FAST_EXEC_OK = False
    class FastExecCore:
        def __init__(self, *a, **kw): pass
        def fast_market_order(self, *a, **kw): return None
        def fast_confirm_deal(self, *a, **kw): return None
        def pre_serialize_order(self, *a, **kw): return b""
        def flush_pre_built(self, *a, **kw): return None
        def warmup(self): pass
        def shutdown(self): pass
        def stats(self): return {}

# ─── M42 : TCP Tuner + uvloop ───────────────────────────────────────────────
try:
    from tcp_tuner import TCPTuner, install_tcp_tuning, install_uvloop
    _TCP_TUNER_OK = True
except ImportError:
    _TCP_TUNER_OK = False
    class TCPTuner:
        def __init__(self, *a, **kw): pass
        def initialize(self): pass
        def tune_ws(self, s): pass
        def tune_session(self, s): pass
        def stats(self): return {}
    def install_tcp_tuning(): pass
    def install_uvloop(): return False

# ─── M43 : Predictive Pre-Builder ───────────────────────────────────────────
try:
    from pre_builder import PreBuilder
    _PRE_BUILDER_OK = True
except ImportError:
    _PRE_BUILDER_OK = False
    class PreBuilder:
        def __init__(self, *a, **kw): pass
        def pre_build(self, *a, **kw): return False
        def get_pre_built(self, *a, **kw): return None
        def consume(self, *a, **kw): return None
        def invalidate(self, *a, **kw): pass
        def has_pre_built(self, *a, **kw): return False
        def stats(self): return {}

# ─── Phase 1.1 : Dead-Man Switch + Watchdog ──────────────────────────────────
try:
    from watchdog import DeadManSwitch
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False
    class DeadManSwitch:
        def __init__(self, *a, **kw): pass
        def ping(self): pass
        def ws_ping(self): pass
        def start(self): pass
        def stop(self): pass
        @property
        def ws_fallback(self): return False
        @property
        def seconds_since_last_tick(self): return 0

# ─── Phase 1.2 : Order Guardian (Confirm + Orphan + Slippage + Margin) ───────
try:
    from order_guardian import OrderGuardian
    _GUARDIAN_OK = True
except ImportError:
    _GUARDIAN_OK = False
    class OrderGuardian:
        def __init__(self, *a, **kw): pass
        def confirm_order(self, *a, **kw): return True
        def scan_orphans(self, *a, **kw): return []
        def log_slippage(self, *a, **kw): pass
        def check_margin(self, *a, **kw): return True
        def get_slippage_stats(self): return {}

# ─── Phase 2 : Portfolio Shield (Advanced Risk) ─────────────────────────────
try:
    from portfolio_shield import PortfolioShield
    _SHIELD_OK = True
except ImportError:
    _SHIELD_OK = False
    class PortfolioShield:
        def __init__(self, *a, **kw): pass
        def check_monthly_dd(self, bal): return False
        def check_correlation(self, inst, trades): return False, ""
        def check_sector_exposure(self, inst, trades, bal): return False, ""
        def compute_atr_trailing_sl(self, price, atr, d, sl=0): return sl
        def adjust_sl_for_regime(self, sl, e, d, r): return sl
        def compute_var(self, pos, bal, **kw): return {"var_amount": 0, "var_pct": 0}
        def should_friday_close(self, inst): return True
        def format_status(self, *a, **kw): return ""

# ─── Phase 3 : ML Retrain Pipeline ──────────────────────────────────────────
try:
    from ml_retrain_pipeline import MLRetrainPipeline
    _ML_PIPELINE_OK = True
except ImportError:
    _ML_PIPELINE_OK = False
    class MLRetrainPipeline:
        def __init__(self, *a, **kw): pass
        def retrain_m52(self, **kw): return {"status": "skip"}
        def retest_pairs(self): return {"status": "skip"}
        def attribute_performance(self): return {"status": "skip"}

# ─── Phase 4 : Health Endpoint ──────────────────────────────────────────────
try:
    from health_endpoint import start_health_server
    _HEALTH_ENDPOINT_OK = True
except ImportError:
    _HEALTH_ENDPOINT_OK = False
    def start_health_server(*a, **kw): return 0

# ─── Tâche 1 : State Sync (Orphan Reconciliation) ──────────────────────────
try:
    from state_sync import StateSync
    _STATE_SYNC_OK = True
except ImportError:
    _STATE_SYNC_OK = False
    class StateSync:
        def __init__(self, *a, **kw): pass
        def reconcile(self, capital_trades): return {"status": "skip"}
        @property
        def stats(self): return {}

# ─── Tâche 2 : Spread Guard (Pre-Trade Filter) ─────────────────────────────
try:
    from spread_guard import SpreadGuard
    _SPREAD_GUARD_OK = True
except ImportError:
    _SPREAD_GUARD_OK = False
    class SpreadGuard:
        def __init__(self, *a, **kw): pass
        def check(self, inst, price=0): return True, 0.0, "stub"
        @property
        def stats(self): return {}

# ─── Project Sentience : Affective Engine ───────────────────────────────────
try:
    from emotional_core import EmotionalCore, Mood
    _SENTIENCE_OK = True
except ImportError:
    _SENTIENCE_OK = False
    class Mood:
        NEUTRAL = "NEUTRAL"; CONFIDENT = "CONFIDENT"; EUPHORIC = "EUPHORIC"
        FEARFUL = "FEARFUL"; PANICKED = "PANICKED"; FRUSTRATED = "FRUSTRATED"
    class EmotionalCore:
        def __init__(self, *a, **kw): pass
        @property
        def current_mood(self): return Mood.NEUTRAL
        @property
        def mood_name(self): return "NEUTRAL"
        @property
        def mood_emoji(self): return "😐"
        @property
        def risk_multiplier(self): return 1.0
        @property
        def threshold_adjustment(self): return 0.0
        @property
        def tp_multiplier(self): return 1.0
        def is_trading_allowed(self, engine=""): return True
        def is_asset_traumatized(self, inst): return False
        def on_trade_result(self, *a, **kw): pass
        def on_balance_update(self, *a, **kw): pass
        def tick(self): pass
        def format_status(self): return ""
        @property
        def stats(self): return {}

# ─── Project Argus : RSS Sensors + NLP Brain ────────────────────────────────
try:
    from argus_sensors import ArgusSensors
    _ARGUS_SENSORS_OK = True
except ImportError:
    _ARGUS_SENSORS_OK = False
    class ArgusSensors:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass
        def get_recent(self, limit=20): return []
        def inject_headline(self, *a, **kw): pass
        @property
        def stats(self): return {}

try:
    from argus_brain import ArgusBrain
    _ARGUS_BRAIN_OK = True
except ImportError:
    _ARGUS_BRAIN_OK = False
    class ArgusBrain:
        def __init__(self, *a, **kw): pass
        def load_model(self): return False
        def analyze(self, h, s=""): return {"sentiment":"neutral","confidence":0,"impact_score":0,"assets":[],"is_impulse":False}
        def get_news_bias(self, inst): return 0.0
        def get_active_impulses(self, inst=""): return []
        def format_status(self): return ""
        @property
        def stats(self): return {}

# ─── Apex Predator: L2 Microstructure + Hedge Manager + MLOps ───────────────
try:
    from l2_microstructure import L2Microstructure
    _L2_OK = True
except ImportError:
    _L2_OK = False
    class L2Microstructure:
        def __init__(self, *a, **kw): pass
        def check_entry(self, inst, d, df=None): return True, "L2 stub"
        def snapshot(self, inst): return {}
        def format_status(self): return ""
        @property
        def stats(self): return {}

try:
    from hedge_manager import HedgeManager
    _HEDGE_OK = True
except ImportError:
    _HEDGE_OK = False
    class HedgeManager:
        def __init__(self, *a, **kw): pass
        def evaluate_hedge(self, *a, **kw): return None
        def execute_hedge(self, h): return None
        def tick(self): pass
        def is_hedged(self, inst): return False
        def format_status(self): return ""
        @property
        def stats(self): return {}

try:
    from mlops_retrainer import MLOpsRetrainer
    _MLOPS_OK = True
except ImportError:
    _MLOPS_OK = False
    class MLOpsRetrainer:
        def __init__(self, *a, **kw): pass
        def start_scheduler(self): pass
        def stop(self): pass
        def run_pipeline(self): return {}
        def format_status(self): return ""
        @property
        def stats(self): return {}

# ─── Project Prometheus: Trade Journal + Shadow Tester + Core ───────────────
try:
    from trade_journal import TradeJournal
except ImportError:
    class TradeJournal:
        def __init__(self, *a, **kw): pass
        def log_close(self, *a, **kw): pass
        def get_losers(self, **kw): return []
        def get_stats(self, **kw): return {"total": 0}
        def format_status(self): return ""
        @property
        def count(self): return 0

try:
    from shadow_tester import ShadowTester
except ImportError:
    class ShadowTester:
        def __init__(self): pass
        def backtest(self, df, params, **kw): return {"sharpe": 0, "total_trades": 0}
        @property
        def stats(self): return {}

try:
    from prometheus_core import PrometheusCore
except ImportError:
    class PrometheusCore:
        def __init__(self, *a, **kw): pass
        def start_nightly(self): pass
        def stop(self): pass
        def run_cycle(self, **kw): return {}
        def format_status(self): return ""
        @property
        def stats(self): return {}

