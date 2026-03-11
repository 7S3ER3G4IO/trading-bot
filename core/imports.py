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
from concurrent.futures import ThreadPoolExecutor


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
