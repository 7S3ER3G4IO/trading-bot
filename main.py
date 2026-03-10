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

        # ─── État Capital.com ─────────────────────────────────────────────
        self.capital_trades: Dict[str, Optional[dict]] = {s: None for s in CAPITAL_INSTRUMENTS}
        self._capital_closed_today: list = []
        self._london_tracker = SessionTracker()
        self._ny_tracker     = SessionTracker()
        self._last_dashboard_day: Optional[date] = None

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
            dash_update(
                balance=balance, initial=self.initial_balance,
                pnl_total=pnl_total_real,
                pnl_today=round(pnl_today, 2),
                trades=open_trades, wr_overall=round(wr, 1),
                n_total=total, symbols=list(CAPITAL_INSTRUMENTS),
                paused=self._manual_pause, futures_balance=0.0,
                max_slots=MAX_OPEN_TRADES,
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

        # ─── Moteur de trading Capital.com ───────────────────────────────────

        # Pause manuelle ou drawdown
        if self._manual_pause or self._dd_paused:
            return

        # Capital.com non disponible → rien à faire
        if not self.capital.available:
            return

        # ── Surveillance des positions ouvertes ──────────────────────────
        self._monitor_capital_positions()

        # ── Vérification session London/NY (08h-10h30 / 13h30-16h UTC) ──
        if not self.strategy.is_session_ok():
            return

        # ── Pause calendrier économique ───────────────────────────────────
        should_pause, reason = self.calendar.should_pause_trading()
        if should_pause:
            logger.debug(f"📅 Trading suspendu : {reason}")
            return

        # ── Limite corrélation (max 2 CFD simultanées) ───────────────────────
        active_count = sum(1 for s in self.capital_trades.values() if s is not None)
        if active_count >= 2:
            return  # Plafond atteint — on surveille mais on n'ouvre rien

        # ── Scan des instruments Capital.com ─────────────────────────────────
        balance = self.capital.get_balance()
        if balance <= 0:
            return

        per_instrument = balance / len(CAPITAL_INSTRUMENTS)

        for instrument in CAPITAL_INSTRUMENTS:
            # Ne pas ouvrir si limite atteinte entre deux itérations
            if sum(1 for s in self.capital_trades.values() if s is not None) >= 2:
                break
            try:
                self._process_capital_symbol(instrument, per_instrument)
            except Exception as e:
                logger.error(f"❌ _process_capital_symbol {instrument} : {e}")

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
            return

        df  = self.strategy.compute_indicators(df)
        sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument)
        if sig == "HOLD":
            return

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

        # ── Dashboard : enregistre l'ouverture ─────────────────────────────
        try:
            dash_open(symbol=CAPITAL_NAMES.get(instrument, instrument),
                      side=direction, entry=entry, qty=size1)
        except Exception:
            pass

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
