"""
prometheus_metrics.py — Serveur HTTP Prometheus (port 9090)

Expose les métriques du bot pour Prometheus/Grafana scraping.
Différent de prometheus_core.py (= trade journal Project Prometheus).
"""
import os
import threading
from loguru import logger

# ─── Prometheus client library ─────────────────────────────────────────────────
try:
    from prometheus_client import (
        Gauge, Counter, Histogram, start_http_server,
        REGISTRY, CollectorRegistry
    )
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False
    logger.warning("⚠️ prometheus_client non installé — métriques HTTP désactivées")
    logger.warning("   pip install prometheus_client")

PROM_PORT = int(os.getenv("PROMETHEUS_PORT", "9090"))

# ─── Métriques définies ────────────────────────────────────────────────────────
if _PROM_AVAILABLE:
    # Balance & PnL
    nemesis_balance         = Gauge("nemesis_balance_usd",         "Balance du compte en $")
    nemesis_initial_balance = Gauge("nemesis_initial_balance_usd",  "Balance initiale du compte")
    nemesis_pnl_total       = Gauge("nemesis_pnl_total_pct",        "PnL total en % depuis le début")
    nemesis_pnl_daily       = Gauge("nemesis_pnl_daily_usd",        "PnL journalier en $")
    nemesis_hwm             = Gauge("nemesis_high_water_mark_usd",   "High Water Mark en $")

    # Positions
    nemesis_open_positions  = Gauge("nemesis_open_positions",        "Nombre de positions ouvertes")
    nemesis_max_positions   = Gauge("nemesis_max_positions",         "Nombre max de positions autorisées")

    # Drawdown
    nemesis_daily_dd_pct    = Gauge("nemesis_daily_drawdown_pct",    "Drawdown journalier en %")
    nemesis_monthly_dd_pct  = Gauge("nemesis_monthly_drawdown_pct",  "Drawdown mensuel en %")
    nemesis_dd_paused       = Gauge("nemesis_dd_paused",             "1 si trading suspendu par DD")

    # Challenge Prop Firm
    nemesis_challenge_pct   = Gauge("nemesis_challenge_progress_pct", "Progression vers l'objectif +10%")
    nemesis_challenge_goal  = Gauge("nemesis_challenge_goal_pct",     "Objectif Prop Firm (%)")

    # Signaux & trades
    nemesis_signals_today   = Gauge("nemesis_signals_today",          "Nombre de signaux générés aujourd'hui")
    nemesis_trades_today    = Gauge("nemesis_trades_today",           "Nombre de trades fermés aujourd'hui")
    nemesis_wins_today      = Gauge("nemesis_wins_today",             "Nombre de trades gagnants aujourd'hui")
    nemesis_win_rate_today  = Gauge("nemesis_win_rate_today",         "Win rate journalier (0-1)")
    nemesis_trades_counter  = Counter("nemesis_trades_total",         "Total de trades depuis le démarrage")

    # MT5 connexion
    nemesis_mt5_connected   = Gauge("nemesis_mt5_connected",          "1 si MT5 connecté")
    nemesis_mt5_balance     = Gauge("nemesis_mt5_balance_usd",        "Balance MT5 en direct")

    # Slippage
    nemesis_avg_slippage    = Gauge("nemesis_avg_slippage_pips",      "Slippage moyen sur les 5 derniers trades")

    # Fear & Greed
    nemesis_fear_greed      = Gauge("nemesis_fear_greed_index",       "Fear & Greed index (0-100)")

    # Performance loop
    nemesis_scan_duration_s = Gauge("nemesis_scan_duration_seconds",  "Durée du dernier scan en secondes")
    nemesis_last_scan_ts    = Gauge("nemesis_last_scan_timestamp",    "Timestamp du dernier scan")


class PrometheusMetricsServer:
    """
    Serveur HTTP Prometheus pour les métriques NEMESIS.
    Démarré en thread background sur le port PROM_PORT (défaut: 9090).
    """

    _instance = None

    def __init__(self, bot=None):
        self._bot = bot
        self._started = False
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = PrometheusMetricsServer()
        return cls._instance

    def attach_bot(self, bot):
        """Attache le bot pour accès aux métriques live."""
        self._bot = bot

    def start(self):
        """Démarre le serveur HTTP Prometheus en background."""
        if not _PROM_AVAILABLE:
            return
        with self._lock:
            if self._started:
                return
            try:
                start_http_server(PROM_PORT)
                self._started = True
                logger.info(f"📡 Prometheus metrics : http://0.0.0.0:{PROM_PORT}/metrics")
                # Lancer l'updater en thread daemon
                t = threading.Thread(target=self._update_loop, daemon=True, name="prom_updater")
                t.start()
            except Exception as e:
                logger.warning(f"⚠️ Prometheus server: {e}")

    def _update_loop(self):
        """Met à jour les métriques toutes les 15s."""
        import time
        while True:
            try:
                self._update_metrics()
            except Exception as e:
                logger.debug(f"Prometheus update: {e}")
            time.sleep(15)

    def _update_metrics(self):
        """Snapshot les valeurs du bot dans les métriques Prometheus."""
        if not _PROM_AVAILABLE or self._bot is None:
            return

        bot = self._bot

        # Balance & PnL
        try:
            bal = bot.broker.get_balance() if bot.broker.available else 0.0
            ini = getattr(bot, 'initial_balance', 100_000.0)
            hwm = getattr(bot, '_equity_hwm', ini)
            pnl_pct = (bal - ini) / ini * 100 if ini > 0 else 0.0
            pnl_day = bal - getattr(bot, '_daily_start_balance', ini)
            nemesis_balance.set(bal)
            nemesis_initial_balance.set(ini)
            nemesis_pnl_total.set(round(pnl_pct, 4))
            nemesis_pnl_daily.set(round(pnl_day, 2))
            nemesis_hwm.set(hwm)
        except Exception:
            pass

        # Positions
        try:
            open_pos = sum(1 for s in bot.positions.values() if s is not None)
            nemesis_open_positions.set(open_pos)
            from config import MAX_OPEN_TRADES
            nemesis_max_positions.set(MAX_OPEN_TRADES)
        except Exception:
            pass

        # DD
        try:
            dstart = getattr(bot, '_daily_start_balance', 1.0)
            bal2 = bot.broker.get_balance() if bot.broker.available else dstart
            dd_d = (dstart - bal2) / dstart * 100 if dstart > 0 else 0.0
            mstart = getattr(bot, '_monthly_start_balance', dstart)
            dd_m = (mstart - bal2) / mstart * 100 if mstart > 0 else 0.0
            nemesis_daily_dd_pct.set(max(dd_d, 0))
            nemesis_monthly_dd_pct.set(max(dd_m, 0))
            nemesis_dd_paused.set(1 if getattr(bot, '_dd_paused', False) else 0)
        except Exception:
            pass

        # Challenge
        try:
            c_pct = pnl_pct
            nemesis_challenge_pct.set(max(min(c_pct / 10.0 * 100, 100), 0))
            nemesis_challenge_goal.set(10.0)
        except Exception:
            pass

        # Trades journaliers
        try:
            trades = getattr(bot, '_capital_closed_today', [])
            n_trades = len(trades)
            n_wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
            wr = n_wins / n_trades if n_trades > 0 else 0.0
            nemesis_trades_today.set(n_trades)
            nemesis_wins_today.set(n_wins)
            nemesis_win_rate_today.set(round(wr, 4))
        except Exception:
            pass

        # MT5
        try:
            mt5_ok = hasattr(bot, 'mt5') and bot.mt5.available and bot.mt5._is_connected()
            nemesis_mt5_connected.set(1 if mt5_ok else 0)
            if mt5_ok:
                nemesis_mt5_balance.set(bot.mt5.get_balance() or 0.0)
        except Exception:
            pass

        # Slippage
        try:
            st = getattr(bot, 'slippage_tracker', None)
            if st:
                nemesis_avg_slippage.set(st.avg_slippage(window=5))
        except Exception:
            pass

        # Fear & Greed
        try:
            fg = getattr(bot.context, '_fg_value', 50) if hasattr(bot, 'context') else 50
            nemesis_fear_greed.set(fg)
        except Exception:
            pass


def install_prometheus(bot) -> PrometheusMetricsServer:
    """Initialise et démarre le serveur Prometheus pour le bot."""
    server = PrometheusMetricsServer.get_instance()
    server.attach_bot(bot)
    server.start()
    return server
