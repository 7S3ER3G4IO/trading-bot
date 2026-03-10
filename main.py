"""
main.py — ⚡ Nemesis v2.0 — Capital.com CFD | London/NY Breakout
"""

import os
import time
import signal
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta, date
from loguru import logger

from logger import setup_logger
from config import LOOP_INTERVAL_SECONDS, DAILY_REPORT_HOUR_UTC, SESSION_HOURS, MAX_OPEN_TRADES
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from telegram_notifier import TelegramNotifier
from telegram_bot_handler import TelegramBotHandler, InlineKeyboardMarkup
from daily_reporter import DailyReporter
from economic_calendar import EconomicCalendar
from market_context import MarketContext
from database import Database
from brokers.capital_client import (
    CapitalClient, CAPITAL_INSTRUMENTS,
    INSTRUMENT_NAMES as CAPITAL_NAMES,
    PIP_FACTOR as CAPITAL_PIP,
)
import telegram_capital as tgc
from telegram_capital import SessionTracker
from capital_websocket import CapitalWebSocket
from concurrent.futures import ThreadPoolExecutor  # placement ordres en parallèle


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

bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt propre en cours...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)




# ─── TradingBot ───────────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  ⚡  NEMESIS v2.0 — Capital.com CFD | London/NY Breakout")
        logger.info(f"  📊  {' | '.join(CAPITAL_INSTRUMENTS)}")
        logger.info("=" * 60)

        # ─── Modules core ─────────────────────────────────────────────────
        self.strategy = Strategy()
        self.db       = Database()
        self.telegram = TelegramNotifier()
        self.handler  = TelegramBotHandler()
        self.reporter = DailyReporter()
        self.calendar = EconomicCalendar()
        self.context  = MarketContext()

        # ─── Broker Capital.com ───────────────────────────────────────────
        self.capital = CapitalClient()
        if self.capital.available:
            logger.info(f"🏦 Capital.com actif — {len(CAPITAL_INSTRUMENTS)} instruments : {', '.join(CAPITAL_INSTRUMENTS)}")
        else:
            logger.info("ℹ️  Capital.com non configuré — vérifier CAPITAL_API_KEY / EMAIL / PASSWORD dans Railway")

        # ─── WebSocket Capital.com — BE temps réel (<500ms) ──────────────
        self.capital_ws = CapitalWebSocket(
            capital_client=self.capital,
            on_be_triggered=self._on_ws_be_triggered,
        )
        if self.capital.available:
            self.capital_ws.start()
            # Feature R : Enregistre le callback de prix temps réel (<1s trigger)
            self.capital_ws.register_signal_callback(self._on_ws_price_tick)


        # ─── Solde initial ────────────────────────────────────────────────
        # Sleep 3s : laisse le fallback Capital.com s'établir après 429
        time.sleep(3)
        bal = self.capital.get_balance() if self.capital.available else 0.0
        if bal == 0.0 and self.capital.available:
            time.sleep(2)  # 2e tentative si session encore en cours d'auth
            bal = self.capital.get_balance() or 0.0
        # Si solde DEMO = 0 (compte non initialisé) → fallback 10 000€
        self.risk                 = RiskManager(max(bal, 10_000.0))
        self.initial_balance      = bal or 10_000.0
        self._daily_start_balance = self.initial_balance
        self._dd_paused           = False
        self.DAILY_DD_LIMIT       = float(os.getenv("DAILY_DD_LIMIT", "3.0"))
        # ── Drawdown Mensuel (circuit breaker long terme) ─────────────────────
        self._monthly_start_balance = self.initial_balance
        self._monthly_dd_paused     = False
        self._last_reset_month      = datetime.now(timezone.utc).month
        # ── Historique equity pour Chart.js ───────────────────────────────
        self._equity_history: list  = [
            {"t": datetime.now(timezone.utc).strftime("%H:%M"), "v": self.initial_balance}
        ]
        self._bot_start_time        = datetime.now(timezone.utc)

        # ─── État Capital.com ─────────────────────────────────────────────
        self.capital_trades: Dict[str, Optional[dict]] = {s: None for s in CAPITAL_INSTRUMENTS}
        self._capital_closed_today: list = []
        self._london_tracker = SessionTracker()
        self._ny_tracker     = SessionTracker()
        self._last_dashboard_day: Optional[date] = None
        # Retest Entry : breakouts en attente de re-test du niveau cassé
        # Format : { instrument: { sig, retest_level, atr, score, confirmations,
        #                          detected_at (datetime), ticks_waited } }
        self._pending_retest: Dict[str, Optional[dict]] = {s: None for s in CAPITAL_INSTRUMENTS}


        # ─── État général ─────────────────────────────────────────────────
        self.last_report_hour      = -1  # réservé
        self._last_reset_day       = datetime.now(timezone.utc).date()
        self._manual_pause         = False
        self._news_paused          = False
        self._news_pause_notified  = False
        self._last_wallet_post     = datetime.now(timezone.utc)
        self._last_hyperopt_week   = None
        self._last_morning_day     = None
        self._last_session_push    = ""    # "London" ou "NY" pour éviter double envoi
        self._last_heartbeat_push  = datetime.now(timezone.utc)  # heartbeat toutes les 30min
        self._last_no_signal_alert = datetime.now(timezone.utc)  # alerte "aucun signal" / 10min
        # ── Sprint 4 — Auto-optimisation & Backup ──────────────────────
        self._last_backup_time    = datetime.now(timezone.utc)   # backup Supabase
        self._drift_size_reduced  = False   # flag réduction taille post-drift
        self._drift_reduced_until: Optional[datetime] = None

        # ── Sprint 5 — Heatmap & Rapport journalier ─────────────────────
        # Heatmap : {instrument: {hour_utc: [pnl1, pnl2, ...]}}
        self._heatmap_data: dict = {inst: {} for inst in CAPITAL_INSTRUMENTS}
        self._last_daily_report_day: Optional[date] = None

        # ── Drift Detector + Protection + MTF + Equity + HMM Regime ────────────
        self.drift      = DriftDetector()
        self.protection = ProtectionModel()  # Blacklist auto après 3 SL consécutifs
        self.mtf        = MTFFilter(capital_client=self.capital)  # Filtre 1h/4h
        self.hmm        = MarketRegimeHMM()  # Détecteur de régime HMM (TREND/RANGING)
        try:
            self.equity = EquityCurve(initial_balance=self.initial_balance or 10_000.0)
        except Exception as _e:
            logger.warning(f"⚠️ EquityCurve init échoué ({_e}) — réinitialisation propre.")
            self.equity = EquityCurve(initial_balance=self.initial_balance or 10_000.0, history_file=None)

        # ── Sprint Final — Modules IA/ML/AB ──────────────────────────────────────
        self.lstm    = LSTMPredictor()       # Feature P : Timing prédictif
        self.drl     = DRLPositionSizer()    # Feature T : Sizing adaptatif
        self.ab      = ABTester()            # Feature U : A/B Testing stratégie


        # BUG FIX #C : Le refresh calendrier se fait en thread daemon (non bloquant)
        self.calendar.start_background_refresh()

        # ─── TradingView Webhook (opt-in) ─────────────────────────────────
        if _WEBHOOK_OK:
            self._webhook = get_webhook_server()
            self._webhook.start()
            logger.info("📡 Webhook TradingView actif")
        else:
            self._webhook = None

        # ─── Log IP Railway ───────────────────────────────────────────────
        try:
            import requests as _rq
            _ip = _rq.get("https://ifconfig.me", timeout=5).text.strip()
            logger.info(f"🌐 IP publique Railway : {_ip}")
        except Exception:
            pass

        # ─── Callbacks Telegram ───────────────────────────────────────────
        self.handler.register_callbacks(
            get_status   = self._status_text,
            get_trades   = self._trades_text,
            close_trade  = self._force_close,
            force_be     = self._force_be,
            pause        = self._do_pause,
            resume       = self._do_resume,
            send_brief   = self._do_brief,
            send_backtest= self._do_backtest,
            # Sprint 3 — commandes premium
            get_best_pair= self._cmd_best_pair,
            get_risk     = self._cmd_risk,
            get_regime   = self._cmd_regime,
        )

        self.handler.start_polling()

        # ─── Restauration BDD ─────────────────────────────────────────────
        self._restore_from_db()

        self.calendar.refresh()
        start_bal = self.capital.get_balance() if self.capital.available else 0.0
        self.telegram.notify_start(start_bal, CAPITAL_INSTRUMENTS)
        logger.info(f"💰 Solde initial Capital.com : {start_bal:.2f}€")

        # ─── Dashboard Web ────────────────────────────────────────────────
        if _DASHBOARD_OK and os.getenv("DASHBOARD_ENABLED", "true").lower() == "true":
            port = start_dashboard()
            logger.info(f"🌐 Dashboard web → http://0.0.0.0:{port}")


    def _restore_from_db(self):
        """Restaure les trades Capital.com ouverts après redémarrage."""
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

    # BUG FIX #N : _correlation_ok() supprimé — méthode morte jamais appelée.
    # La limite de 2 trades simultanés est déjà gérée dans _tick() ligne 345–347.

    # ─── Boucle principale ───────────────────────────────────────────────────

    def run(self):
        logger.info(f"⏱  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | CTRL+C pour arrêter\n")
        _err_count = 0
        while bot_running:
            try:
                self._tick()
                _err_count = 0
            except Exception as e:
                _err_count += 1
                bal = 0.0
                try:
                    bal = self.capital.get_balance() if self.capital.available else 0.0
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
        today = now.date()

        try:
            balance = self.capital.get_balance() if self.capital.available else 0.0
            open_trades = []
            for instr, state in self.capital_trades.items():
                if state is None:
                    continue
                name  = CAPITAL_NAMES.get(instr, instr)
                entry = state.get("entry", 0.0)
                # PnL non-réalisé en temps réel (prix actuel vs entrée)
                unrealized_pnl = 0.0
                try:
                    px = self.capital.get_current_price(instr)
                    if px:
                        mid = px["mid"]
                        direction = state.get("direction", "BUY")
                        unrealized_pnl = round((mid - entry) * (1 if direction == "BUY" else -1) * 3, 2)
                except Exception:
                    pass
                open_trades.append({
                    "symbol": name,
                    "side":   state.get("direction", ""),
                    "entry":  entry,
                    "qty":    1,
                    "pnl":    unrealized_pnl,  # PnL live, mis à jour chaque tick
                })
            pnl_today = sum(t.get("pnl", 0) for t in self._capital_closed_today)
            wins  = sum(1 for t in self._capital_closed_today if t.get("pnl", 0) > 0)
            total = len(self._capital_closed_today)
            wr    = (wins / total * 100) if total > 0 else 0.0
            pnl_total_real = round(balance - self.initial_balance, 2) if balance > 0 else 0.0
            # \u2500\u2500 Snapshot \u00e9quit\u00e9 pour Chart.js (max 200 points) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            if balance > 0:
                self._equity_history.append({
                    "t": now.strftime("%H:%M"),
                    "v": round(balance, 2),
                })
                if len(self._equity_history) > 200:
                    self._equity_history = self._equity_history[-200:]

            # Calcul DD mensuel pour affichage
            monthly_dd_pct = 0.0
            if self._monthly_start_balance > 0 and balance > 0:
                monthly_dd_pct = round(
                    (self._monthly_start_balance - balance) / self._monthly_start_balance * 100, 2
                )

            uptime_h = round((datetime.now(timezone.utc) - self._bot_start_time).total_seconds() / 3600, 1)
            dash_update(
                balance=balance, initial=self.initial_balance,
                pnl_total=pnl_total_real,
                pnl_today=round(pnl_today, 2),
                trades=open_trades, wr_overall=round(wr, 1),
                n_total=total, symbols=list(CAPITAL_INSTRUMENTS),
                paused=self._manual_pause, futures_balance=0.0,
                max_slots=MAX_OPEN_TRADES,
                equity_history=list(self._equity_history),
                monthly_dd_pct=monthly_dd_pct,
                uptime_h=uptime_h,
            )

            # ── Filtres dashboard (valeurs réelles) ────────────────────────
            try:
                fg = self.context._fg_value
                fg_label = self.context._fg_label
                dash_filter("fear_greed",
                    f"{fg}/100 ({fg_label})" if fg is not None else "—")
            except Exception:
                pass
            try:
                drift_res = self.drift.check_drift()
                drift_str = "🟢 Stable" if not drift_res.get("drift") else "🔴 Dérivé"
                dash_filter("drift", drift_str)
            except Exception:
                pass
            try:
                news_pause, news_reason = self.calendar.should_pause_trading()
                dash_filter("news", "⏸️ Pause" if news_pause else "🟢 OK")
            except Exception:
                pass
            if balance > 0:
                logger.debug(
                    f"💰 Balance : {balance:,.2f}€ | PnL total : {pnl_total_real:+.2f}€"
                    f" | Positions ouvertes : {len(open_trades)}"
                )
        except Exception:
            pass


        # ── EquityCurve : enregistrement + circuit breaker ────────────────────
        if balance > 0:
            self.equity.record(balance)
            if self.equity.is_below_ma(ma_period=20) and not self._dd_paused:
                logger.warning("⏸️  EquityCurve sous MA20 — circuit breaker déclenché")
                self._dd_paused = True
                pnl_pct = self.equity.total_pnl_pct()
                try:
                    self.notifier.notify_circuit_breaker(
                        reason="Equity sous MA20 (20 derniers points)",
                        balance=balance,
                        pnl_pct=pnl_pct,
                    )
                except Exception:
                    pass

        # ── Reset quotidien (minuit UTC) ─────────────────────────────────────
        if today != self._last_reset_day:
            self._last_reset_day = today
            self._capital_closed_today.clear()
            self._dd_paused = False
            self.reporter.reset_for_new_day()  # remet rapport à zéro
            # BUG FIX #2 : met à jour le solde de début de journée pour le DD journalier
            if self.capital.available:
                self._daily_start_balance = self.capital.get_balance() or self._daily_start_balance
            logger.info("🔄 Reset quotidien — stats journalières effacées")
            self._last_session_push = ""    # reset push session pour le nouveau jour

            # ── Reset mensuel & Drawdown Mensuel ─────────────────────────────
            cur_month = now.month
            if cur_month != self._last_reset_month:
                self._last_reset_month      = cur_month
                self._monthly_dd_paused     = False
                self._monthly_start_balance = self.capital.get_balance() or self._monthly_start_balance
                logger.info("📅 Reset mensuel — drawdown mensuel remis à zéro")
            else:
                # Vérification DD mensuel (toujours dans le même mois)
                if self._monthly_start_balance > 0 and not self._monthly_dd_paused:
                    bal_now = self.capital.get_balance() or 0
                    monthly_dd_pct = (self._monthly_start_balance - bal_now) / self._monthly_start_balance * 100
                    if monthly_dd_pct >= 15:
                        self._monthly_dd_paused = True
                        self._dd_paused = True
                        logger.critical(f"🚨 DD MENSUEL CRITIQUE {monthly_dd_pct:.1f}% ≥ 15% — pause totale")
                        self.telegram.send_message(
                            f"🚨 <b>DD MENSUEL CRITIQUE — {monthly_dd_pct:.1f}%</b>\n"
                            f"Seuil 15% atteint. Bot en pause jusqu'au 1er du mois."
                        )
                    elif monthly_dd_pct >= 10:
                        self._dd_paused = True
                        logger.warning(f"⚠️ DD mensuel {monthly_dd_pct:.1f}% ≥ 10% — pause 48h")
                        self.telegram.send_message(
                            f"⚠️ <b>DD Mensuel — {monthly_dd_pct:.1f}%</b>\n"
                            f"Seuil 10% atteint. Pause trading 48h. Reprise demain."
                        )

        # \u2500\u2500 SPRINT 4 : Backup Supabase automatique (toutes les 5 min) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Survie au crash/redémarrage Railway sans perdre l'état des positions.
        elapsed_backup = (now - self._last_backup_time).total_seconds()
        if elapsed_backup >= 300:  # 5 minutes
            self._last_backup_time = now
            try:
                for inst, state in self.capital_trades.items():
                    if state is not None:
                        self.db.save_capital_trade(inst, state)
                logger.debug("💾 Backup Supabase — états positions sauvegardés")
            except Exception as _bk_e:
                logger.debug(f"Backup Supabase: {_bk_e}")

        # \u2500\u2500 SPRINT 4 : Drift Auto-Size Reduction \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Si concept drift détecté → réduire automatiquement de 50% pendant 48h
        # (au lieu de juste alerter comme avant)
        try:
            drift_result = self.drift.check_drift()
            if drift_result.get("drift") and not self._drift_size_reduced:
                self._drift_size_reduced  = True
                self._drift_reduced_until = now + timedelta(hours=48)
                logger.warning("🔴 Drift détecté → taille réduite de 50% pour 48h")
                self.telegram.send_message(
                    "🔴 <b>Concept Drift détecté</b>\n"
                    "La stratégie dérive par rapport au backtest.\n"
                    "Taille des positions réduite de <b>50%</b> pour 48h.\n"
                    "Optimisation auto planifiée dimanche prochain."
                )
            elif self._drift_reduced_until and now > self._drift_reduced_until:
                # Fin de la période de réduction
                self._drift_size_reduced  = False
                self._drift_reduced_until = None
                logger.info("🟢 Période drift terminée — taille normale restaurée")
        except Exception:
            pass

        # \u2500\u2500 SPRINT 4 : Auto-Optimisation Hebdomadaire (Dimanche 2h UTC) \u2500\u2500\u2500\u2500\u2500
        # Lance optimizer.py (Optuna) chaque dimanche matin à 2h UTC.
        # Evite de relancer si déjà lancé cette semaine (clé = isoweek).
        if (now.weekday() == 6 and now.hour == 2 and now.minute < 5):
            cur_week = now.isocalendar()[1]
            if self._last_hyperopt_week != cur_week:
                self._last_hyperopt_week = cur_week
                logger.info(f"⚙️  Auto-Optimisation hebdo S{cur_week} — lancement...")
                self.telegram.send_message(
                    f"⚙️ <b>Auto-Optimisation S{cur_week}</b>\n"
                    f"Optuna en cours (30 trials × {len(CAPITAL_INSTRUMENTS)} instruments)...\n"
                    f"Résultats dans ~10 minutes."
                )
                import threading, subprocess, sys
                def _run_optimizer():
                    try:
                        result = subprocess.run(
                            [sys.executable, "optimizer.py",
                             "--trials", "30", "--days", "30"],
                            cwd=os.path.dirname(os.path.abspath(__file__)),
                            capture_output=True, text=True, timeout=600
                        )
                        if result.returncode == 0:
                            logger.info("✅ Auto-Optimisation terminée")
                            self.telegram.send_message(
                                "✅ <b>Auto-Optimisation terminée</b>\n"
                                "Nouveaux paramètres appliqués au prochain tick."
                            )
                        else:
                            logger.warning(f"⚠️ Optimizer exit {result.returncode}: {result.stderr[:200]}")
                    except subprocess.TimeoutExpired:
                        logger.warning("⏱️ Optimizer timeout (>10min)")
                    except Exception as _opt_e:
                        logger.error(f"❌ Optimizer: {_opt_e}")

                    # Feature P : Entraîner le LSTM sur chaque instrument
                    try:
                        for _inst in CAPITAL_INSTRUMENTS:
                            df_train = self.capital.fetch_ohlcv(_inst, timeframe="5m", count=400)
                            if df_train is not None and len(df_train) >= 100:
                                df_train = self.strategy.compute_indicators(df_train)
                                ok = self.lstm.train(df_train)
                                if ok:
                                    logger.info(f"🧠 LSTM Predictor entraîné sur {_inst}")
                    except Exception as _lstm_e:
                        logger.warning(f"LSTM training: {_lstm_e}")

                    # Feature U : Rapport A/B hebdomadaire
                    try:
                        report = self.ab.weekly_report()
                        winner = self.ab.global_winner()
                        self.telegram.send_message(
                            f"{report}\n🏆 Variante globale : <b>{winner}</b>"
                        )
                    except Exception as _ab_e:
                        logger.debug(f"AB weekly: {_ab_e}")

                threading.Thread(target=_run_optimizer, daemon=True).start()


        # ── Auto-push Telegram : ouverture de session ─────────────────────────

        h_utc = now.hour
        # Détecte début de session London (8h UTC) et NY (13h UTC)
        current_session = ""
        if h_utc == 8:   current_session = "London"
        elif h_utc == 13: current_session = "NY"

        if current_session and current_session != self._last_session_push:
            self._last_session_push = current_session
            try:
                bal_push = self.capital.get_balance() if self.capital.available else 0.0
                pnl_push = round(bal_push - self.initial_balance, 2) if bal_push > 0 else 0.0
                pnl_pct_push = (pnl_push / self.initial_balance * 100) if self.initial_balance > 0 else 0.0
                session_icon = "🇬🇧" if current_session == "London" else "🇺🇸"
                self.telegram.send_message(
                    f"{session_icon} <b>Session {current_session} ouverte</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Balance : <b>{bal_push:,.2f}€</b>\n"
                    f"📊 PnL total : <b>{pnl_push:+.2f}€ ({pnl_pct_push:+.1f}%)</b>\n"
                    f"🤖 Bot : 🟢 ACTIF — scanning {len(CAPITAL_INSTRUMENTS)} instruments\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━"
                )
                logger.info(f"{session_icon} Session {current_session} ouverte — alerte Telegram envoyée")
            except Exception as _e:
                logger.debug(f"Auto-push session : {_e}")

        # ── Auto-push Telegram : heartbeat toutes les 30min en session active ──
        in_session = h_utc in SESSION_HOURS
        since_last = (now - self._last_heartbeat_push).total_seconds()
        if in_session and since_last >= 1800:  # 30 minutes
            self._last_heartbeat_push = now
            try:
                bal_hb    = self.capital.get_balance() if self.capital.available else 0.0
                pnl_hb    = round(bal_hb - self.initial_balance, 2) if bal_hb > 0 else 0.0
                pnl_pct   = (pnl_hb / self.initial_balance * 100) if self.initial_balance > 0 else 0.0
                open_pos  = [instr for instr, s in self.capital_trades.items() if s is not None]
                pos_lines = ""
                for epic in open_pos:
                    state = self.capital_trades[epic]
                    name  = CAPITAL_NAMES.get(epic, epic)
                    entry = state.get("entry", 0.0)
                    direction = state.get("direction", "?")
                    unreal = 0.0
                    try:
                        px = self.capital.get_current_price(epic)
                        if px:
                            unreal = round((px["mid"] - entry) * (1 if direction == "BUY" else -1) * 3, 2)
                    except Exception:
                        pass
                    icon = "🟢" if unreal >= 0 else "🔴"
                    pos_lines += f"  • <b>{name}</b> {direction} | {icon} {unreal:+.2f}€\n"
                pnl_today_hb = sum(t.get("pnl", 0) for t in self._capital_closed_today)
                self.telegram.send_message(
                    f"📡 <b>Heartbeat Nemesis</b> — {cet.strftime('%H:%M')} CET\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Balance : <b>{bal_hb:,.2f}€</b>  ({pnl_pct:+.1f}%)\n"
                    f"📈 PnL aujourd'hui : <b>{pnl_today_hb:+.2f}€</b>\n"
                    + (f"📊 Positions ouvertes :\n{pos_lines}" if pos_lines else "📊 Aucune position ouverte\n")
                    + f"━━━━━━━━━━━━━━━━━━━━━━━"
                )
            except Exception as _e:
                logger.debug(f"Auto-push heartbeat : {_e}")

        # ── Vérification drawdown journalier ─────────────────────────────
        if not self._dd_paused and self.capital.available:
            cur_bal = self.capital.get_balance()
            # BUG FIX #2 : utilise _daily_start_balance (solde début de jour) et non initial_balance (lancement bot)
            if cur_bal > 0 and self._daily_start_balance > 0:
                dd_pct = (self._daily_start_balance - cur_bal) / self._daily_start_balance * 100
                if dd_pct >= self.DAILY_DD_LIMIT:
                    self._dd_paused = True
                    self.telegram.send_message(
                        f"🚨 <b>DRAWDOWN JOURNALIER ATTEINT</b>\n"
                        f"Balance : <code>{cur_bal:,.2f}€</code>\n"
                        f"DD : <b>{dd_pct:.1f}%</b> (limite : {self.DAILY_DD_LIMIT:.1f}%)\n"
                        f"⏸️ Trading suspendu jusqu'à demain."
                    )
                    logger.warning(f"🚨 DD journalier {dd_pct:.1f}% — trading suspendu")

        # ── Morning Brief (07h00 UTC) ─────────────────────────────────────────
        if self.context.should_send_brief():
            balance = self.capital.get_balance() if self.capital.available else 0.0
            _, reason = self.calendar.should_pause_trading()
            brief = self.context.build_morning_brief(balance, reason or None)
            self.telegram.notify_morning_brief(brief, nb_instruments=len(CAPITAL_INSTRUMENTS))
            self.context.mark_brief_sent()

        # ── Fear & Greed refresh (1×/heure) ──────────────────────────────
        self.context.refresh_fear_greed()

        # ── Wallet stats (toutes les 30 min) ─────────────────────────────
        wallet_interval = timedelta(minutes=30)
        if now - self._last_wallet_post >= wallet_interval:
            balance_w = self.capital.get_balance() if self.capital.available else 0.0
            if balance_w > 0:
                self._post_wallet_stats(balance_w)
            self._last_wallet_post = now

        # ── Rapport journalier (20h UTC) + hebdo (21h UTC) ───────────────
        if self.reporter.should_send_report():
            self.telegram.send_message(self.reporter.build_report())
            self.reporter.mark_report_sent()
        if self.reporter.should_send_weekly():
            self.telegram.send_message(self.reporter.build_weekly_report())
            self.reporter.mark_weekly_sent()

        # \u2500\u2500 Sprint 5 : Rapport visuel PNG journalier (20h UTC) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if h_utc == 20 and today != self._last_daily_report_day:
            self._last_daily_report_day = today
            try:
                import threading
                threading.Thread(target=self._send_daily_report, daemon=True).start()
            except Exception as _rp_e:
                logger.debug(f"Daily report: {_rp_e}")


        # ─── Moteur de trading Capital.com ───────────────────────────────────

        # Pause manuelle ou drawdown
        if self._manual_pause or self._dd_paused:
            logger.info("⏸️  Trading en pause (manuel ou DD) — skip ce tick")
            return

        # Capital.com non disponible → rien à faire
        if not self.capital.available:
            logger.warning("⚠️  Capital.com non disponible — skip ce tick")
            return

        # ── Surveillance des positions ouvertes ──────────────────────────
        self._monitor_capital_positions()

        # ── Vérification session London/NY (08h-10h30 / 13h30-16h UTC) ──
        if not self.strategy.is_session_ok():
            logger.debug(f"🕐 Hors session ({now.hour}h{now.minute:02d} UTC) — skip")
            return

        # ── Pause calendrier économique ───────────────────────────────────
        should_pause, reason = self.calendar.should_pause_trading()
        if should_pause:
            logger.info(f"📅 Trading suspendu : {reason}")
            return

        # ── Limite corrélation (max 2 CFD simultanées) ───────────────────────
        active_count = sum(1 for s in self.capital_trades.values() if s is not None)
        if active_count >= 2:
            logger.debug(f"🔒 Positions max atteint ({active_count}/2) — skip ce tick")
            return  # Plafond atteint — on surveille mais on n'ouvre rien

        # ── Scan des instruments Capital.com ─────────────────────────────────
        balance = self.capital.get_balance()
        if balance <= 0:
            logger.warning("⚠️  Balance = 0 ou inaccessible — skip ce tick")
            return

        per_instrument = balance / len(CAPITAL_INSTRUMENTS)

        # ── Heartbeat visible : confirme que la boucle tourne ──────────────────
        logger.info(
            f"🔍 Scan {len(CAPITAL_INSTRUMENTS)} instruments | "
            f"Balance={balance:,.0f}€ | Positions={active_count}/2 | "
            f"{now.hour}h{now.minute:02d} UTC"
        )

        signals_found = 0
        for instrument in CAPITAL_INSTRUMENTS:
            # Ne pas ouvrir si limite atteinte entre deux itérations
            if sum(1 for s in self.capital_trades.values() if s is not None) >= 2:
                break
            try:
                _open_before = sum(1 for s in self.capital_trades.values() if s is not None)
                self._process_capital_symbol(instrument, per_instrument)
                _open_after  = sum(1 for s in self.capital_trades.values() if s is not None)
                if _open_after > _open_before:
                    signals_found += 1
            except Exception as e:
                logger.error(f"❌ _process_capital_symbol {instrument} : {e}")

        # ── Alerte Telegram "scan sans signal" toutes les 10 minutes ──────────
        # Garde l'utilisateur informé que le bot tourne et surveille le marché
        if signals_found == 0:
            elapsed_ns = (now - self._last_no_signal_alert).total_seconds()
            if elapsed_ns >= 600:  # 10 minutes
                self._last_no_signal_alert = now
                session_str = "London" if now.hour < 13 else "NY"
                open_pos = [CAPITAL_NAMES.get(i, i) for i, s in self.capital_trades.items() if s is not None]
                pos_str = ", ".join(open_pos) if open_pos else "Aucune"
                fg = getattr(self.context, "_fg_value", None)
                fg_str = f" | F&G : {fg}/100" if fg is not None else ""
                try:
                    self.telegram.send_message(
                        f"🔍 <b>Nemesis surveille</b> — {session_str} session{fg_str}\n"
                        f"⏰ {now.strftime('%H:%M')} UTC | Balance : <b>{balance:,.0f}€</b>\n"
                        f"📊 Positions ouvertes : {pos_str}\n"
                        f"Aucun breakout détecté sur {len(CAPITAL_INSTRUMENTS)} instruments \u2014 surveillance continue…"
                    )
                except Exception:
                    pass


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
            logger.warning(f"⚠️  {instrument}: OHLCV None ou insuffisant ({len(df) if df is not None else 'None'} bougies) — skip")
            return


        df  = self.strategy.compute_indicators(df)

        # ── UPGRADE : Retest Entry (Anti-Fakeout) ────────────────────────────
        # Vérifie s'il y a un retest en attente pour cet instrument.
        # BYPASS si score >= 2 (signal fort = entrée directe sans attendre retest)
        pending = self._pending_retest.get(instrument)
        if pending:
            try:
                px_now = self.capital.get_current_price(instrument)
                if px_now:
                    mid       = px_now["mid"]
                    p_sig     = pending["sig"]
                    p_level   = pending["retest_level"]  # niveau cassé
                    p_atr     = pending["atr"]
                    tolerance = p_atr * 0.5              # zone de retest = ATR × 0.5
                    ticks     = pending.get("ticks_waited", 0) + 1
                    pending["ticks_waited"] = ticks

                    # Condition retest : prix revenu près du niveau cassé
                    in_retest_zone = abs(mid - p_level) <= tolerance

                    # Annulation : trop longtemps sans retest (> 6 ticks = 3 min)
                    if ticks > 6:
                        logger.info(f"⏳ Retest {instrument} expiré ({ticks} ticks) — annulé")
                        self._pending_retest[instrument] = None
                        return

                    if in_retest_zone:
                        # ✅ Prix a retesté → on entre !
                        logger.info(
                            f"🔄 RETEST CONFIRMÉ {instrument} {p_sig} "
                            f"| prix={mid:.5f} ≈ niveau={p_level:.5f} (±{tolerance:.5f})"
                        )
                        self._pending_retest[instrument] = None
                        # Synthétiser le signal pendentif comme signal courant
                        sig          = p_sig
                        score        = pending["score"]
                        confirmations = pending["confirmations"] + ["Retest✓"]
                    else:
                        # Pas encore retesté → attendre
                        logger.debug(
                            f"⏳ Retest {instrument} {p_sig} | prix={mid:.5f} "
                            f"| niveau={p_level:.5f} | ticks={ticks}/6"
                        )
                        return
            except Exception as _re:
                logger.debug(f"Retest check {instrument}: {_re}")
                self._pending_retest[instrument] = None
                return
        else:
            # Pas de retest en attente → analyser le signal
            sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument)
            if sig == "HOLD":
                return

            # Stocker le breakout en attente de retest UNIQUEMENT si score < 2
            # Score ≥ 2 = signal fort → entrée directe sans attendre le retest
            sr_now  = self.strategy.compute_session_range(df)
            atr_now = self.strategy.get_atr(df)
            if atr_now > 0 and score < 2:
                retest_level = sr_now["high"] if sig == "BUY" else sr_now["low"]
                self._pending_retest[instrument] = {
                    "sig":           sig,
                    "retest_level":  retest_level,
                    "atr":           atr_now,
                    "score":         score,
                    "confirmations": confirmations,
                    "ticks_waited":  0,
                }
                logger.info(
                    f"🔔 Breakout {instrument} {sig} score={score} | niveau={retest_level:.5f} "
                    f"| Attente retest (ATR={atr_now:.5f})…"
                )
                return  # ← attendre le retest la prochaine fois
            # score >= 2 ou atr=0 : entrée directe sans retest
            logger.info(f"⚡ Entrée directe {instrument} {sig} score={score} (bypass retest)")


        # BUG FIX #5 : Vérification RiskManager avant d'ouvrir
        balance_for_risk = self.capital.get_balance() or balance
        if not self.risk.can_open_trade(balance_for_risk, instrument=instrument):
            logger.info(f"⛔ {instrument} bloqué par RiskManager (DD, MAX_TRADES ou déjà ouvert)")
            return

        # Protection Model : blacklist après 3 SL consécutifs
        if self.protection.is_blocked(instrument):
            return

        # MTFFilter : confluence 1h + 4h avant d'entrer
        if not self.mtf.validate_signal(instrument, sig):
            return

        # ── UPGRADE : Filtre Corrélation (évite surexposition USD) ────────────
        # Si 2 paires USD (EUR/USD, GBP/USD, USD/JPY) sont déjà ouvertes dans
        # la même direction → exposition trop corrélée, on bloque.
        USD_PAIRS = {"EURUSD", "GBPUSD", "USDJPY"}
        if instrument in USD_PAIRS:
            same_dir_usd = sum(
                1 for ep, st in self.capital_trades.items()
                if st is not None
                and ep in USD_PAIRS
                and ep != instrument
                and st.get("direction") == ("BUY" if sig == "BUY" else "SELL")
            )
            if same_dir_usd >= 2:
                logger.info(
                    f"⛔ Corrélation USD bloquée {instrument} — "
                    f"{same_dir_usd} paires USD déjà ouvertes même direction"
                )
                return

        entry    = float(df.iloc[-1]["close"])
        sr       = self.strategy.compute_session_range(df)
        direction = "BUY" if sig == "BUY" else "SELL"
        rng      = sr["size"]

        if rng <= 0 or sr["pct"] < 0.08:
            return

        # ── UPGRADE : Slippage Guard (spread < ATR × 0.15 avant tout ordre) ──
        # Un spread trop large signifie marché peu liquide ou news en cours.
        # On ne veut pas payer 15% d'un ATR en slippage​/spread.
        try:
            px_check = self.capital.get_current_price(instrument)
            if px_check:
                spread      = px_check["ask"] - px_check["bid"]
                atr_check   = self.strategy.get_atr(df)
                spread_max  = atr_check * 0.15 if atr_check > 0 else spread  # bypass si ATR=0
                if spread > spread_max and atr_check > 0:
                    logger.info(
                        f"🚫 Slippage Guard {instrument} — spread={spread:.5f} > max={spread_max:.5f} "
                        f"(ATR×0.15) — skip"
                    )
                    return
        except Exception as _sg:
            logger.debug(f"Slippage check {instrument}: {_sg}")

        # ── UPGRADE : Exposition max par devise (30% total, 15% par instrument) ──
        # Limite le risque total en cas de marché correctionnel brutal.
        balance_now = self.capital.get_balance() or balance
        if balance_now > 0:
            total_open = len([s for s in self.capital_trades.values() if s is not None])
            max_open_value = balance_now * 0.30   # 30% max en exposition totale
            # Heuristique : chaque position vaut en moyenne 3% du balance (RISK=1% × 3 lots)
            est_exposure = total_open * balance_now * 0.03
            if est_exposure >= max_open_value:
                logger.info(
                    f"⛔ Exposition max atteinte {instrument} — "
                    f"{total_open} positions ≈ {est_exposure:.0f}€ > 30% ({max_open_value:.0f}€)"
                )
                return

        # \u2500\u2500 SPRINT FINAL P : LSTM Timing Prediction \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Si le score LSTM < 0.65 → timing pas optimal → wait
        try:
            lstm_allow, lstm_score = self.lstm.should_enter(df)
            if not lstm_allow:
                logger.debug(
                    f"🧠 LSTM block {instrument} {sig} — score={lstm_score:.2f} < 0.65"
                )
                return
        except Exception:
            lstm_score = 1.0

        # \u2500\u2500 SPRINT FINAL U : A/B Testing — lire les paramètres actifs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # L'ABTester alterne les instruments entre variante A (stable) et B (explorateur)
        ab_variant = self.ab.get_variant(instrument)

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

        # \u2500\u2500 SPRINT FINAL T : DRL Size Multiplier \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Agent Kelly adaptatif : multiplie la taille en fonction de la performance récente
        drl_mult = self.drl.get_multiplier()
        if drl_mult != 1.0:
            size1 = max(min_sz, round(size1 * drl_mult, 2))
            logger.debug(f"🎯 DRL size mult={drl_mult:.2f}× → size1={size1}")


        # Sprint 4 : Drift size reduction (50% si drift actif)
        if self._drift_size_reduced and self._drift_reduced_until and datetime.now(timezone.utc) < self._drift_reduced_until:
            size1 = max(min_sz, round(size1 * 0.5, 2))
            logger.debug(f"🔴 Drift reduction active — taille réduite à {size1}")


        # ── UPGRADE : Session Overlap Boost (13h-17h UTC = volume max) ─────────
        # Le London-NY Overlap est la fenêtre de liquidité maximale de la journée.
        # On augmente la taille à 1.5× pendant cet overlap.
        h_utc_now = datetime.now(timezone.utc).hour
        in_overlap = 13 <= h_utc_now < 17
        if in_overlap:
            size1_boosted = max(min_sz, round(size1 * 1.5, 2))
            logger.info(
                f"⚡ Session Overlap (London∕NY) — taille boostée : "
                f"{size1:.2f} → {size1_boosted:.2f}"
            )
            size1 = size1_boosted

        # ── UPGRADE : R:R Adaptatif (ADX > 30 = tendance forte) ─────────────
        # En tendance forte, on laisse courir plus loin.
        adx_now = float(df.iloc[-1].get("adx", 0)) if "adx" in df.columns else 0
        if adx_now > 30:
            rr_tp2 = 2.5 if sig == "BUY" else 2.5
            rr_tp3 = 4.0 if sig == "BUY" else 4.0
            logger.info(f"📈 ADX={adx_now:.0f} > 30 — R:R étendu : TP2=×2.5R, TP3=×4.0R")
            if sig == "BUY":
                tp2 = entry + rng * rr_tp2
                tp3 = entry + rng * rr_tp3
            else:
                tp2 = entry - rng * rr_tp2
                tp3 = entry - rng * rr_tp3

        # ── UPGRADE : HMM Regime Switching ───────────────────────────────
        # Détecte le régime de marché : TREND_UP / TREND_DOWN / RANGING
        # En RANGING : réduit la taille de 50% (marché sans direction)
        # En TREND_UP : bloque SELL. En TREND_DOWN : bloque BUY.
        regime_result = {"name": "RANGING", "regime": 0, "confidence": 0.5}
        try:
            regime_result = self.hmm.detect_regime(df, symbol=instrument)
            regime_name   = regime_result["name"]
            regime_conf   = regime_result["confidence"]
            logger.debug(f"🧠 HMM Regime {instrument} : {regime_name} (conf={regime_conf:.0%})")

            if regime_result["regime"] == 0 and regime_conf >= 0.6:
                # RANGING + confiance élevée → réduit la taille
                size1 = max(min_sz, round(size1 * 0.5, 2))
                logger.info(f"🔶 HMM RANGING ({regime_conf:.0%}) — taille réduite à {size1}")
            elif regime_result["regime"] == 1 and sig == "SELL" and regime_conf >= 0.65:
                # TREND_UP mais signal SELL → contre-tendance, skip
                logger.info(f"⛔ HMM TREND_UP ({regime_conf:.0%}) bloque SELL sur {instrument}")
                return
            elif regime_result["regime"] == 2 and sig == "BUY" and regime_conf >= 0.65:
                # TREND_DOWN mais signal BUY → contre-tendance, skip
                logger.info(f"⛔ HMM TREND_DOWN ({regime_conf:.0%}) bloque BUY sur {instrument}")
                return
        except Exception as _hmm_e:
            logger.debug(f"HMM {instrument}: {_hmm_e}")

        if size1 <= 0:
            return

        # ─── ORDRES EN PARALLÈLE ────────────────────────
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
            # ─ Trade Journal (analyse post-trade) ─────────────────────
            "score":      score,
            "confirmations": confirmations,
            "regime":    regime_result.get("name", "RANGING"),
            "fear_greed": self.context._fg_value,
            "in_overlap": in_overlap,
            "adx_at_entry": adx_now,
            # ─ Time-based Stop Loss (90min) ──────────────────────────
            "open_time":  datetime.now(timezone.utc),
        }
        # BUG FIX #3 : calcule name/session une seule fois (suppression doublon)
        name    = CAPITAL_NAMES.get(instrument, instrument)
        hour    = datetime.now(timezone.utc).hour
        minute  = datetime.now(timezone.utc).minute
        # London : 08h-10h UTC | NY : 13h30-16h UTC
        session = "London" if (hour < 13 or (hour == 13 and minute < 30)) else "NY"
        tracker = self._london_tracker if session == "London" else self._ny_tracker
        tracker.record_entry(name=name, sig=sig, entry=entry, size=size1)

        # BUG FIX #5 : Notifie le RiskManager de l'ouverture
        self.risk.on_trade_opened(instrument=instrument)

        # Persiste immédiatement en BDD (survit aux redémarrages Railway mid-trade)
        try:
            self.db.save_capital_trade(instrument, self.capital_trades[instrument])
        except Exception as exc:
            logger.warning(f"⚠️ DB save_capital_trade open: {exc}")

        logger.info(f"✅ Capital.com {sig} {instrument} @ {entry:.5f} | SL={sl:.5f} TP1={tp1:.5f} TP2={tp2:.5f} TP3={tp3:.5f}")

        # ─── ÉTAPE 4 : Telegram en background (ne bloque pas la boucle) ────────

        import threading
        _snap = dict(instrument=instrument, name=name, sig=sig,
                     entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                     size=size1, score=score, session=session,
                     range_pct=sr["pct"], range_high=sr["high"], range_low=sr["low"],
                     confirmations=list(confirmations), df=df.copy())  # .copy() évite race condition
        threading.Thread(target=lambda: tgc.notify_capital_entry(**_snap), daemon=True).start()

        # \u2500\u2500 SPRINT FINAL K : Signal Card visuel \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        # Génère image mplfinance chart 5m (2h) avec niveaux annotés
        # Envoyée via Telegram.sendPhoto en background (pas de blocage loop)
        try:
            _regime_name = regime_result.get("name", "RANGING") if regime_result else "RANGING"
            _fg_val = getattr(self.context, "_fg_value", None)
            _card_df = df.copy()
            _card_args = dict(
                df=_card_df, instrument=instrument, direction=direction,
                entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                score=score, confirmations=list(confirmations),
                regime=_regime_name, fear_greed=_fg_val, session=session,
            )
            def _do_send_card():
                img = generate_signal_card(**_card_args)
                if img:
                    caption = (
                        f"📸 <b>{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} "
                        f"{name}</b>  |  Score {score}/7\n"
                        f"💰 Entry: {entry:.5f}  |  SL: {sl:.5f}\n"
                        f"🎯 TP1: {tp1:.5f}  TP2: {tp2:.5f}  TP3: {tp3:.5f}"
                    )
                    self.telegram.send_photo(img, caption=caption)
            threading.Thread(target=_do_send_card, daemon=True).start()
        except Exception as _kex:
            logger.debug(f"Signal card: {_kex}")

        # Stocker la variante A/B dans l'état pour feedback à la fermeture
        if self.capital_trades[instrument]:
            self.capital_trades[instrument]["ab_variant"] = ab_variant


        # ── Dashboard : enregistre l'ouverture ─────────────────────────────
        try:
            dash_open(symbol=CAPITAL_NAMES.get(instrument, instrument),
                      side=direction, entry=entry, qty=size1)
        except Exception:
            pass

    def _on_ws_price_tick(self, epic: str, mid: float) -> None:
        """
        Feature R — Callback appelé par le WebSocket à chaque tick de prix.
        Throttle : 1 appel/s par epic (géré dans capital_websocket.py).

        Si un instrument n'a pas encore de position ET qu'un retest est en attente
        (prix proche du niveau cassé ± ATR), on évalue immédiatement le signal
        sans attendre le prochain polling 30s.

        Latence cible : < 2 secondes après le tick de prix.
        """
        try:
            # Vérifier si une position est déjà ouverte (ne pas doubler)
            if self.capital_trades.get(epic) is not None:
                return

            # Vérifier si un retest est en attente pour cet epic
            retest = self._pending_retest.get(epic)
            if retest is None:
                return

            retest_level = retest.get("retest_level", 0)
            atr_now = retest.get("atr", 0)
            if atr_now <= 0:
                return

            # Si le prix est dans la zone de retest (±0.5 ATR du niveau) → trigger
            if abs(mid - retest_level) <= atr_now * 0.5:
                logger.debug(
                    f"⚡ WS real-time trigger {epic} — prix {mid:.5f} ≈ retest {retest_level:.5f}"
                )
                # Récupérer balance courante (cache)
                bal = self.capital.get_balance() if self.capital.available else 0.0
                if bal > 0:
                    self._process_capital_symbol(epic, bal)
        except Exception as _ws_e:
            logger.debug(f"WS price_tick {epic}: {_ws_e}")

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

            # \u2500\u2500 UPGRADE : Time-based Stop Loss (90 minutes max sans TP1) \u2500\u2500\u2500\u2500\u2500\u2500
            # Si le trade est ouvert depuis > 90 min et TP1 non touché
            # → le breakout n'a pas performé → fermeture forcée (trade zombie)
            # Exception : si TP1 déjà touché (trailing stop actif), on laisse courir.
            if not state.get("tp1_hit"):
                open_time = state.get("open_time")
                if open_time:
                    age_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                    if age_minutes > 90:
                        name_ts = CAPITAL_NAMES.get(instrument, instrument)
                        logger.warning(
                            f"⏱️  Time-Stop {instrument} — {age_minutes:.0f}min sans TP1 "
                            f"→ fermeture forcée"
                        )
                        # Fermer toutes les positions encore ouvertes
                        for ref in refs:
                            if ref and ref in open_refs:
                                try:
                                    self.capital.close_position(ref)
                                except Exception as _ts_e:
                                    logger.debug(f"Time-stop close {ref}: {_ts_e}")
                        self.telegram.send_message(
                            f"⏱️ <b>Time-Stop déclenché — {name_ts}</b>\n"
                            f"Ouvert depuis <b>{age_minutes:.0f} min</b> sans atteindre TP1.\n"
                            f"Fermeture de toutes les positions (trade zombie évité)."
                        )
                        self.capital_trades[instrument] = None
                        self._pending_retest[instrument] = None
                        continue  # passer à l'instrument suivant


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

            # ── ATR Trailing Stop (après TP1 / BE activé) ───────────────────
            # Déplace le SL à ATR × 1.5 derrière le prix actuel pour capturer
            # plus de gains sur les trades gagnants.
            if state.get("tp1_hit"):
                try:
                    df_trail = self.capital.get_ohlcv(instrument, "MINUTE_5", count=20)
                    if df_trail is not None and len(df_trail) >= 14:
                        df_trail = self.strategy.compute_indicators(df_trail)
                        atr = self.strategy.get_atr(df_trail)
                        if atr > 0:
                            px = self.capital.get_current_price(instrument)
                            if px:
                                mid = px["mid"]
                                direction = state.get("direction", "BUY")
                                # Nouveau SL : ATR 1.5× derrière le prix
                                if direction == "BUY":
                                    new_trail_sl = round(mid - atr * 1.5, 5)
                                    # Ne descend jamais sous l'entrée (déjà BE)
                                    new_trail_sl = max(new_trail_sl, entry)
                                else:
                                    new_trail_sl = round(mid + atr * 1.5, 5)
                                    new_trail_sl = min(new_trail_sl, entry)

                                # Appliquer le trailing stop aux positions ouvertes 2 et 3
                                for ref in [refs[1], refs[2]]:
                                    if ref and ref in open_refs:
                                        self.capital.modify_position_stop(ref, new_trail_sl)
                                state["trailing_sl"] = new_trail_sl
                                logger.debug(
                                    f"🔄 Trailing Stop {instrument} {direction} "
                                    f"| prix={mid:.5f} | SL→{new_trail_sl:.5f} (ATR={atr:.5f})"
                                )
                except Exception as _te:
                    logger.debug(f"Trailing stop {instrument}: {_te}")


            # Toutes les positions fermées → reset + unwatch WS
            if not ref1_open and not ref2_open and not ref3_open:
                logger.info(f"✅ Capital.com {instrument} — toutes positions fermées")
                # Enregistre pour le dashboard quotidien
                name_close = CAPITAL_NAMES.get(instrument, instrument)
                pip_close  = CAPITAL_PIP.get(instrument, 0.0001)
                # BUG FIX #1 : initialiser les variables AVANT le try pour éviter NameError si get_current_price échoue
                close_px = entry
                pnl_est  = 0.0
                result   = "LOSS"
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
                        "symbol": name_close,
                    })
                    # \u2500\u2500 Sprint 5 : Heatmap \u2014 enregistrement PnL par heure \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
                    try:
                        h_heat = datetime.now(timezone.utc).hour
                        if instrument not in self._heatmap_data:
                            self._heatmap_data[instrument] = {}
                        self._heatmap_data[instrument].setdefault(h_heat, []).append(
                            round(pnl_est * 3, 4)
                        )
                    except Exception:
                        pass

                    # Session tracker — résumé London/NY
                    h_close = datetime.now(timezone.utc).hour
                    m_close = datetime.now(timezone.utc).minute
                    tracker_close = self._london_tracker if (h_close < 13 or (h_close == 13 and m_close < 30)) else self._ny_tracker
                    tracker_close.record_close(name=name_close, pnl=pnl_est * 3, result=result)

                    # \u2500\u2500 Sprint Final : Feedback DRL + AB + LSTM sur résultat \u2500\u2500\u2500\u2500\u2500\u2500\u2500
                    pnl_trade = round(pnl_est * 3, 4)
                    won_trade = pnl_trade > 0
                    rr_trade  = abs(pnl_trade) / max(abs(pnl_est), 0.0001)
                    try:
                        self.drl.record_trade(pnl_trade, rr_trade, state["direction"])
                    except Exception:
                        pass
                    try:
                        ab_v = state.get("ab_variant", "A")
                        self.ab.record_result(instrument, ab_v, pnl_trade, won_trade)
                    except Exception:
                        pass
                    try:
                        self.lstm.notify_trade_result(won_trade)
                    except Exception:
                        pass

                except Exception:
                    pass
                # Persiste la fermeture en BDD
                try:
                    self.db.close_capital_trade(instrument)
                except Exception:
                    pass
                # Dashboard : enregistre la fermeture + PnL (pnl_est/close_px/result toujours définis)
                try:
                    dash_close(symbol=name_close,
                               pnl=round(pnl_est * 3, 2), result=result,
                               side=state["direction"])
                    self.reporter.record_trade(
                        symbol=name_close,
                        side=state["direction"],
                        result=result,
                        pnl_gross=round(pnl_est * 3, 2),
                        entry=state["entry"],
                        exit_price=close_px,
                    )
                except Exception:
                    pass
                self.capital_ws.unwatch(instrument)
                self.capital_trades[instrument] = None
                # BUG FIX #5 + #I : Notifie RiskManager, DriftDetector et ProtectionModel
                self.risk.on_trade_closed(instrument=instrument)
                pnl_final = round(pnl_est * 3, 2)
                self.protection.on_trade_closed(instrument, pnl_final)
                self.drift.record_trade(
                    pnl=pnl_final,
                    win=(result == "WIN"),
                    symbol=name_close,
                )
                # Solde fresh après fermeture pour log/Telegram (réel, pas estimé)
                try:
                    fresh_bal = self.capital.get_balance() or balance
                    pnl_real  = round(fresh_bal - self.initial_balance, 2)
                    icon = "🟢" if pnl_final >= 0 else "🔴"
                    logger.info(
                        f"{icon} {name_close} {result} | PnL trade : {pnl_final:+.2f}€ "
                        f"| Balance : {fresh_bal:,.2f}€ | PnL total : {pnl_real:+.2f}€"
                    )
                except Exception:
                    pass
    # ─── Wallet stats ────────────────────────────────────────────────────────

    def _post_wallet_stats(self, balance: float):
        """Envoie les stats portefeuille Capital.com via tgc.send_daily_dashboard."""
        try:
            closed_today = self._capital_closed_today
            # Win rate par instrument
            wr_by_instr: dict = {}
            for t in closed_today:
                instr = t.get("instrument", "?")
                name  = CAPITAL_NAMES.get(instr, instr)
                wins_i  = sum(1 for x in closed_today if x.get("instrument") == instr and x.get("pnl", 0) > 0)
                total_i = sum(1 for x in closed_today if x.get("instrument") == instr)
                wr_by_instr[name] = (wins_i / total_i * 100) if total_i > 0 else 0.0

            tgc.send_daily_dashboard(
                balance=balance,
                initial_balance=self.initial_balance,
                day_trades=closed_today,
                win_rate_instrument=wr_by_instr,
            )
        except Exception as e:
            logger.warning(f"⚠️  _post_wallet_stats : {e}")

    # ─── Callbacks Telegram ──────────────────────────────────────────────────

    def _force_close(self, instrument: str) -> str:
        """Ferme de force une position Capital.com via commande Telegram."""
        state = self.capital_trades.get(instrument)
        if state is None:
            return f"⚠️ Aucune position ouverte sur {instrument}"
        try:
            refs = state.get("refs", [])
            closed = 0
            for ref in refs:
                if ref:
                    ok = self.capital.close_position(ref)
                    if ok:
                        closed += 1
            name = CAPITAL_NAMES.get(instrument, instrument)
            # ── Nettoyage état ──────────────────────────────────────────
            self.capital_trades[instrument] = None
            self.capital_ws.unwatch(instrument)
            # ── Persiste la fermeture en BDD (sinon resurgi au restart) ─
            try:
                self.db.close_capital_trade(instrument)
            except Exception:
                pass
            # ── Dashboard ────────────────────────────────────────────────
            try:
                dash_close(symbol=name, pnl=0.0, result="MANUAL", side=state.get("direction","?"))
            except Exception:
                pass
            return f"✅ {name} fermé manuellement ({closed} positions)"
        except Exception as e:
            return f"❌ Erreur fermeture {instrument} : {e}"

    def _force_be(self, instrument: str) -> str:
        """Active manuellement le Break-Even sur une position Capital.com."""
        state = self.capital_trades.get(instrument)
        if state is None:
            return f"⚠️ Aucune position ouverte sur {instrument}"
        entry = state.get("entry", 0)
        refs  = state.get("refs", [])
        ok_count = 0
        for ref in refs[1:]:   # TP2 + TP3
            if ref:
                try:
                    if self.capital.modify_position_stop(ref, entry):
                        ok_count += 1
                except Exception:
                    pass
        name = CAPITAL_NAMES.get(instrument, instrument)
        return f"✅ BE activé sur {name} ({ok_count} positions)" if ok_count else f"❌ BE échec {name}"

    def _send_daily_report(self) -> None:
        """
        Sprint 5 — Rapport visuel journalier.
        Génère un PNG 2 panneaux via matplotlib :
          1. Barres PnL par instrument (session du jour)
          2. Heatmap instrument × heure UTC
        Envoyé via Telegram sendPhoto à 20h UTC.
        """
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np

        try:
            balance = self.capital.get_balance() if self.capital.available else 0.0
            pnl_total = round(balance - self.initial_balance, 2) if balance > 0 else 0.0
            wins  = sum(1 for t in self._capital_closed_today if t.get("pnl", 0) > 0)
            total = len(self._capital_closed_today)
            wr    = round(wins / total * 100, 1) if total else 0.0

            # ─ 1. PnL par instrument ─────────────────────────────────────────
            pnl_by_inst: dict = {}
            for t in self._capital_closed_today:
                sym = t.get("instrument", t.get("symbol", "?"))
                pnl_by_inst[sym] = pnl_by_inst.get(sym, 0) + t.get("pnl", 0)

            # ─ 2. Heatmap data (instrument × heure) ─────────────────────────
            instruments = list(CAPITAL_INSTRUMENTS)
            hours = list(range(7, 21))  # 7h-20h UTC (sessions actives)
            heat_matrix = np.zeros((len(instruments), len(hours)))
            for i, inst in enumerate(instruments):
                for j, h in enumerate(hours):
                    pnls = self._heatmap_data.get(inst, {}).get(h, [])
                    heat_matrix[i, j] = sum(pnls) if pnls else 0.0

            # ─ Figure ────────────────────────────────────────────────────────
            fig = plt.figure(figsize=(14, 8), facecolor="#060911")

            # Panneau 1 : barres PnL
            ax1 = fig.add_subplot(2, 1, 1)
            ax1.set_facecolor("#0d1220")
            if pnl_by_inst:
                labels = list(pnl_by_inst.keys())
                values = list(pnl_by_inst.values())
                colors = ["#22d3a0" if v >= 0 else "#ff4f6e" for v in values]
                bars = ax1.bar(labels, values, color=colors, edgecolor="#1e2a45", linewidth=0.8)
                ax1.axhline(0, color="#5a6a8a", linewidth=0.8, linestyle="--")
                for bar, val in zip(bars, values):
                    ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01,
                             f"{val:+.2f}€", ha="center", va="bottom" if val >= 0 else "top",
                             color="#c8d6f0", fontsize=8)
            ax1.set_title(
                f"📊 NEMESIS — Rapport Journalier {datetime.now(timezone.utc).strftime('%d/%m/%Y')} | "
                f"PnL: {pnl_total:+.2f}€ | WR: {wr:.0f}% | {total} trades",
                color="#c8d6f0", fontsize=10, pad=8
            )
            ax1.set_ylabel("PnL (€)", color="#5a6a8a", fontsize=9)
            ax1.tick_params(colors="#5a6a8a", labelsize=8)
            for spine in ax1.spines.values():
                spine.set_color("#1e2a45")

            # Panneau 2 : Heatmap
            ax2 = fig.add_subplot(2, 1, 2)
            ax2.set_facecolor("#0d1220")
            cmap = mcolors.LinearSegmentedColormap.from_list(
                "nemesis", ["#ff4f6e", "#141a2e", "#22d3a0"]
            )
            abs_max = max(abs(heat_matrix).max(), 0.01)
            im = ax2.imshow(heat_matrix, cmap=cmap, aspect="auto",
                            vmin=-abs_max, vmax=abs_max)
            ax2.set_xticks(range(len(hours)))
            ax2.set_xticklabels([f"{h}h" for h in hours], color="#5a6a8a", fontsize=7)
            ax2.set_yticks(range(len(instruments)))
            ax2.set_yticklabels(instruments, color="#c8d6f0", fontsize=8)
            ax2.set_title("🔥 Heatmap Performance (Instrument × Heure UTC)", color="#c8d6f0", fontsize=9)
            for i in range(len(instruments)):
                for j in range(len(hours)):
                    val = heat_matrix[i, j]
                    if val != 0:
                        ax2.text(j, i, f"{val:+.1f}", ha="center", va="center",
                                 color="white", fontsize=6, fontweight="bold")

            plt.tight_layout(pad=1.5)

            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
            plt.close(fig)
            buf.seek(0)

            caption = (
                f"📊 <b>Rapport Journalier Nemesis</b>\n"
                f"💰 PnL total : <b>{pnl_total:+.2f}€</b>\n"
                f"🎯 Win Rate  : <b>{wr:.0f}%</b> ({wins}/{total} trades)\n"
                f"📅 {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M')} UTC"
            )
            self.telegram.send_photo(buf.read(), caption=caption)
            logger.info("📊 Rapport journalier PNG envoyé via Telegram")
        except Exception as _rp_e:
            logger.error(f"❌ Daily report: {_rp_e}")

    def _do_pause(self) -> str:

        """Met le bot en pause manuelle."""
        self._manual_pause = True
        return "⏸️ Bot mis en pause."

    def _do_resume(self) -> str:
        """Reprend le trading après pause manuelle."""
        self._manual_pause = False
        return "▶️ Trading repris."

    def _do_brief(self) -> str:
        """Envoie la matinale à la demande."""
        try:
            import threading
            balance = self.capital.get_balance() if self.capital.available else 0.0
            _, reason = self.calendar.should_pause_trading()
            brief = self.context.build_morning_brief(balance, reason or None)
            self.telegram.notify_morning_brief(brief, nb_instruments=len(CAPITAL_INSTRUMENTS))
            return "☀️ Matinale envoyée."
        except Exception as e:
            return f"❌ Matinale : {e}"

    def _do_backtest(self, symbol: str = None, days: int = 30) -> str:
        """Lance un backtest rapide en arrière-plan."""
        try:
            import threading, subprocess, sys
            _days = str(days) if days else "30"
            def _run():
                result = subprocess.run(
                    [sys.executable, "backtester_oanda.py", "--days", _days],
                    capture_output=True, text=True, timeout=120
                )
                if result.returncode == 0:
                    self.telegram.send_message("✅ <b>Backtest terminé</b>\n" + result.stdout[-1000:])
                else:
                    self.telegram.send_message(f"❌ Backtest erreur:\n{result.stderr[:500]}")
            threading.Thread(target=_run, daemon=True).start()
            return "⏳ Backtest lancé..."
        except Exception as e:
            return f"❌ Backtest : {e}"

    # ── Sprint 3 : Commandes premium Telegram ─────────────────────────────────

    def _cmd_best_pair(self) -> str:
        """Retourne l'instrument le plus profitable sur la session courante."""
        pnl_by_inst: dict = {}
        for t in self._capital_closed_today:
            sym = t.get("symbol", "?")
            pnl_by_inst[sym] = pnl_by_inst.get(sym, 0) + t.get("pnl", 0)
        if not pnl_by_inst:
            return (
                "🏆 <b>Meilleur Instrument</b>\n"
                "<code>Aucun trade fermé aujourd'hui.</code>"
            )
        ranked = sorted(pnl_by_inst.items(), key=lambda x: x[1], reverse=True)
        lines = "\n".join(
            f"  {'🥇' if i==0 else '🥈' if i==1 else '🥉' if i==2 else '  '}"
            f" {sym}: <b>{pnl:+.2f}€</b>"
            for i, (sym, pnl) in enumerate(ranked)
        )
        winner = ranked[0]
        return (
            f"🏆 <b>Meilleur Instrument — {winner[0]}</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{lines}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>"
        )

    def _cmd_risk(self) -> str:
        """Résumé de l'exposition et du drawdown actuel."""
        balance = self.capital.get_balance() if self.capital.available else 0.0
        open_count = sum(1 for s in self.capital_trades.values() if s is not None)
        daily_dd = 0.0
        if self._daily_start_balance > 0 and balance > 0:
            daily_dd = (self._daily_start_balance - balance) / self._daily_start_balance * 100
        monthly_dd = 0.0
        if self._monthly_start_balance > 0 and balance > 0:
            monthly_dd = (self._monthly_start_balance - balance) / self._monthly_start_balance * 100
        paused_str = "⏸️ PAUSED" if self._dd_paused or self._manual_pause else "🟢 ACTIF"
        return (
            f"🛡️ <b>Risk Summary</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Statut       : {paused_str}\n"
            f"  Balance      : {balance:,.2f}€\n"
            f"  Positions    : {open_count}/{MAX_OPEN_TRADES}\n"
            f"  DD Journalier: {daily_dd:+.2f}% (limite {self.DAILY_DD_LIMIT:.0f}%)\n"
            f"  DD Mensuel   : {monthly_dd:+.2f}% (10%=48h | 15%=stop)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>"
        )

    def _cmd_regime(self) -> str:
        """Retourne le régime HMM pour chaque instrument actif."""
        REGIME_EMOJI = {0: "⬛ RANGING", 1: "🟢 TREND_UP", 2: "🔴 TREND_DOWN"}
        lines = []
        for inst in CAPITAL_INSTRUMENTS:
            try:
                df = self.capital.fetch_ohlcv(inst, timeframe="5m", count=50)
                if df is None or len(df) < 20:
                    lines.append(f"  {inst}: <i>données insuffisantes</i>")
                    continue
                df = self.strategy.compute_indicators(df)
                res = self.hmm.detect_regime(df, symbol=inst)
                regime_name = REGIME_EMOJI.get(res["regime"], res["name"])
                conf = res["confidence"]
                lines.append(f"  {inst}: {regime_name} ({conf:.0%})")
            except Exception as e:
                lines.append(f"  {inst}: ⚠️ {str(e)[:30]}")
        body = "\n".join(lines)
        return (
            f"🧠 <b>Régimes HMM</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{body}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>"
        )

    # ──────────────────────────────────────────────────────────────────────────


    def _status_text(self) -> str:
        balance = self.capital.get_balance() if self.capital.available else 0.0
        pnl_total = round(balance - self.initial_balance, 2) if balance > 0 else 0.0
        pnl_pct   = (pnl_total / self.initial_balance * 100) if self.initial_balance > 0 else 0.0
        bal_str   = f"{balance:,.2f}€"

        paused = "⏸️ PAUSED" if (self._manual_pause or self.handler.is_paused()) else "🟢 ACTIF"

        # Positions Capital.com avec PnL temps réel
        cap_lines = ""
        cap_open  = 0
        total_unrealized = 0.0
        for epic, state in self.capital_trades.items():
            if state is None:
                continue
            cap_open += 1
            name  = CAPITAL_NAMES.get(epic, epic)
            entry = state.get("entry", 0.0)
            direction = state.get("direction", "?")
            tp1_icon  = "✅" if state.get("tp1_hit") else "○"
            # PnL non-réalisé live
            unrealized = 0.0
            try:
                px = self.capital.get_current_price(epic)
                if px:
                    mid = px["mid"]
                    unrealized = round((mid - entry) * (1 if direction == "BUY" else -1) * 3, 2)
                    total_unrealized += unrealized
            except Exception:
                pass
            pnl_icon = "🟢" if unrealized >= 0 else "🔴"
            cap_lines += (
                f"  • <b>{name}</b> {direction} | éntrée: <code>{entry:.5f}</code> "
                f"| PnL: {pnl_icon} <b>{unrealized:+.2f}€</b> TP1{tp1_icon}\n"
            )

        # Equity curve stats
        equity_pct = self.equity.total_pnl_pct()
        max_dd     = self.equity.max_drawdown()
        cb_status  = "🔴 Sous MA20" if self.equity.is_below_ma() else "🟢 OK"

        ctx = self.context.get_context_line() if hasattr(self.context, 'get_context_line') else ""
        return (
            f"⚡ <b>NEMESIS — Statut</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance : <b>{bal_str}</b>\n"
            f"  PnL total  : <b>{pnl_total:+.2f}€ ({pnl_pct:+.1f}%)</b>\n"
            f"  Non-réalisé : <b>{total_unrealized:+.2f}€</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Positions ouvertes : <b>{cap_open}/{len(CAPITAL_INSTRUMENTS)}</b>\n"
            f"{cap_lines}"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Equity : PnL={equity_pct:+.1f}%  MaxDD={max_dd:.1f}%  CB={cb_status}\n"
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

        # Aucune position ouverte

        if not lines:
            return "📋 <b>Aucune position ouverte.</b>", None

        text   = "📋 <b>Positions actives :</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n" + "\n\n".join(lines)
        markup = TelegramBotHandler.trade_keyboard(markup_epic) if markup_epic else None
        return text, markup


if __name__ == "__main__":
    bot = TradingBot()

    # TASK-017 : SIGTERM handler Railway — sauvegarde l'équity curve avant shutdown
    def _sigterm_handler(signum, frame):
        logger.info("🛑 SIGTERM reçu — sauvegarde en cours...")
        try:
            bot.equity._save()
            logger.info("✅ equity_history.json sauvegardé.")
        except Exception as _ex:
            logger.warning(f"⚠️ Sauvegarde equity échouée : {_ex}")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    bot.run()
