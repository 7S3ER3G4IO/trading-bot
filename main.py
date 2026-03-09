"""
main.py — ⚡ Nemesis v1.0
Multi-Asset | DD Protection | Auto-Hyperopt | ATR Filter | Dashboard Web
"""

import os
import time
import signal
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta, date
from loguru import logger

from logger import setup_logger
from config import SYMBOLS, LOOP_INTERVAL_SECONDS, DAILY_REPORT_HOUR_UTC
from data_fetcher import DataFetcher
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from order_executor import OrderExecutor
from telegram_notifier import TelegramNotifier
from telegram_bot_handler import TelegramBotHandler, InlineKeyboardMarkup
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from market_context import MarketContext
from database import Database
from chart_generator import ChartGenerator
from brokers.capital_client import CapitalClient, CAPITAL_INSTRUMENTS, INSTRUMENT_NAMES as CAPITAL_NAMES, PIP_FACTOR as CAPITAL_PIP
from brokers.binance_futures import BinanceFuturesClient, FUTURES_INSTRUMENTS, INSTRUMENT_NAMES, FUTURES_MIN_SCORE
import telegram_capital as tgc
from telegram_capital import SessionTracker
from capital_websocket import CapitalWebSocket
from concurrent.futures import ThreadPoolExecutor  # import top-level (perf)

# ─── Features avancées 2026 ────────────────────────────────────
try:
    from market_sentiment import MarketSentiment
    _SENTIMENT_OK = True
except ImportError:
    _SENTIMENT_OK = False

try:
    from funding_rate import FundingRateFilter
    _FUNDING_OK = True
except ImportError:
    _FUNDING_OK = False

try:
    from onchain_data import OnChainData
    _ONCHAIN_OK = True
except ImportError:
    _ONCHAIN_OK = False
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
    logger.warning("⚠️  morning_brief non disponible")

try:
    from mtf_filter import MTFFilter
    _MTF_OK = True
except ImportError:
    _MTF_OK = False

try:
    from equity_curve import EquityCurve
    _EQUITY_OK = True
except ImportError:
    _EQUITY_OK = False

try:
    from news_sentiment import NewsSentiment
    _NEWS_OK = True
except ImportError:
    _NEWS_OK = False

try:
    from orderbook_imbalance import OrderBookImbalance
    _OBI_OK = True
except ImportError:
    _OBI_OK = False

try:
    from liquidation_zones import LiquidityZones
    _LIQUIDITY_OK = True
except ImportError:
    _LIQUIDITY_OK = False

try:
    from hmm_regime import MarketRegimeHMM
    _HMM_OK = True
except ImportError:
    _HMM_OK = False

try:
    from tradingview_webhook import get_webhook_server
    # Opt-in : seulement si WEBHOOK_SECRET est défini dans Railway
    _WEBHOOK_OK = bool(os.getenv("WEBHOOK_SECRET"))
except ImportError:
    _WEBHOOK_OK = False

try:
    from protection_model import ProtectionModel
    _PROTECTION_OK = True
except ImportError:
    _PROTECTION_OK = False

try:
    from volatility_regime import VolatilityRegime
    _VOLREG_OK = True
except ImportError:
    _VOLREG_OK = False

try:
    from drift_detector import DriftDetector
    _DRIFT_OK = True
except ImportError:
    _DRIFT_OK = False

try:
    from twap_executor import TWAPExecutor
    _TWAP_OK = True
except ImportError:
    _TWAP_OK = False

BINANCE_FEE_RATE  = 0.001   # 0.1% par ordre
TRAILING_ATR_MULT = 1.5     # Trailing stop à 1.5x ATR après TP2

bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt propre en cours...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ─── TradeState ───────────────────────────────────────────────────────────────

class TradeState:
    def __init__(self, symbol, side, entry, total_amount, sl, tp1, tp2, tp3, be,
                 db_id: int = 0):
        self.symbol        = symbol
        self.side          = side
        self.entry         = entry
        self.total_amount  = total_amount
        self.current_sl    = sl
        self.initial_sl    = sl
        self.tp1           = tp1
        self.tp2           = tp2
        self.tp3           = tp3
        self.be            = be
        self.remaining     = total_amount
        self.tp1_hit       = False
        self.tp2_hit       = False
        self.be_active     = False
        self.trailing_active = False
        self.total_pnl     = 0.0
        self.total_fees    = 0.0
        self.db_id         = db_id
        # Vrais IDs d'ordres Binance (None = surveillance logicielle)
        self.sl_order_id   = None
        self.tp1_order_id  = None
        self.tp2_order_id  = None

    def is_open(self) -> bool:
        return self.remaining > 0.000001

    def fees_for(self, qty: float) -> float:
        """Frais Binance 0.1% × 2 ordres."""
        return round(self.entry * qty * BINANCE_FEE_RATE * 2, 4)


# ─── TradingBot ───────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  ⚡  NEMESIS v1.0 — Multi-Asset + 3 TP + BE")
        logger.info(f"  📊  {' | '.join(SYMBOLS)}")
        logger.info("=" * 60)

        # Modules core
        self.fetcher  = DataFetcher()
        self.strategy = Strategy()
        self.executor = OrderExecutor()
        self.db       = Database()
        self.telegram = TelegramNotifier()
        self.handler  = TelegramBotHandler()
        self.reporter = DailyReporter()
        self.calendar = EconomicCalendar()
        self.context  = MarketContext()
        self.charter  = ChartGenerator()

        # Broker Capital.com (Forex / Gold / Indices / Pétrole)
        self.capital = CapitalClient()
        if self.capital.available:
            logger.info(f"🏦 Capital.com actif — {len(CAPITAL_INSTRUMENTS)} instruments : {', '.join(CAPITAL_INSTRUMENTS)}")
        else:
            logger.info("ℹ️  Capital.com non configuré — en attente des credentials (CAPITAL_API_KEY / EMAIL / PASSWORD)")

        # WebSocket Capital.com — monitoring BE temps réel (<500ms)
        self.capital_ws = CapitalWebSocket(
            capital_client=self.capital,
            on_be_triggered=self._on_ws_be_triggered,
        )
        if self.capital.available:
            self.capital_ws.start()

        # Futures — activé via BINANCE_FUTURES_API_KEY (voir plus bas)
        self.futures = None
        self.futures_trades: dict = {}   # instrument → trade_id
        self.futures_meta:   dict = {}   # instrument → {side, entry, qty, sl, tp}
        self.futures_log:    list = []   # trades fermés [{instrument, pnl, side, ts}]
        self.futures_pnl_total: float = 0.0

        # ─── Solde + Risk + State ──────────────────────────────────────────
        bal = self.fetcher.get_balance()["free"]
        self.risk             = RiskManager(bal)
        self.initial_balance  = bal
        self.trades: Dict[str, Optional[TradeState]] = {s: None for s in SYMBOLS}
        self.capital_trades: Dict[str, Optional[dict]] = {s: None for s in CAPITAL_INSTRUMENTS}
        # Trades Capital.com fermés aujourd'hui (pour le dashboard)
        self._capital_closed_today: list = []
        # Structure par instrument :
        # { "refs": [ref_tp1, ref_tp2, ref_tp3],  # deal references
        #   "entry": float,   "direction": str,
        #   "tp1_hit": bool,  "sl": float  }
        # Suivi sessions pour les résumés
        self._london_tracker = SessionTracker()
        self._ny_tracker     = SessionTracker()
        self._last_dashboard_day: Optional[date] = None  # pour le dashboard quotidien 21h UTC
        self._news_paused    = False  # True quand un événement macro est imminent

        # Log IP publique Railway (utile pour whitelist Binance API)
        try:
            import requests as _rq
            _ip = _rq.get("https://ifconfig.me", timeout=5).text.strip()
            logger.info(f"🌐 IP publique Railway : {_ip}  ← à whitelist sur Binance API")
        except Exception:
            pass

        self.last_reset_day       = datetime.now(timezone.utc).date()
        self.last_report_hour     = -1
        self._manual_pause        = False
        self._news_pause_notified = False
        self._last_wallet_post    = datetime.now(timezone.utc)
        self._pre_alert_sent: Dict[str, Optional[datetime]] = {s: None for s in SYMBOLS}

        # ─── #1 Protection drawdown journalier ─────────────────────────────
        self._daily_start_balance  = bal          # Balance au début du jour
        self._dd_paused            = False        # Pause automatique si DD > seuil
        self.DAILY_DD_LIMIT        = float(os.getenv("DAILY_DD_LIMIT", "3.0"))  # %

        # ─── #4 Auto-Hyperopt hebdomadaire ──────────────────────────────
        self._last_hyperopt_week   = None         # Semaine ISO du dernier Hyperopt

        # ─── Matinale (envoyée 1× par jour à 07h UTC) ────────────────────
        self._last_morning_day     = None         # Date du dernier envoi matinale

        # ─── Features avancées 2026 ──────────────────────────────────────
        self.sentiment  = MarketSentiment()   if _SENTIMENT_OK else None
        self.funding    = FundingRateFilter()  if _FUNDING_OK   else None
        self.onchain    = OnChainData()        if _ONCHAIN_OK   else None
        # WR historique par symbole → Kelly Criterion
        self._wr_history: dict = {s: 0.55 for s in SYMBOLS}
        # DCA : timestamp du dernier renforcement par symbole
        self._dca_ts: dict = {}

        # ─── Batch 3 — Features avancées ────────────────────────────────────
        self.mtf        = MTFFilter()          if _MTF_OK        else None
        self.equity     = EquityCurve(bal)     if _EQUITY_OK     else None
        self.news       = NewsSentiment()      if _NEWS_OK       else None
        self.obi        = OrderBookImbalance() if _OBI_OK        else None
        self.protection = ProtectionModel()    if _PROTECTION_OK else None
        # Circuit breaker WR hebdomadaire
        self._weekly_trades: list = []    # [(ts, win:bool)]
        self._wr_paused     = False       # Pause si WR hebdo < 35%
        # TradingView Webhook
        # TradingView Webhook (opt-in — activer via WEBHOOK_SECRET dans Railway)
        if _WEBHOOK_OK:
            self._webhook = get_webhook_server()
            self._webhook.start()
            logger.info(f"📡 Webhook TradingView actif")
        else:
            self._webhook = None
            logger.debug("ℹ️  Webhook désactivé (WEBHOOK_SECRET non défini)")

        # ─── Batch 4 — Features avancées ────────────────────────────────────
        self.vol_regime      = VolatilityRegime()  if _VOLREG_OK    else None
        self.drift           = DriftDetector()     if _DRIFT_OK     else None
        self.twap            = TWAPExecutor()      if _TWAP_OK      else None
        self.liquidity_zones = LiquidityZones()    if _LIQUIDITY_OK else None
        self.hmm_regime      = MarketRegimeHMM()   if _HMM_OK       else None
        # Futures — activé si BINANCE_FUTURES_API_KEY défini dans Railway
        if os.getenv("BINANCE_FUTURES_API_KEY") and os.getenv("BINANCE_FUTURES_SECRET"):
            try:
                self.futures = BinanceFuturesClient()
                logger.info(f"⚡ Futures Binance actif — {len(FUTURES_INSTRUMENTS)} instruments")
            except Exception as e:
                logger.warning(f"⚠️  Futures init : {e}")
                self.futures = None
        else:
            self.futures = None
            logger.info("ℹ️  Futures désactivé (BINANCE_FUTURES_API_KEY non défini)")
        # DCA pyramiding state : {symbol: {"entry_price", "qty", "ts"}}
        self._dca_positions: dict = {}
        # Benchmark BTC : {date: btc_price}
        self._btc_ref_price: float = 0.0
        self._bot_ref_balance: float = bal
        # Monte Carlo : semaine ISO du dernier envoi
        self._last_monte_carlo_week: int = -1

        # Enregistre les callbacks pour les boutons inline / commandes
        self.handler.register_callbacks(
            get_status   = self._status_text,
            get_trades   = self._trades_text,
            close_trade  = self._force_close,
            force_be     = self._force_be,
            pause        = self._do_pause,
            resume       = self._do_resume,
            send_brief   = self._do_brief,
            send_backtest= self._do_backtest,
        )
        self.handler.start_polling()

        # Reprend les trades ouverts depuis la BDD (survie aux redémarrages)
        self._restore_from_db()

        self.calendar.refresh()
        futures_bal = self.futures.get_balance() if (self.futures and self.futures.available) else 0.0
        # Utilise le solde Capital.com pour le message de démarrage si disponible
        start_bal = self.capital.get_balance() if self.capital.available else bal
        self.telegram.notify_start(start_bal, CAPITAL_INSTRUMENTS, futures_balance=futures_bal)
        logger.info(f"💰 Solde Capital.com : {start_bal:.2f}€ | Futures: {futures_bal:.2f} USDT")

        # ─── #8 Dashboard Web ────────────────────────────────────────────────
        if _DASHBOARD_OK and os.getenv("DASHBOARD_ENABLED", "true").lower() == "true":
            port = start_dashboard()
            logger.info(f"🌐 Dashboard web → http://0.0.0.0:{port}")

        # ─── #7 Corrélation actifs (close prices dernière heure) ─────────────
        self._price_history: Dict[str, list] = {s: [] for s in SYMBOLS}

    def _restore_from_db(self):
        """Restaure les trades ouverts (Binance + Capital.com) après redémarrage."""
        # ── Binance Spot ──
        open_trades = self.db.load_open_trades()
        for t_dict in open_trades:
            symbol = t_dict["symbol"]
            if symbol not in SYMBOLS:
                continue
            try:
                state = TradeState(
                    symbol=symbol, side=t_dict["side"], entry=t_dict["entry"],
                    total_amount=t_dict["amount"], sl=t_dict["sl"],
                    tp1=t_dict["tp1"], tp2=t_dict["tp2"], tp3=t_dict["tp3"],
                    be=t_dict["be"], db_id=t_dict["id"]
                )
                state.current_sl     = t_dict["current_sl"]
                state.remaining      = t_dict["remaining"]
                state.tp1_hit        = bool(t_dict["tp1_hit"])
                state.tp2_hit        = bool(t_dict["tp2_hit"])
                state.be_active      = bool(t_dict["be_active"])
                state.total_pnl      = t_dict["total_pnl"]
                state.sl_order_id    = t_dict.get("sl_order_id")
                state.tp1_order_id   = t_dict.get("tp1_order_id")
                state.tp2_order_id   = t_dict.get("tp2_order_id")
                self.trades[symbol]  = state
                logger.info(f"🔄 Trade Binance restauré : {symbol} {t_dict['side']} @ {t_dict['entry']}")
            except Exception as e:
                logger.error(f"❌ Restauration trade Binance {symbol} : {e}")

        # ── Capital.com CFD ──
        cap_trades = self.db.load_open_capital_trades()
        for t_dict in cap_trades:
            instrument = t_dict["instrument"]
            # Filtre les instruments connus seulement
            if instrument not in CAPITAL_INSTRUMENTS:
                continue
            try:
                self.capital_trades[instrument] = {
                    "refs":      [t_dict.get("ref1"), t_dict.get("ref2"), t_dict.get("ref3")],
                    "entry":     t_dict["entry"],
                    "sl":        t_dict["sl"],
                    "tp1":       t_dict["tp1"],
                    "tp2":       t_dict["tp2"],
                    "tp3":       t_dict["tp3"],
                    "direction": t_dict["direction"],
                    "tp1_hit":   bool(t_dict["tp1_hit"]),
                    "tp2_hit":   bool(t_dict["tp2_hit"]),
                }
                # Relance la surveillance WebSocket
                state = self.capital_trades[instrument]
                self.capital_ws.watch(
                    instrument=instrument,
                    entry=state["entry"],
                    tp1=state["tp1"],
                    tp2=state["tp2"],
                    tp1_ref=state["refs"][0] or "",
                    ref2=state["refs"][1] or "",
                    ref3=state["refs"][2] or "",
                )
                logger.info(f"🔄 Trade Capital.com restauré : {instrument} {t_dict['direction']} @ {t_dict['entry']}")
            except Exception as e:
                logger.error(f"❌ Restauration trade Capital.com {instrument} : {e}")

    def _correlation_ok(self, symbol: str, price: float) -> bool:
        """
        #7 — Filtre de corrélation entre actifs.
        Si 2+ actifs sont corrélés >90% ET ont tous un trade ouvert,
        bloque l'entrée pour éviter de doubler l'exposition.
        """
        # Mémorise les derniers N prix pour chaque actif
        history = self._price_history.get(symbol, [])
        history.append(price)
        if len(history) > 120:  # garde 60 min (120 bougies 30s)
            history.pop(0)
        self._price_history[symbol] = history

        # Seuil : max 2 trades simultanés parmi actifs corrélés
        active = [s for s, t in self.trades.items() if t is not None]
        if len(active) >= 2:
            import numpy as np
            # Si BTC et ETH ont déjà tous les deux un trade ouvert → bloquer les autres
            correlated_pairs = [("BTC/USDT", "ETH/USDT"), ("BTC/USDT", "BNB/USDT")]
            for s1, s2 in correlated_pairs:
                if s1 in active and s2 in active and symbol not in (s1, s2):
                    logger.debug(f"🔗 Corrélation {s1}/{s2} active — exposition max atteinte")
                    return False
        return True

    def _run_auto_hyperopt(self):
        """
        #4 — Lance le Hyperopt Optuna en arrière-plan (thread non-bloquant).
        Exécuté automatiquement chaque lundi à 00h UTC.
        Met à jour symbol_params.json → params rechargés au prochain tick.
        """
        import threading, subprocess, sys
        def _run():
            try:
                self.telegram.send_message(
                    "⚙️ <b>Auto-Hyperopt démarré</b>\n"
                    "Optimisation des paramètres pour la semaine...\n"
                    "⏳ ~60 secondes"
                )
                result = subprocess.run(
                    [sys.executable, "optimizer.py", "--days", "14", "--trials", "80"],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    self.telegram.send_message(
                        "✅ <b>Auto-Hyperopt terminé</b>\n"
                        "Nouveaux paramètres actifs pour la semaine 🎯"
                    )
                    logger.info("✅ Auto-Hyperopt terminé")
                else:
                    logger.error(f"❌ Auto-Hyperopt échec: {result.stderr[:200]}")
            except Exception as e:
                logger.error(f"❌ Auto-Hyperopt erreur: {e}")
        threading.Thread(target=_run, daemon=True).start()

    # ─── Boucle principale ───────────────────────────────────────────────────

    def run(self):
        logger.info(f"⏱  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | CTRL+C pour arrêter\n")
        _err_count = 0  # Compteur d'erreurs consécutives
        while bot_running:
            try:
                self._tick()
                _err_count = 0  # Reset si tick OK
            except Exception as e:
                _err_count += 1
                # Essaie le solde Capital.com en priorité, sinon Binance
                bal = 0.0
                try:
                    if self.capital.available:
                        bal = self.capital.get_balance()
                    else:
                        bal = self.fetcher.get_balance().get("free", 0.0)
                except Exception:
                    pass
                logger.error(f"❌ Erreur boucle #{_err_count} : {e}")
                self.telegram.notify_error(str(e), balance=bal, count=_err_count)
                if _err_count >= 3:
                    self.telegram.notify_crash(str(e), consecutive=_err_count)
            time.sleep(LOOP_INTERVAL_SECONDS)
        logger.info("✅ Bot arrêté.")

    def _tick(self):
        now  = datetime.now(timezone.utc)
        cet  = now + timedelta(hours=1)
        today = now.date()   # en scope partout dans _tick

        # ── Dashboard update ────────────────────────────────────────────────
        try:
            balance = self.fetcher.get_balance()["free"]
            open_trades = []
            for sym, t in self.trades.items():
                if t:
                    try:
                        cur = self.fetcher.get_ticker(sym)
                        pnl = (cur - t.entry) * t.qty * (1 if t.side == "LONG" else -1)
                        open_trades.append({"symbol": sym.replace("/USDT",""), "side": t.side,
                                            "entry": t.entry, "qty": t.qty, "pnl": round(pnl,2)})
                    except Exception:
                        open_trades.append({"symbol": sym.replace("/USDT",""), "side": t.side,
                                            "entry": t.entry, "qty": t.qty, "pnl": 0.0})
            pnl_today = sum(t["pnl"] for t in open_trades)
            wins  = sum(1 for tr in self.reporter.trade_log if tr.get("win")) if hasattr(self.reporter,"trade_log") else 0
            total = len(self.reporter.trade_log) if hasattr(self.reporter,"trade_log") else 0
            wr    = (wins/total*100) if total > 0 else 0.0
            fut_dash_bal = self.futures.get_balance() if (self.futures and self.futures.available) else 0.0
            dash_update(
                balance=balance, initial=self.initial_balance,
                pnl_total=round(balance - self.initial_balance + self.futures_pnl_total, 2),
                pnl_today=round(pnl_today + self.futures_pnl_total, 2),
                trades=open_trades, wr_overall=round(wr,1),
                n_total=total + len(self.futures_log),
                symbols=[s.replace("/USDT","") for s in SYMBOLS],
                paused=self._manual_pause,
                futures_balance=fut_dash_bal,
            )
        except Exception:
            pass

        # Morning Brief
        if self.context.should_send_brief():
            balance = self.fetcher.get_balance()["free"]
            next_news = None
            _, reason = self.calendar.should_pause_trading()
            if reason:
                next_news = reason
            brief = self.context.build_morning_brief(balance, next_news)
            self.telegram.notify_morning_brief(brief)
            self.context.mark_brief_sent()

        # Bilan journalier
        if now.hour == DAILY_REPORT_HOUR_UTC and self.last_report_hour != now.hour:
            if self.reporter.should_send_report():
                report_lines = self.reporter.build_report_lines()
                date_str = datetime.now(timezone.utc).strftime("%d/%m")
                self.telegram.notify_daily_report(report_lines, date_str)
                self.reporter.mark_report_sent()
            # Rapport Futures du jour
            if self.futures_log:
                wins  = sum(1 for t in self.futures_log if t.get("win"))
                total_f = len(self.futures_log)
                pnl_f = sum(t["pnl"] for t in self.futures_log)
                wr_f  = wins / total_f * 100 if total_f > 0 else 0
                def _fline(t):
                    e = "✅" if t["win"] else "❌"
                    sym = t["instrument"].replace("/USDT:USDT", "")
                    return f"{e} {sym} {t['side']}  {t['pnl']:+.2f}$\n"
                lines = "".join(_fline(t) for t in self.futures_log)
                self.telegram.send_message(
                    f"\U0001f7e3 <b>BILAN FUTURES — {date_str}</b>\n"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"{lines}"
                    f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                    f"Trades : <b>{wins}/{total_f}</b>  WR : <b>{wr_f:.0f}%</b>\n"
                    f"PnL Futures : <b>{pnl_f:+.2f} USDT</b>"
                )
            self.last_report_hour = now.hour

        # Bilan hebdomadaire (dimanche 22h CET)
        if self.reporter.should_send_weekly():
            self.telegram.notify_weekly_report(self.reporter.build_weekly_report())
            self.reporter.mark_weekly_sent()

        # Reset journalier
        if cet.date() != self.last_reset_day:
            bal = self.fetcher.get_balance()["free"]
            self.risk.reset_daily(bal)
            self.reporter.reset_for_new_day()
            self.initial_balance      = bal
            self._daily_start_balance = bal
            self.futures_log          = []   # reset bilan futures journalier
            self.futures_pnl_total    = 0.0
            self._dd_paused           = False  # Reprend le trading au bout du jour
            self.last_reset_day       = cet.date()
            logger.info(f"📊 Nouveau jour — balance de début : {bal:.2f} USDT")

        # ── #1 Protection Drawdown Journalier ────────────────────────────
        # Utilise le solde Capital.com si disponible, sinon Binance
        try:
            if self.capital.available:
                current_bal = self.capital.get_balance()
            else:
                current_bal = self.fetcher.get_balance()["free"]
            if self._daily_start_balance > 0:
                daily_dd_pct = (self._daily_start_balance - current_bal) / self._daily_start_balance * 100
                if daily_dd_pct >= self.DAILY_DD_LIMIT and not self._dd_paused:
                    self._dd_paused = True
                    self.telegram.send_message(
                        f"⛔ <b>Protection DD Journalier</b>\n"
                        f"Perte du jour : <code>{daily_dd_pct:.1f}%</code> (seuil : {self.DAILY_DD_LIMIT}%)\n"
                        f"Trading suspendu jusqu'à minuit UTC. 🛌"
                    )
                    logger.warning(f"⛔ DD journalier {daily_dd_pct:.1f}% ≥ {self.DAILY_DD_LIMIT}% — pause auto")
        except Exception:
            pass

        if self._dd_paused:
            return   # Aucun nouveau trade jusqu'à minuit

        # ── #4 Auto-Hyperopt hebdomadaire (chaque lundi 00h-01h UTC) ─────────
        current_week = now.isocalendar()[1]  # N° de semaine ISO
        if (now.weekday() == 0 and now.hour == 0 and   # Lundi minuit UTC
                self._last_hyperopt_week != current_week):
            self._last_hyperopt_week = current_week
            self._run_auto_hyperopt()

        # ── Matinale 07h UTC (session London) ────────────────────────────
        if (now.hour == 7 and now.minute == 0 and
                self._last_morning_day != today and _MORNING_OK):
            self._last_morning_day = today
            import threading
            threading.Thread(
                target=lambda: generate_morning_brief(CAPITAL_INSTRUMENTS, self.telegram),
                daemon=True,
                name="morning-brief"
            ).start()
            logger.info("☀️  Matinale lancée en arrière-plan")

        # ── Monte Carlo hebdomadaire (dimanche 22h UTC) ───────────────────────
        current_week = now.isocalendar()[1]
        if (now.weekday() == 6 and now.hour == 22 and now.minute == 0 and
                self._last_monte_carlo_week != current_week):
            self._last_monte_carlo_week = current_week
            import threading
            threading.Thread(
                target=self._do_monte_carlo, daemon=True, name="monte-carlo"
            ).start()
            logger.info("🎲 Monte Carlo hebdo lancé en arrière-plan")

        # ── Résumés de session + Dashboard quotidien ─────────────────────────
        # London close recap : 11h00 UTC
        if now.hour == 11 and now.minute == 0:
            try:
                cap_bal = self.capital.get_balance() if self.capital.available else 0.0
                self._london_tracker.send_session_recap("London", cap_bal)
            except Exception as _e:
                logger.error(f"❌ London recap: {_e}")

        # NY close recap : 17h00 UTC
        if now.hour == 17 and now.minute == 0:
            try:
                cap_bal = self.capital.get_balance() if self.capital.available else 0.0
                self._ny_tracker.send_session_recap("NY", cap_bal)
            except Exception as _e:
                logger.error(f"❌ NY recap: {_e}")

        # Dashboard quotidien : 21h00 UTC (22h Paris)
        if (now.hour == 21 and now.minute == 0 and
                self._last_dashboard_day != today):
            self._last_dashboard_day = today
            try:
                cap_bal    = self.capital.get_balance() if self.capital.available else 0.0
                day_trades = getattr(self, "_capital_closed_today", [])
                tgc.send_daily_dashboard(
                    balance=cap_bal,
                    initial_balance=self.initial_balance,
                    day_trades=day_trades,
                    win_rate_instrument={},
                )
                self._capital_closed_today = []   # reset pour le lendemain
            except Exception as _e:
                logger.error(f"❌ Daily dashboard: {_e}")

        # Calendrier économique
        pause, reason = self.calendar.should_pause_trading()
        if pause:
            if not self._news_pause_notified:
                self.telegram.notify_news_pause(reason, 30)
                self._news_pause_notified = True
            logger.warning(f"⏸  Pause news — {reason}")
            return
        self._news_pause_notified = False

        # Pause manuelle
        if self._manual_pause or self.handler.is_paused():
            logger.info("⏸  Bot en pause manuelle")
            return

        # Mode Futures Demo uniquement — balance = solde Futures
        if self.futures and self.futures.available:
            balance = self.futures.get_balance()
        else:
            balance = self.fetcher.get_balance()["free"]

        logger.info(
            f"[{now.strftime('%H:%M:%S')}] "
            f"{'🟣 Futures' if (self.futures and self.futures.available) else '💰 Spot'} "
            f"{balance:.2f} USDT | Trades={sum(1 for t in self.futures_trades.values() if t)}/{len(FUTURES_INSTRUMENTS)}"
        )

        # Wallet stats auto-post toutes les 30 min dans le groupe dédié
        wallet_interval = timedelta(minutes=30)
        if now - self._last_wallet_post >= wallet_interval:
            self._post_wallet_stats(balance)
            self._last_wallet_post = now

        # Refresh Fear & Greed une fois par tick
        self.context.refresh_fear_greed()

        # ─── Loop Spot Binance (désactivé — Futures Demo uniquement) ─────────
        # Pour réactiver : retirer le commentaire ci-dessous
        # for symbol in SYMBOLS:
        #     try:
        #         self._process_symbol(symbol, balance)
        #     except Exception as e:
        #         logger.error(f"❌ {symbol} : {e}")

        # ─── Loop Capital.com — Breakout London/NY Open (actif principal) ─────────
        if self.capital.available and not self._manual_pause:
            capital_balance = self.capital.get_balance()
            for instrument in CAPITAL_INSTRUMENTS:
                try:
                    self._process_capital_symbol(instrument, capital_balance)
                except Exception as e:
                    logger.error(f"❌ Capital.com {instrument} : {e}")
            # Monitoring Break-Even (TP1 touché → SL déplacé à l'entrée)
            try:
                self._monitor_capital_positions()
            except Exception as e:
                logger.error(f"❌ Capital.com monitor BE : {e}")

        # ─── Boucle Binance Futures (désactivée — stratégie Capital.com uniquement) ─
        # Pour réactiver : décommenter le bloc ci-dessous
        # if self.futures and self.futures.available and not self._manual_pause:
        #     try:
        #         fut_free  = self.futures.get_balance()
        #         fut_total = self.futures.get_total_balance()
        #         n_max = len(FUTURES_INSTRUMENTS)
        #         per_instrument_budget = fut_total / n_max if fut_total > 0 else 0.0
        #     except Exception:
        #         fut_free = fut_total = per_instrument_budget = 0.0
        #     for instrument in FUTURES_INSTRUMENTS:
        #         try:
        #             self._process_futures_symbol(instrument, per_instrument_budget, fut_free)
        #         except Exception as e:
        #             logger.error(f"❌ Futures {instrument} : {e}")

    def _process_futures_symbol(self, instrument: str, balance: float, free_balance: float = 0.0):
        """
        Analyse un instrument Binance Futures (LONG & SHORT).
        balance      = budget fixe par instrument (total / nb instruments)
        free_balance = solde libre restant (pour vérifier la marge disponible)
        Endpoint : demo-fapi.binance.com (5000 USDT virtuels).
        Session : 24h/7j (crypto — aucune restriction horaire).
        """
        # ── A. Position déjà ouverte ? Vérifier si toujours active côté Binance ──
        if self.futures_trades.get(instrument):
            pos = self.futures.get_position(instrument)
            if abs(pos["positionAmt"]) > 0.0001:
                return  # Toujours ouverte — rien à faire

            # Position fermée (SL ou TP touché côté Binance)
            meta      = self.futures_meta.pop(instrument, {})
            pnl       = self.futures.get_last_realized_pnl(instrument, limit=5)
            close_px  = float(self.futures.fetch_ohlcv(instrument, count=1).iloc[-1]["close"]) \
                        if meta else pos.get("entryPrice", 0)
            entry_px  = meta.get("entry", pos.get("entryPrice", 0))
            side      = meta.get("side", "BUY")

            # Mise à jour PnL cumulé
            self.futures_pnl_total += pnl

            # Log pour rapport journalier
            from datetime import datetime, timezone as tz
            self.futures_log.append({
                "instrument": instrument,
                "side":       side,
                "pnl":        pnl,
                "ts":         datetime.now(tz.utc).strftime("%H:%M"),
                "win":        pnl >= 0,
            })

            # Notification Telegram
            try:
                self.telegram.notify_futures_closed(
                    instrument=instrument, side=side,
                    pnl=pnl, entry=entry_px, close_price=close_px,
                )
            except Exception:
                pass

            logger.info(f"📊 Futures {instrument} fermé — PnL: {pnl:+.2f} USDT")
            del self.futures_trades[instrument]
            return  # Réouverture possible au prochain tick

        # Pas assez de marge libre pour ouvrir ce trade
        min_free_required = balance * 0.5  # au moins 50% du budget-instrument doit être libre
        if free_balance > 0 and free_balance < min_free_required:
            logger.debug(f"⏳ Futures {instrument} : marge libre insuffisante ({free_balance:.0f} < {min_free_required:.0f} USDT)")
            return

        # ── B. Pas de position ouverte — chercher un signal ───────────────────
        # Données OHLCV depuis Binance Futures Demo (5m)
        df = self.futures.fetch_ohlcv(instrument, timeframe="5m", count=300)
        if df is None or len(df) < 100:
            return

        # Indicateurs + signal
        df = self.strategy.compute_indicators(df)
        sig, score, confirmations = self.strategy.get_signal(
            df, symbol=instrument,
            min_score_override=FUTURES_MIN_SCORE,
            futures_mode=True,   # 24h/7j, LONG+SHORT dans tout régime
        )

        if sig == "HOLD":
            return

        entry = float(df.iloc[-1]["close"])
        atr   = self.strategy.get_atr(df)
        if atr <= 0:
            return

        # SL = 0.8 × ATR (tight pour scalping), TP1 = 2×, TP2 intégré via SL→BE
        sl_dist = atr * 0.8
        sl  = entry - sl_dist if sig == "BUY" else entry + sl_dist
        tp  = entry + sl_dist * 2.0 if sig == "BUY" else entry - sl_dist * 2.0

        # Taille de position via position_size_qty du futures client
        qty = self.futures.position_size_qty(
            balance=balance, risk_pct=0.01,
            entry=entry, sl=sl, instrument=instrument,
        )
        if qty <= 0:
            return

        trade_id = self.futures.place_market_order(
            instrument=instrument,
            side=sig,
            qty=qty,
            sl_price=sl,
            tp_price=tp,
        )

        if trade_id:
            self.futures_trades[instrument] = trade_id
            self.futures_meta[instrument]   = {
                "side": sig, "entry": entry, "qty": qty, "sl": sl, "tp": tp,
            }
            name    = INSTRUMENT_NAMES.get(instrument, instrument.replace(":USDT", ""))
            sl_pct  = abs(sl - entry) / entry * 100
            tp_pct  = abs(tp - entry) / entry * 100
            if sig == "BUY":
                msg = (
                    f"🟢 <b>FUTURES LONG — {name}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📍 Entrée : <code>{entry:.4f} USDT</code>\n"
                    f"🎯 TP     : <code>{tp:.4f}</code>  (+{tp_pct:.1f}%)\n"
                    f"🛑 SL     : <code>{sl:.4f}</code>  (-{sl_pct:.1f}%)\n"
                    f"📐 R:R    : <b>1:2</b>  |  Score : {score}/7\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 Qté : {qty:.4f}  |  ID : {trade_id}\n"
                    f"🏦 <i>Binance Futures Demo — 0 vrai argent</i>"
                )
            else:
                msg = (
                    f"🔴 <b>FUTURES SHORT — {name}</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📍 Entrée : <code>{entry:.4f} USDT</code>\n"
                    f"🎯 TP     : <code>{tp:.4f}</code>  (-{tp_pct:.1f}%)\n"
                    f"🛑 SL     : <code>{sl:.4f}</code>  (+{sl_pct:.1f}%)\n"
                    f"📐 R:R    : <b>1:2</b>  |  Score : {score}/7\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💼 Qté : {qty:.4f}  |  ID : {trade_id}\n"
                    f"🏦 <i>Binance Futures Demo — 0 vrai argent</i>"
                )
            self.telegram.send_message(msg)

    def _process_capital_symbol(self, instrument: str, balance: float):
        """
        Analyse un instrument Capital.com avec la stratégie London/NY Open Breakout.
        Ouvre 3 positions (taille/3) avec 3 niveaux de TP :
          TP1 = range × 0.8   (sortie rapide + déclencheur BE)
          TP2 = range × 1.8   (objectif principal)
          TP3 = range × 3.0   (laisser courir)
        """
        state = self.capital_trades.get(instrument)

        # Trade déjà ouvert — on ne re-entre pas
        if state is not None:
            return

        # Données 5m
        df = self.capital.fetch_ohlcv(instrument, timeframe="5m", count=300)
        if df is None or len(df) < 50:
            return

        df  = self.strategy.compute_indicators(df)
        sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument)
        if sig == "HOLD":
            return

        entry    = float(df.iloc[-1]["close"])
        sr       = self.strategy.compute_session_range(df)
        direction = "BUY" if sig == "BUY" else "SELL"
        rng      = sr["size"]

        if rng <= 0 or sr["pct"] < 0.08:
            return

        # SL commun (autre extrémité du range + buffer 10%)
        if sig == "BUY":
            sl    = sr["low"]  - rng * 0.1
            tp1   = entry + rng * 0.8
            tp2   = entry + rng * 1.8
            tp3   = entry + rng * 3.0
        else:
            sl    = sr["high"] + rng * 0.1
            tp1   = entry - rng * 0.8
            tp2   = entry - rng * 1.8
            tp3   = entry - rng * 3.0

        # Taille totale puis split en 3
        total_size = self.capital.position_size(
            balance=balance, risk_pct=0.01, entry=entry, sl=sl, epic=instrument
        )
        min_sz = CapitalClient.MIN_SIZE.get(instrument.upper(), 0.01)
        size1 = max(min_sz, round(total_size / 3, 2))

        if size1 <= 0:
            return

        # ─── ÉTAPE 1 : Ordres en parallèle (toutes les 3 positions simultanément) ───
        def _place(tp):
            return self.capital.place_market_order(instrument, direction, size1, sl, tp)

        with ThreadPoolExecutor(max_workers=3) as pool:
            f1 = pool.submit(_place, tp1)
            f2 = pool.submit(_place, tp2)
            f3 = pool.submit(_place, tp3)
            ref1 = f1.result()
            ref2 = f2.result()
            ref3 = f3.result()

        if not any([ref1, ref2, ref3]):
            return

        # ─── ÉTAPE 2 : WebSocket — monitoring BE temps réel (à la milliseconde) ───
        self.capital_ws.watch(
            instrument=instrument,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            tp1_ref=ref1 or "",
            ref2=ref2 or "",
            ref3=ref3 or "",
        )

        # ─── ÉTAPE 3 : Sauvegarde état ──────────────────────────────────────
        self.capital_trades[instrument] = {
            "refs":      [ref1, ref2, ref3],
            "entry":     entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "tp3":       tp3,
            "direction": direction,
            "tp1_hit":   False,
            "tp2_hit":   False,
        }
        # Session tracker (pour résumé London/NY)
        name    = CAPITAL_NAMES.get(instrument, instrument)
        hour    = datetime.now(timezone.utc).hour
        minute_ = datetime.now(timezone.utc).minute
        # London : 08h-10h UTC | NY : 13h30-16h UTC
        tracker = self._london_tracker if (hour < 13 or (hour == 13 and minute_ < 30)) else self._ny_tracker
        tracker.record_entry(name=name, sig=sig, entry=entry, size=size1)

        # Persiste immédiatement en BDD (survit aux redémarrages Railway mid-trade)
        try:
            self.db.save_capital_trade(instrument, self.capital_trades[instrument])
        except Exception as exc:
            logger.warning(f"⚠️ DB save_capital_trade open: {exc}")

        logger.info(f"✅ Capital.com {sig} {instrument} @ {entry:.5f} | SL={sl:.5f} TP1={tp1:.5f} TP2={tp2:.5f} TP3={tp3:.5f}")

        # ─── ÉTAPE 4 : Telegram en background (ne bloque pas la boucle) ────────
        name    = CAPITAL_NAMES.get(instrument, instrument)
        # Session London : 08h-10h UTC | NY : 13h30-16h UTC
        hour = datetime.now(timezone.utc).hour
        minute = datetime.now(timezone.utc).minute
        session = "London" if (hour < 13 or (hour == 13 and minute < 30)) else "NY"

        import threading
        _snap = dict(instrument=instrument, name=name, sig=sig,
                     entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                     size=size1, score=score, session=session,
                     range_pct=sr["pct"], range_high=sr["high"], range_low=sr["low"],
                     confirmations=list(confirmations), df=df.copy())  # .copy() évite race condition
        threading.Thread(target=lambda: tgc.notify_capital_entry(**_snap), daemon=True).start()

    def _on_ws_be_triggered(self, instrument: str, entry_or_sl: float, event: str = "TP1"):
        """
        Callback WebSocket — appelé en < 500ms quand TP1 ou TP2 est franchi.
        event="TP1" : SL pos2+pos3 → entrée (BE)
        event="TP2" : SL pos3 → TP1 (trailing lock-in)
        Met à jour l'état interne et envoie la notification Telegram.
        """
        state = self.capital_trades.get(instrument)
        if state is None:
            return

        name = CAPITAL_NAMES.get(instrument, instrument)
        pip  = CAPITAL_PIP.get(instrument, 0.0001)

        if event == "TP1":
            state["tp1_hit"] = True
            rng = abs(state["entry"] - state["sl"])
            pips_tp1 = round(rng * 0.8 / pip)
            logger.info(f"⚡ WS BE instant — {instrument} @ {entry_or_sl:.5f}")
            tgc.notify_tp1_be(
                name=name, instrument=instrument,
                entry=entry_or_sl, pips_tp1=pips_tp1, size=0,
            )
        elif event == "TP2":
            state["tp2_hit"] = True   # ← FIX : évite double-trigger
            pips_tp2 = round(abs(state["entry"] - entry_or_sl) / pip)
            logger.info(f"⚡ WS TP2 trailing activé — {instrument} SL pos3 → {entry_or_sl:.5f}")
            self.telegram.send_message(
                f"🎯 <b>TP2 touché — {name}</b>\n"
                f"SL pos3 déplacé à TP1 (<code>{entry_or_sl:.5f}</code>)\n"
                f"🟢 Gains TP1 verrouillés sur position 3 !"
            )

    def _monitor_capital_positions(self):
        """
        Surveille les positions ouvertes Capital.com.
        Quand TP1 est touché (position 1 fermée) :
          → Déplace le SL des positions 2 et 3 au niveau d'entrée (Break-Even).
        Quand toutes les positions sont fermées → réinitialise l'état.
        """
        if not self.capital.available:
            return

        open_pos  = self.capital.get_open_positions()
        # Capital.com GET /positions retourne dealId dans l'objet position
        open_refs = {
            p.get("position", {}).get("dealId")
            for p in open_pos
            if p.get("position", {}).get("dealId")
        }

        for instrument, state in list(self.capital_trades.items()):
            if state is None:
                continue

            refs     = state["refs"]         # [ref1, ref2, ref3]
            entry    = state["entry"]
            tp1_hit  = state["tp1_hit"]

            ref1_open = refs[0] in open_refs if refs[0] else False
            ref2_open = refs[1] in open_refs if refs[1] else False
            ref3_open = refs[2] in open_refs if refs[2] else False

            # TP1 touché si ref1 a disparu des positions ouvertes
            # Note : si le WebSocket est actif, il gère déjà le BE.
            # Ce polling est le fallback si le WS est déconnecté.
            if not tp1_hit and refs[0] and not ref1_open:
                state["tp1_hit"] = True
                name = CAPITAL_NAMES.get(instrument, instrument)
                pip  = CAPITAL_PIP.get(instrument, 0.0001)
                pips_tp1 = round(abs(entry - state["sl"]) / pip * 0.8)
                logger.info(f"🎯 [POLL FALLBACK] TP1 touché {instrument} — activation Break-Even")

                for ref in [refs[1], refs[2]]:
                    if ref and ref in open_refs:
                        self.capital.modify_position_stop(ref, entry)

                # Persiste tp1_hit dans la BDD
                try:
                    self.db.save_capital_trade(instrument, state)
                except Exception:
                    pass

                tgc.notify_tp1_be(name=name, instrument=instrument,
                                   entry=entry, pips_tp1=pips_tp1, size=0)

            # Toutes les positions fermées → reset + unwatch WS
            if not ref1_open and not ref2_open and not ref3_open:
                logger.info(f"✅ Capital.com {instrument} — toutes positions fermées")
                # Enregistre pour le dashboard quotidien
                name_close = CAPITAL_NAMES.get(instrument, instrument)
                pip_close  = CAPITAL_PIP.get(instrument, 0.0001)
                try:
                    current  = self.capital.get_current_price(instrument)
                    close_px = current["mid"] if current else entry
                    pnl_est  = (close_px - entry) * (1 if state["direction"] == "BUY" else -1)
                    pips_pnl = round(pnl_est / pip_close)
                    result   = "WIN" if pnl_est > 0 else "LOSS"
                    self._capital_closed_today.append({
                        "instrument": instrument,
                        "pnl": round(pnl_est * 3, 4),
                        "direction": state["direction"],
                    })
                    # Session tracker — résumé London/NY
                    h_close = datetime.now(timezone.utc).hour
                    m_close = datetime.now(timezone.utc).minute
                    tracker_close = self._london_tracker if (h_close < 13 or (h_close == 13 and m_close < 30)) else self._ny_tracker
                    tracker_close.record_close(name=name_close, pnl=pnl_est * 3, result=result)
                except Exception:
                    pass
                # Persiste la fermeture en BDD
                try:
                    self.db.close_capital_trade(instrument)
                except Exception:
                    pass
                self.capital_ws.unwatch(instrument)
                self.capital_trades[instrument] = None





    def _process_symbol(self, symbol: str, balance: float):
        trade = self.trades.get(symbol)

        if trade and trade.is_open():
            ticker = self.fetcher.get_ticker(symbol)
            self._monitor_trade(trade, ticker["last"], balance)
            return

        if not self.risk.can_open_trade(balance):
            return

        df = self.fetcher.get_ohlcv(symbol=symbol)
        df = self.strategy.compute_indicators(df)

        sig, score, confirmations = self.strategy.get_signal(df)

        # ── HMM Régime de marché ────────────────────────────────────────────
        if sig != SIGNAL_HOLD and _HMM_OK and hasattr(self, "hmm_regime"):
            try:
                regime_result = self.hmm_regime.detect_regime(df, symbol)
                hmm_adj       = self.hmm_regime.get_signal_adjustment(regime_result, sig)
                score        += hmm_adj
                regime_name   = regime_result["name"]
                if hmm_adj != 0:
                    adj_txt = f"+1 HMM {regime_name}" if hmm_adj > 0 else f"-1 HMM {regime_name}"
                    confirmations.append(adj_txt)
                    logger.debug(f"🧠 HMM {symbol}: {regime_name} adj={hmm_adj:+d}")
                dash_filter("regime", regime_name)
            except Exception:
                pass

        # ── Liquidity Zones ────────────────────────────────────────────────
        if sig != SIGNAL_HOLD and _LIQUIDITY_OK and hasattr(self, "liquidity_zones"):
            try:
                current_price = self.fetcher.get_ticker(symbol)["last"]
                liq = self.liquidity_zones.analyze(symbol, sig, current_price)
                if not liq["valid"]:
                    logger.info(f"💧 Liquidity block {symbol}: {liq['message']}")
                    return   # Trade bloqué par une muraille d'ordres
                score += liq["score_bonus"]
                if liq["score_bonus"] > 0:
                    confirmations.append(f"+1 Liquidité cible")
            except Exception:
                pass

        # ═ PRE-ALERT : score = 4/6 mais pas encore signal ═
        PRE_ALERT_SCORE   = 4
        PRE_ALERT_COOLDOWN = timedelta(hours=2)
        now_utc = datetime.now(timezone.utc)

        if sig == SIGNAL_HOLD:
            last_pre = self._pre_alert_sent.get(symbol)
            if score >= PRE_ALERT_SCORE:
                # Envoie le pre-alert si cooldown écoulé
                if last_pre is None or (now_utc - last_pre) >= PRE_ALERT_COOLDOWN:
                    ticker_price = self.fetcher.get_ticker(symbol)["last"]
                    # Direction via régime de marché (déjà calculé dans df)
                    regime    = self.strategy.market_regime(df)
                    direction = "BUY" if regime == "BULL" else "SELL"
                    self.telegram.notify_pre_signal(direction, symbol, ticker_price, score)
                    self._pre_alert_sent[symbol] = now_utc
                    logger.info(f"⏳ Pre-alert {symbol} | score={score}/6 | {regime}")

            elif last_pre is not None and score < PRE_ALERT_SCORE:
                # Setup qui était en formation mais qui s'est annulé
                if (now_utc - last_pre) < PRE_ALERT_COOLDOWN:
                    self.telegram.notify_setup_cancelled(symbol)
                    self._pre_alert_sent[symbol] = None
                    logger.info(f"❌ Setup annulé {symbol}")
            return

        ticker = self.fetcher.get_ticker(symbol)
        price  = ticker["last"]
        atr    = self.strategy.get_atr(df)
        levels = self.risk.calculate_levels(price, atr, sig)

        # ── #1 Fear & Greed Filter ──────────────────────────────────
        sentiment_scale = 1.0
        if self.sentiment:
            fg = self.sentiment.get_fear_greed()
            dash_filter("fear_greed", f"{fg['emoji']} {fg['value']}/100")
            if sig == "BUY" and not self.sentiment.should_allow_long():
                logger.warning(f"🚨 {symbol} LONG bloqué par Fear&Greed ({fg['value']}/100)")
                return
            if sig == "SELL" and not self.sentiment.should_allow_short():
                logger.warning(f"🚨 {symbol} SHORT bloqué par Fear&Greed ({fg['value']}/100)")
                return
            # Bonus +1 si SELL en Extreme Fear (trend baissier confirmé)
            fg_bonus = self.sentiment.extreme_fear_bonus(sig)
            if fg_bonus > 0:
                score += fg_bonus
                confirmations.append(f"+1 Extreme Fear (trend SELL)")
                logger.info(f"😱 Bonus F&G SELL {symbol} : score → {score}")
            sentiment_scale = self.sentiment.position_scale()

        # ── #2 Funding Rate Filter ───────────────────────────────
        if self.funding:
            if sig == "BUY" and not self.funding.should_allow_long(symbol):
                logger.warning(f"💸 {symbol} LONG bloqué par Funding Rate")
                return
            if sig == "SELL" and not self.funding.should_allow_short(symbol):
                logger.warning(f"💸 {symbol} SHORT bloqué par Funding Rate")
                return

        # ── #8 Protection Model (losing streak blacklist) ────────
        if self.protection and self.protection.is_blocked(symbol):
            return

        # ── #1 MTF Confluence Filter ─────────────────────────────
        if self.mtf and not self.mtf.validate_signal(symbol, sig):
            return

        # ── #6 Order Book Imbalance filter ───────────────────────
        if self.obi and not self.obi.confirms_signal(symbol, sig):
            logger.warning(f"📗 OBI {symbol} : orderbook contraire au signal")
            return

        # ── #7 News Sentiment Filter ──────────────────────────────
        if self.news and not self.news.should_allow_trade(sig):
            return

        # ── #6 Kelly Criterion Sizing ──────────────────────────────
        wr_hist  = self._wr_history.get(symbol, 0.55)
        rr_ratio = levels.get("sl_distance", 1)
        if rr_ratio and levels["sl"] and price:
            rr_ratio = abs(levels["tp1"] - price) / max(abs(levels["sl"] - price), 0.0001)

        amount = self.risk.kelly_position_size(
            balance, price, levels["sl"],
            win_rate=wr_hist,
            rr_ratio=max(1.0, rr_ratio),
            sentiment_scale=combined_scale,
        )
        if amount <= 0:
            return

        order = self._open_order(symbol, sig, amount)
        if not order:
            return

        t = TradeState(
            symbol=symbol, side=sig, entry=price, total_amount=amount,
            sl=levels["sl"], tp1=levels["tp1"], tp2=levels["tp2"],
            tp3=levels["tp3"], be=levels["be"],
        )
        # Sauvegarde en BDD
        t.db_id = self.db.save_trade_open(t)
        self.trades[symbol] = t
        self.risk.on_trade_opened()

        # ── #8 DCA Pyramiding : mise à jour si position gagnante ──────────
        # (la prochaine fois qu'on reçoit un signal sur ce même actif déjà ouvert
        #  avec le même sens → renforcer de 50%)

        # ══ VRAIS ORDRES BINANCE ══
        frac = round(amount / 3, 5)
        # Stop-Loss sur toute la position restante
        t.sl_order_id = self.executor.place_stop_loss(symbol, sig, amount, levels["sl"])
        # TP1 et TP2 : ordres LIMIT pour 1/3 de la position chacun
        t.tp1_order_id = self.executor.place_take_profit(symbol, sig, frac, levels["tp1"])
        t.tp2_order_id = self.executor.place_take_profit(symbol, sig, frac, levels["tp2"])
        # TP3 : trailing stop logiciel (dynamique, pas d'ordre fixe)

        # Persistance des IDs d'ordres
        self.db.update_trade(t.db_id,
            sl_order_id=t.sl_order_id,
            tp1_order_id=t.tp1_order_id,
            tp2_order_id=t.tp2_order_id,
        )

        # Clavier inline
        from telegram_bot_handler import TelegramBotHandler
        keyboard = TelegramBotHandler.trade_keyboard(symbol)

        # Contexte macro
        ctx_line = self.context.get_context_line()

        # Notification texte avec boutons
        self.telegram.notify_trade_open(
            side=sig, symbol=symbol, entry=price,
            tp1=levels["tp1"], tp2=levels["tp2"],
            tp3=levels["tp3"], sl=levels["sl"],
            amount=amount, balance=balance,
            score=score, confirmations=confirmations,
            context_line=ctx_line,
            markup=keyboard,
        )

        # Chart
        try:
            score_desc = f"ADX+RSI+EMA {score}/6"
            chart = self.charter.generate_trade_chart(
                df=df, symbol=symbol, side=sig,
                entry=price, tp1=levels["tp1"], tp2=levels["tp2"],
                tp3=levels["tp3"], sl=levels["sl"],
                score=score, indicators_desc=score_desc,
            )
            if chart:
                pair   = symbol.replace("/", "")
                action = "ACHAT" if sig == SIGNAL_BUY else "VENTE"
                self.telegram.send_photo(
                    chart,
                    f"📊 *{pair} {action}* — Score `{score}/6`",
                    markup=keyboard,
                )
        except Exception as e:
            logger.warning(f"⚠️  Chart : {e}")

    def _monitor_trade(self, t: TradeState, price: float, balance: float):
        buy = t.side == SIGNAL_BUY
        keyboard = TelegramBotHandler.trade_keyboard(t.symbol)

        def hit_up(target):   return price >= target if buy else price <= target
        def hit_down(target): return price <= target if buy else price >= target

        # Trailing Stop (après TP2)
        if t.trailing_active:
            atr_approx = abs(t.tp1 - t.entry)  # approx
            new_sl     = price - TRAILING_ATR_MULT * atr_approx if buy \
                         else price + TRAILING_ATR_MULT * atr_approx
            if (buy and new_sl > t.current_sl) or (not buy and new_sl < t.current_sl):
                old_sl = t.current_sl
                t.current_sl = new_sl
                self.db.update_trade(t.db_id, current_sl=new_sl)
                self.telegram.notify_trailing_stop_update(t.symbol, old_sl, new_sl)

        # TP1
        if not t.tp1_hit and hit_up(t.tp1):
            qty      = round(t.total_amount / 3, 5)
            fees     = t.fees_for(qty)
            pnl_g    = abs(t.tp1 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            t.remaining  -= qty
            t.tp1_hit     = True
            t.current_sl  = t.be
            t.be_active   = True

            # L'ordre TP1 LIMIT a été exécuté par Binance — pas de close_partial nécessaire
            # si TP1 order_id existe (ordre Binance fillé automatiquement)
            if not t.tp1_order_id:
                self._close_partial(t.symbol, t.side, qty)   # fallback logiciel

            # ═ Remplacer le SL par un vrai ordre Break Even sur Binance ═
            new_sl_id = self.executor.replace_stop_loss(
                t.symbol, t.side, t.sl_order_id, t.remaining, t.be
            )
            t.sl_order_id = new_sl_id

            self.db.update_trade(t.db_id,
                tp1_hit=1, be_active=1, current_sl=t.current_sl,
                remaining=t.remaining, total_pnl=t.total_pnl,
                sl_order_id=t.sl_order_id,
            )
            self.telegram.notify_tp_hit(
                1, t.symbol, price, t.entry, pnl_g, fees,
                balance, t.remaining, be_activated=True, markup=keyboard
            )
            self.reporter.record_trade(
                t.symbol, t.side, "TP1", pnl_g, t.entry, price, qty
            )
            logger.info(f"🎯 {t.symbol} TP1 | SL→BE={t.be:.2f} (ordre Binance placé)")
            return

        # TP2
        if t.tp1_hit and not t.tp2_hit and hit_up(t.tp2):
            qty      = round(t.total_amount / 3, 5)
            fees     = t.fees_for(qty)
            pnl_g    = abs(t.tp2 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            t.remaining  -= qty
            t.tp2_hit     = True
            t.trailing_active = True

            # TP2 LIMIT Binance fillé automatiquement si ordre existait
            if not t.tp2_order_id:
                self._close_partial(t.symbol, t.side, qty)  # fallback logiciel

            self.db.update_trade(t.db_id,
                tp2_hit=1, remaining=t.remaining, total_pnl=t.total_pnl
            )
            self.telegram.notify_tp_hit(
                2, t.symbol, price, t.entry, pnl_g, fees,
                balance, t.remaining, markup=keyboard
            )
            self.reporter.record_trade(
                t.symbol, t.side, "TP2", pnl_g, t.entry, price, qty
            )
            logger.info(f"🎯 {t.symbol} TP2 | Trailing Stop activé")
            return

        # TP3 → ferme tout (Trailing Stop ou prix atteint)
        if t.tp1_hit and t.tp2_hit and hit_up(t.tp3):
            qty   = t.remaining
            fees  = t.fees_for(qty)
            pnl_g = abs(t.tp3 - t.entry) * qty
            t.total_pnl  += pnl_g
            t.total_fees += fees
            self._close_partial(t.symbol, t.side, qty)
            self.telegram.notify_tp3_closed(
                t.symbol, price, t.entry, pnl_g, fees, balance
            )
            self._finalize_trade(t, price, "TP3 MAX PROFIT", balance)
            return

        # SL / BE
        if hit_down(t.current_sl):
            qty   = t.remaining
            fees  = t.fees_for(qty)
            pnl_g = (abs(t.current_sl - t.entry) * qty * (-1 if not t.be_active else 0))
            t.total_pnl  += pnl_g
            t.total_fees += fees

            # Annule tous les ordres Binance restants (TP3 limit s'il existe, etc.)
            self.executor.cancel_all_orders(t.symbol)

            # Si pas de vrai ordre SL (surveillance logicielle), close manuellement
            if not t.sl_order_id:
                self._close_partial(t.symbol, t.side, qty)

            self.telegram.notify_sl_hit(
                t.symbol, price, t.entry, t.be_active, pnl_g, fees, balance
            )
            label = "Break Even" if t.be_active else "Stop-Loss"
            self._finalize_trade(t, price, label, balance)

    def _post_wallet_stats(self, balance: float):
        """Poste les stats wallet toutes les 30 min dans le groupe dédié."""
        try:
            open_trades = []
            for sym, t in self.trades.items():
                if t:
                    ticker = self.fetcher.get_ticker(sym)
                    price  = ticker["last"]
                    pnl    = (price - t.entry) * t.remaining * (1 if t.side == "BUY" else -1)
                    open_trades.append({"symbol": sym, "side": t.side, "pnl": pnl})

            daily_pnl  = sum(tr.pnl_net for tr in self.reporter._trades)
            total_pnl  = balance - self.initial_balance
            nb_trades  = len(self.reporter._trades)
            wins       = sum(1 for tr in self.reporter._trades if tr.result != "SL")
            win_rate   = (wins / nb_trades * 100) if nb_trades > 0 else 0

            self.telegram.post_wallet_stats(
                balance=balance,
                initial_balance=self.initial_balance,
                open_trades=open_trades,
                daily_pnl=daily_pnl,
                total_pnl=total_pnl,
                win_rate=win_rate,
                nb_trades=nb_trades,
            )
        except Exception as e:
            logger.warning(f"⚠️  Wallet stats : {e}")

    def _finalize_trade(self, t: TradeState, exit_price: float, reason: str, balance: float):

        result   = "BE" if "BE" in reason else ("TP3" if "TP3" in reason else "SL")
        net      = t.total_pnl - t.total_fees
        day_summ = f"PnL net du jour estimé : {net:+.2f} USDT"

        self.db.close_trade(t.db_id, exit_price, result, t.total_pnl, t.total_fees)
        self.reporter.record_trade(
            t.symbol, t.side, result, t.total_pnl,
            t.entry, exit_price, t.remaining
        )
        # notify_trade_closed appelé uniquement pour les clôtures inattendues (MANUAL, SL)
        # TP3 et BE ont déjà leur propre notification via notify_tp3_closed / notify_sl_hit
        if result not in ("TP3 MAX PROFIT", "BE"):
            self.telegram.notify_trade_closed(
                t.symbol, reason, t.total_pnl, t.total_fees,
                balance, self.initial_balance,
                t.entry, exit_price, ""
            )
        self._end_trade(t.symbol)

    # ─── Ordres ──────────────────────────────────────────────────────────────

    def _open_order(self, symbol: str, side: str, amount: float):
        if side == SIGNAL_BUY:
            return self.executor.buy_market(symbol, amount)
        base = symbol.split("/")[0]
        held = self.executor.get_position(base)
        qty  = min(amount, held)
        if qty > 0.00001:
            return self.executor.sell_market(symbol, qty)
        logger.warning(f"⚠️  {symbol} SELL — pas de {base}")
        return None

    def _close_partial(self, symbol: str, side: str, qty: float):
        qty = round(qty, 5)
        if qty <= 0:
            return
        if side == SIGNAL_BUY:
            self.executor.sell_market(symbol, qty)
        else:
            self.executor.buy_market(symbol, qty)

    def _end_trade(self, symbol: str):
        self.trades[symbol] = None
        self.risk.on_trade_closed()

    # ─── Callbacks boutons inline ─────────────────────────────────────────────

    def _force_close(self, symbol: str) -> str:
        """Ferme un trade : Capital.com (epic) ou Binance Spot."""
        # ─── Capital.com : symbol = epic (GOLD, EURUSD...) ───
        epic = symbol.upper().replace("/USDT", "").replace(":USDT", "")
        state = self.capital_trades.get(epic) or self.capital_trades.get(symbol)
        if state is not None:
            # Vérifie les positions réellement ouvertes (guard None refs)
            if not self.capital.available:
                return f"❌ Capital.com non disponible"
            open_pos  = self.capital.get_open_positions()
            open_refs = {p.get("position", {}).get("dealId") for p in open_pos if p.get("position", {}).get("dealId")}
            closed = 0
            for ref in state.get("refs", []):
                if ref and ref in open_refs and self.capital.close_position(ref):
                    closed += 1
            self.capital_ws.unwatch(epic)
            self.capital_trades[epic] = None
            try:
                self.db.close_capital_trade(epic)
            except Exception:
                pass
            return (
                f"🔴 <b>Capital.com {epic} fermé</b>\n"
                f"{closed}/3 positions clôturées manuellement."
            )

        # ─── Binance Spot fallback ───
        if not symbol.endswith("/USDT"):
            symbol = f"{symbol}/USDT"
        t = self.trades.get(symbol)
        if not t:
            return f"❌ Pas de trade actif sur <code>{symbol}</code>"
        ticker = self.fetcher.get_ticker(symbol)
        price  = ticker["last"]
        self._close_partial(symbol, t.side, t.remaining)
        pnl_g = abs(price - t.entry) * t.remaining * (1 if (t.side=="BUY") == (price > t.entry) else -1)
        fees  = t.fees_for(t.remaining)
        self.db.close_trade(t.db_id, price, "MANUAL", pnl_g, fees)
        self.reporter.record_trade(symbol, t.side, "MANUAL", pnl_g, t.entry, price, t.remaining)
        self._end_trade(symbol)
        return (
            f"🔴 <b>Trade {symbol} fermé manuellement</b>\n"
            f"💰 PnL net : <b>{pnl_g - fees:+.2f} USDT</b>"
        )

    def _force_be(self, symbol: str) -> str:
        """Force le Break-Even : Capital.com (epic) ou Binance Spot."""
        # ─── Capital.com ───
        epic  = symbol.upper().replace("/USDT", "").replace(":USDT", "")
        state = self.capital_trades.get(epic) or self.capital_trades.get(symbol)
        if state is not None:
            entry = state["entry"]
            moved = 0
            for ref in state.get("refs", [])[1:]:  # pos 2 et 3
                if ref and self.capital.modify_position_stop(ref, entry):
                    moved += 1
            state["tp1_hit"] = True
            # Persiste tp1_hit en BDD pour survivre au redémarrage Railway
            try:
                self.db.save_capital_trade(epic, state)
            except Exception:
                pass
            return (
                f"🟡 <b>BE forcé — {epic}</b>\n"
                f"SL déplacé à l'entrée : <code>{entry:.5f}</code>\n"
                f"{moved}/2 positions modifiées."
            )

        # ─── Binance Spot fallback ───
        if not symbol.endswith("/USDT"):
            symbol = f"{symbol}/USDT"
        t = self.trades.get(symbol)
        if not t:
            return f"❌ Pas de trade actif sur <code>{symbol}</code>"
        t.current_sl = t.be
        t.be_active  = True
        self.db.update_trade(t.db_id, current_sl=t.be, be_active=1)
        return (
            f"🔒 <b>Break-Even forcé — {symbol}</b>\n"
            f"SL déplacé à <code>{t.be:,.2f}</code> (entrée)"
        )

    def _do_pause(self):
        self._manual_pause = True
        logger.info("⏸️  Bot mis en pause manuellement")

    def _do_resume(self):
        self._manual_pause = False
        logger.info("▶️  Bot repris manuellement")

    def _do_brief(self):
        """Envoie le Morning Brief immédiatement (commande /brief Telegram)."""
        try:
            if _MORNING_OK:
                generate_morning_brief(CAPITAL_INSTRUMENTS, self.telegram)
            else:
                self.telegram._send("⚠️ morning_brief module non disponible.")
        except Exception as e:
            logger.error(f"❌ _do_brief: {e}")
            self.telegram._send(f"❌ Erreur Morning Brief : {e}")

    def _do_backtest(self, symbol: str = "ETH/USDT", days: int = 30):
        """Lance un backtest et envoie le rapport sur Telegram (commande /backtest)."""
        try:
            from backtester import run_backtest, format_telegram_report
            logger.info(f"🧪 Backtest {symbol} {days}j lancé...")
            trades, final_bal = run_backtest(symbol, days, risk=0.01)
            report = format_telegram_report(trades, final_bal, symbol, days)
            self.telegram._send(report)
        except Exception as e:
            logger.error(f"❌ _do_backtest: {e}")
            self.telegram._send(f"❌ Erreur Backtest : {e}")

    def _do_monte_carlo(self):
        """Lance Monte Carlo sur les PnLs récents et envoie le résumé Telegram."""
        try:
            from monte_carlo import get_trade_pnls, run_monte_carlo, generate_chart
            pnls = get_trade_pnls("ETH/USDT", days=30)
            if len(pnls) < 5:
                self.telegram._send("📊 <b>Monte Carlo</b>\n<code>Pas assez de trades (< 5) pour simuler.</code>")
                return
            results = run_monte_carlo(pnls, n_trials=1000)
            p5  = results.get("p5_return", 0)
            p50 = results.get("p50_return", 0)
            p95 = results.get("p95_return", 0)
            msg = (
                "🎲 <b>Monte Carlo — 1000 simulations</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 Scénario pessimiste (P5)  : <b>{p5:+.1f}%</b>\n"
                f"📊 Scénario médian    (P50)  : <b>{p50:+.1f}%</b>\n"
                f"📈 Scénario optimiste (P95)  : <b>{p95:+.1f}%</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Basé sur les 30 derniers jours de backtest</i>"
            )
            self.telegram._send(msg)
            try:
                chart = generate_chart(results, "ETH/USDT")
                self.telegram._send_photo(chart, caption="📊 Distribution Monte Carlo")
            except Exception:
                pass
        except Exception as e:
            logger.error(f"❌ _do_monte_carlo: {e}")

    def _status_text(self) -> str:
        # Balance Capital.com en priorité
        if self.capital.available:
            balance = self.capital.get_balance()
            bal_str = f"{balance:,.2f}€  (Capital.com)"
        else:
            balance = self.fetcher.get_balance()["free"]
            bal_str = f"{balance:,.2f} USDT"

        paused = "⏸️ PAUSED" if (self._manual_pause or self.handler.is_paused()) else "🟢 ACTIF"

        # Positions Capital.com
        cap_lines = ""
        cap_open  = 0
        for epic, state in self.capital_trades.items():
            if state is None:
                continue
            cap_open += 1
            name = CAPITAL_NAMES.get(epic, epic)
            tp1_icon = "✅" if state.get("tp1_hit") else "○"
            cap_lines += f"  • <b>{name}</b> {state.get('direction','?')}  TP1{tp1_icon}\n"

        ctx = self.context.get_context_line() if hasattr(self.context, 'get_context_line') else ""
        return (
            f"⚡ <b>NEMESIS — Statut</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Capital : <b>{bal_str}</b>\n"
            f"📊 Positions Capital.com : <b>{cap_open}/{len(CAPITAL_INSTRUMENTS)}</b>\n"
            f"{cap_lines}"
            f"🤖 État : {paused}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{ctx}"
        )

    def _trades_text(self):
        """Retourne le texte + markup des positions Capital.com actives."""
        lines, markup_epic = [], None

        for epic, state in self.capital_trades.items():
            if state is None:
                continue
            name     = CAPITAL_NAMES.get(epic, epic)
            entry    = state.get("entry", 0)
            direction = state.get("direction", "?")
            tp1_icon  = "✅" if state.get("tp1_hit") else "○"

            # Prix courant via REST (lent mais fiable)
            price_data = self.capital.get_current_price(epic)
            if price_data:
                price   = price_data["mid"]
                pip     = CAPITAL_PIP.get(epic, 0.0001)
                pnl_pips = round((price - entry) / pip) if direction == "BUY" else round((entry - price) / pip)
                price_line = f"\n  📍 Prix : <code>{price:.5f}</code>  ({pnl_pips:+.0f} pips)"
            else:
                price_line = ""

            lines.append(
                f"<b>{name}</b> {direction} TP1{tp1_icon}\n"
                f"  📍 Entrée : <code>{entry:.5f}</code>{price_line}\n"
                f"  🛑 SL : <code>{state.get('sl', 0):.5f}</code>"
            )
            markup_epic = epic

        # Fallback Binance Spot si pas de positions Capital.com
        if not lines:
            for sym, t in self.trades.items():
                if not t:
                    continue
                ticker = self.fetcher.get_ticker(sym)
                price  = ticker["last"]
                pnl    = (price - t.entry) * t.remaining * (1 if t.side=="BUY" else -1)
                lines.append(
                    f"<b>{sym}</b> {t.side}\n"
                    f"  PnL≈{pnl:+.2f} | SL: <code>{t.current_sl:,.2f}</code>"
                )

        if not lines:
            return "📋 <b>Aucune position ouverte.</b>", None

        text   = "📋 <b>Positions actives :</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
        markup = TelegramBotHandler.trade_keyboard(markup_epic) if markup_epic else None
        return text, markup


if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
