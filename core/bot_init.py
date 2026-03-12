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
            # Validation optionnelle : VALIDATE_EPICS=1 dans le .env local
            if os.environ.get("VALIDATE_EPICS", "0") == "1":
                self.capital.validate_epics()
        else:
            logger.info("⚠️  Capital.com non configuré — vérifier CAPITAL_API_KEY / EMAIL / PASSWORD dans le .env")


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
        self.DAILY_DD_LIMIT       = float(os.getenv("DAILY_DD_LIMIT", "10.0"))  # 10% (was 3% — too tight)
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
        self._capital_closed_month: list = []  # F-6: monthly leaderboard data
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

        # ─── 3 Modules Institutionnels ────────────────────────────────────
        self.rate_limiter = get_rate_limiter()           # Module 1: Rate-Limit Guardian
        self.quarantine   = AssetQuarantine(             # Module 2: Dynamic Blacklist
            db=self.db,
            telegram_router=self.telegram.router if self.telegram else None,
        )
        self.eod = EoDReconciliation(                    # Module 3: EoD Reconciliation
            capital=self.capital,
            db=self.db,
            quarantine=self.quarantine,
            telegram_router=self.telegram.router if self.telegram else None,
        )

        # ─── 3 Moteurs Intelligence Adaptative ───────────────────────────────
        self.vol_adjuster = VolAdjuster()                # Moteur 1: Volatility TP/SL
        self.ob_guard     = OrderBookGuard(self.capital) # Moteur 2: OrderBook Imbalance
        self.shadow       = ShadowEngine(                # Moteur 3: Shadow Trading
            db=self.db,
            capital_client=self.capital,
        )

        # ─── Audit Quantitatif Go-Live ────────────────────────────────────────
        tg_router = self.telegram.router if self.telegram else None
        self.slippage  = SlippageInjector()              # Étape 1: Reality Slippage
        self.latency   = LatencyTracker(tg_router)       # Étape 2: Latency Tracker
        self.golive    = GoLiveChecker(                  # Étape 3: Go-Live Checklist
            db=self.db,
            rate_limiter=self.rate_limiter,
            telegram_router=tg_router,
        )

        # ─── Moteurs Quantitatifs Avancés ─────────────────────────────────────
        self.ml_engine   = MLEngine(db=self.db)              # Moteur 4: ML Score
        self.alt_data    = AltDataEngine(tg_router)           # Moteur 5: Sentiment
        self.pairs       = PairsTrader(                       # Moteur 6: Stat-Arb
            capital_client=self.capital,
            ohlcv_cache=None,   # sera injecté après OHLCVCache warmup
            db=self.db,
            telegram_router=tg_router,
        )
        self.pairs.start()  # démarre le daemon thread de scan
        self.smart_router = SmartRouter(                      # Moteur 7: TWAP/Iceberg
            capital_client=self.capital,
            db=self.db,
            telegram_router=tg_router,
        )
        self.health = HealthCheck(                            # DevOps: Health Check
            capital=self.capital,
            db=self.db,
            rate_limiter=self.rate_limiter,
            telegram_router=tg_router,
        )
        # Health check au démarrage (non bloquant)
        import threading as _thr
        _thr.Thread(target=self.health.run, daemon=True, name="startup_healthcheck").start()

        self.latency = LatencyTracker(                        # Étape 2: Latency Tracker
            telegram_router=tg_router,
        )

        self.golive = GoLiveChecker(                          # Étape 3: Go-Live Checklist
            db=self.db,
            rate_limiter=self.rate_limiter,
            telegram_router=tg_router,
        )



        # ─── Singularité Algorithmique ────────────────────────────────────────
        self.vpin    = VPINGuard(                           # Moteur 9: VPIN Toxicity
            capital_client=self.capital,
            capital_trades_ref=self.capital_trades,
            db=self.db,
            telegram_router=tg_router,
            close_fn=None,   # sera injecté après bot_monitor init
        )
        self.vpin.ensure_table()
        self.vpin.start()

        self.hmm     = HMMPortfolio(                       # Moteur 10: HMM + BL
            ohlcv_cache=None,  # injecté après OHLCVCache warmup
            db=self.db,
            telegram_router=tg_router,
            asset_profiles=ASSET_PROFILES,
        )
        self.hmm.start()

        self.rl      = RLAgent(                            # Moteur 8: RL DQN
            db=self.db,
            telegram_router=tg_router,
        )

        self.meta    = MetaAgent(                          # Moteur 13: Cerveau du Cerveau
            db=self.db,
            telegram_router=tg_router,
        )
        self.meta.ensure_table()

        self.mev     = MEVShield()                         # Moteur 12: Anti-FrontRun

        # Moteur 11: Memory Pool (GC tuning + buffers numpy) — singleton global
        self.mem     = MEMORY_POOL if MEMORY_POOL else MemoryPool.get_instance()

        # CRON trackers
        self._last_eod_date             = None   # date de dernier audit EoD
        self._last_quarantine_refresh   = datetime.now(timezone.utc)  # refresh 15min

        # ─── Infrastructure Locale (Moteurs 21-22) ────────────────────────
        self.network = NetworkResilience(               # Moteur 21: WiFi Resilience
            capital_client=self.capital,
            db=self.db,
            telegram_router=tg_router,
        )
        self.network.start()

        self.sleep_guard = SleepGuard(                  # Moteur 22: Mac Sleep Guard
            capital_client=self.capital,
            db=self.db,
            telegram_router=tg_router,
            capital_trades_ref=self.capital_trades,
        )
        self.sleep_guard.ensure_table()
        self.sleep_guard.start()

        # ─── Predator Tier (Moteurs 23-25) ────────────────────────────────
        self.onchain_gnn = OnChainGNN(                 # Moteur 23: On-Chain GNN
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.onchain_gnn.start()

        self.algo_hunter = AlgoHunter(                 # Moteur 24: Adversarial AI
            db=self.db,
            capital_client=self.capital,
            capital_ws=self.capital_ws,
            telegram_router=tg_router,
        )
        self.algo_hunter.start()

        self.vol_surface = VolSurface(                 # Moteur 25: Greeks Arb
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.vol_surface.start()

        # ─── Olympe Tier (Moteurs 26-28) ─────────────────────────────────
        self.macro_nlp = MacroNLP(                     # Moteur 26: Macro NLP
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.macro_nlp.start()

        self.swarm = SwarmIntelligence(                # Moteur 27: Swarm Intel
            db=self.db,
            capital_client=self.capital,
            capital_ws=self.capital_ws,
            telegram_router=tg_router,
            instruments=list(CAPITAL_INSTRUMENTS),
        )
        self.swarm.start()

        self.synth_router = SyntheticRouter(           # Moteur 28: Tri-Arb
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.synth_router.start()

        # ─── God Tier (Moteurs 29-31) ────────────────────────────────────
        self.tda = TDAEngine(                          # Moteur 29: TDA
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.tda.start()

        self.flash_loan = FlashLoanEngine(             # Moteur 30: Flash Loans
            db=self.db,
            capital_client=self.capital,
            onchain_gnn=self.onchain_gnn,
            synthetic_router=self.synth_router,
            telegram_router=tg_router,
        )
        self.flash_loan.start()

        self.zerocopy = ZeroCopyEngine(                # Moteur 31: Zero-Copy
            db=self.db,
            instruments=list(CAPITAL_INSTRUMENTS),
        )
        self.zerocopy.start()

        # ─── Singularity Tier (Moteurs 32-34) ───────────────────────────
        self.quantum = QuantumTensorEngine(             # Moteur 32: Quantum
            db=self.db,
            capital_client=self.capital,
            telegram_router=tg_router,
        )
        self.quantum.start()

        self.dark_forest = DarkForestMEV(               # Moteur 33: MEV
            db=self.db,
            flash_loan_engine=self.flash_loan,
            telegram_router=tg_router,
        )
        self.dark_forest.start()

        self.hdc = HDCMemory(                           # Moteur 34: HDC
            db=self.db,
            capital_client=self.capital,
            macro_nlp=self.macro_nlp,
            tda_engine=self.tda,
            swarm=self.swarm,
            telegram_router=tg_router,
        )
        self.hdc.start()

        # ─── Consciousness Tier (Moteurs 35-37) ─────────────────────────
        self.ast_mutator = SelfRewritingKernel(         # Moteur 35: AST
            db=self.db,
            algo_hunter=self.algo_hunter,
            telegram_router=tg_router,
        )
        self.ast_mutator.start()

        self.cfr = CFREngine(                           # Moteur 36: CFR
            db=self.db,
            capital_client=self.capital,
            quantum_engine=self.quantum,
            telegram_router=tg_router,
        )
        self.cfr.start()

        self.fpga = VirtualFPGA(                        # Moteur 37: FPGA
            db=self.db,
        )
        self.fpga.start()

        # BUG FIX #C : Le refresh calendrier se fait en thread daemon (non bloquant)

        # BUG FIX #C : Le refresh calendrier se fait en thread daemon (non bloquant)
        self.calendar.start_background_refresh()

        # ─── TradingView Webhook (opt-in) ─────────────────────────────────
        if WEBHOOK_OK:
            self._webhook = get_webhook_server()
            self._webhook.start()
            logger.info("📡 Webhook TradingView actif")
        else:
            self._webhook = None

        # ─── Log IP locale ────────────────────────────────────────────────
        try:
            import socket as _s
            _ip = _s.gethostbyname(_s.gethostname())
            logger.info(f"🏠 IP locale Docker: {_ip}")
        except Exception:
            pass

        # ─── Callbacks Telegram ───────────────────────────────────────────
        self.handler.register_callbacks(
            pause        = self._do_pause,
            resume       = self._do_resume,
            get_hub_data = self._hub_data,
            stats        = self._cmd_stats,
            performance  = self._cmd_performance,
            health       = self._cmd_health,
            golive       = self._cmd_golive,
            latency      = self._cmd_latency,
            achievements = lambda: self.telegram.gamification.format_achievements_block() if self.telegram.gamification else "⚠️ Gamification not available",
        )

        self.handler.start_polling()

        # ─── Restauration BDD ─────────────────────────────────────────────
        self._restore_from_db()

        # ─── A-2: OHLCV Cache (warmup 200 bougies par instrument) ─────────
        self.ohlcv_cache = OHLCVCache(self.capital)
        if self.capital.available:
            self.ohlcv_cache.warmup(CAPITAL_INSTRUMENTS, ASSET_PROFILES, strategy=self.strategy)
        # Injection tardive: le cache est maintenant disponible
        if hasattr(self, 'pairs'):
            self.pairs._cache = self.ohlcv_cache
        if hasattr(self, 'hmm'):
            self.hmm._cache = self.ohlcv_cache

        # ─── Moteurs Leviathan (14-16) ────────────────────────────────────
        self.spatial_arb = SpatialArbEngine(           # Moteur 14: Cross-Exchange Arb
            db=self.db,
            telegram_router=tg_router,
        )
        self.spatial_arb.start()

        self.market_maker = MarketMaker(               # Moteur 15: Avellaneda-Stoikov MM
            capital_client=self.capital,
            db=self.db,
            telegram_router=tg_router,
            ohlcv_cache=self.ohlcv_cache,
        )
        self.market_maker.start()

        self.cluster = ClusterManager(                 # Moteur 16: Failover State Machine
            db=self.db,
            telegram_router=tg_router,
            capital_client=self.capital,
            arb_engine=self.spatial_arb,
        )
        self.cluster.start()

        # ─── A-4: Register WS breakout callback ──────────────────────────
        if hasattr(self, 'capital_ws') and self.capital_ws:
            self.capital_ws.register_breakout_callback(self._on_ws_breakout)

        # ─── S-4: ML Scorer (self-learning) ──────────────────────────────
        self.ml_scorer = MLScorer()

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

        # ─── LIVE SYNC: fetch actual open positions from Capital.com ──────────
        # Prevents re-entering same instrument after restart when DB is empty.
        try:
            live_positions = self.capital.get_open_positions()
            # Group by epic — mark each open epic so bot won't re-open
            open_epics = set()
            epic_to_ref  = {}  # epic → dealId of first position found
            epic_to_data = {}  # epic → basic state dict

            for pos in live_positions:
                p = pos.get("position", {})
                m = pos.get("market", {})
                epic = m.get("epic", "")
                if not epic:
                    continue
                open_epics.add(epic)
                deal_id   = p.get("dealId", "")
                direction = p.get("direction", "BUY")
                entry     = float(p.get("level", 0))
                sl        = float(p.get("stopLevel", 0)) or entry * (1.02 if direction == "BUY" else 0.98)
                tp        = float(p.get("limitLevel", 0)) or entry * (0.98 if direction == "BUY" else 1.02)
                size      = float(p.get("size", 0))

                if epic not in epic_to_ref:
                    epic_to_ref[epic] = deal_id
                    epic_to_data[epic] = {
                        "direction": direction,
                        "entry":     entry,
                        "sl":        sl,
                        "tp1":       tp,
                        "tp2":       tp,
                        "tp3":       tp,
                        "refs":      [deal_id, None, None],
                        "size":      size,
                        "open_time": datetime.now(timezone.utc),
                        "tp1_hit":   False,
                        "tp2_hit":   False,
                        "score":     0,
                        "confirmations": [],
                        "regime":    "RANGING",
                        "in_overlap": False,
                        "adx_at_entry": 0,
                        "ab_variant": "A",
                        "_live_synced": True,  # marker
                    }

            # Apply live state: only for instruments NOT already restored from DB
            synced = 0
            for epic, state in epic_to_data.items():
                if epic in CAPITAL_INSTRUMENTS and self.capital_trades.get(epic) is None:
                    self.capital_trades[epic] = state
                    synced += 1
                    logger.info(f"🔄 Live sync: {epic} {state['direction']} @ {state['entry']} déjà ouvert → capital_trades restauré")

            if synced:
                logger.warning(f"⚠️ Live sync: {synced} position(s) restaurée(s) depuis Capital.com (DB était vide)")
            elif open_epics:
                logger.info(f"✅ Live sync: {len(open_epics)} position(s) déjà dans capital_trades (DB OK)")
            else:
                logger.info("✅ Live sync: aucune position ouverte sur Capital.com")

        except Exception as _ls_e:
            logger.error(f"❌ Live sync positions: {_ls_e}")

        # C-4: Restore dd_paused state
        try:
            dd_state = self.db.load_bot_state("dd_paused", "0")
            dd_date  = self.db.load_bot_state("dd_paused_date", "")
            today_str = datetime.now(timezone.utc).date().isoformat()
            if dd_state == "1" and dd_date == today_str:
                self._dd_paused = True
                logger.warning("🚨 DD pause restaurée depuis Supabase — trading suspendu")
            elif dd_state == "1":
                # Previous day → clear
                self.db.save_bot_state("dd_paused", "0")
                logger.info("🟢 DD pause expirée (jour précédent) — trading actif")
        except Exception:
            pass

