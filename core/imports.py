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
from telegram_notifier import TelegramNotifier
from telegram_bot_handler import TelegramBotHandler
try:
    from telegram import InlineKeyboardMarkup
except ImportError:
    InlineKeyboardMarkup = None
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from market_context import MarketContext
from database import Database
from brokers.capital_client import (
    CapitalClient, CAPITAL_INSTRUMENTS,
    INSTRUMENT_NAMES as CAPITAL_NAMES,
    PIP_FACTOR as CAPITAL_PIP,
    ASSET_PROFILES,
    MICRO_TF_PROFILES,
    PRICE_DECIMALS,
)
import telegram_capital as tgc
from telegram_capital import SessionTracker
from capital_websocket import CapitalWebSocket
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







