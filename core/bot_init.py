"""
bot_init.py — TradingBot.__init__ et _restore_from_db
"""
from .imports import *


class BotInitMixin:
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
            # Validation optionnelle : VALIDATE_EPICS=1 dans env vars Railway
            if os.environ.get("VALIDATE_EPICS", "0") == "1":
                self.capital.validate_epics()
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
        if WEBHOOK_OK:
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
            # Hub pages v3.0
            get_dashboard  = self._hub_dashboard_page,
            get_risk_page  = self._hub_risk_page,
            get_regime_page= self._hub_regime_page,
            get_stats_page = self._hub_stats_page,
            get_hub_data   = self._hub_data,
        )

        self.handler.start_polling()

        # ─── Restauration BDD ─────────────────────────────────────────────
        self._restore_from_db()

        self.calendar.refresh()
        start_bal = self.capital.get_balance() if self.capital.available else 0.0
        self.telegram.notify_start(start_bal, CAPITAL_INSTRUMENTS)
        logger.info(f"💰 Solde initial Capital.com : {start_bal:.2f}€")

        # ─── Dashboard Web ────────────────────────────────────────────────
        if DASHBOARD_OK and os.getenv("DASHBOARD_ENABLED", "true").lower() == "true":
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
                    "tp1_hit":   bool(t_dict.get("tp1_hit", False)),
                    "tp2_hit":   bool(t_dict.get("tp2_hit", False)),
                    # Champs requis par bot_monitor / bot_signals (defaults sûrs)
                    "score":         t_dict.get("score", 0),
                    "confirmations": t_dict.get("confirmations", []),
                    "regime":        t_dict.get("regime", "RANGING"),
                    "fear_greed":    t_dict.get("fear_greed"),
                    "in_overlap":    t_dict.get("in_overlap", False),
                    "adx_at_entry":  t_dict.get("adx_at_entry", 0),
                    "open_time":     datetime.now(timezone.utc),  # approximatif post-restart
                    "ab_variant":    t_dict.get("ab_variant", "A"),
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
