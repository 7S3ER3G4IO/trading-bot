"""
bot_tick.py — TradingBot.run() et _tick() — boucle principale
"""
from .imports import *


class BotTickMixin:
    # BUG FIX #N : _correlation_ok() supprimé — méthode morte jamais appelée.
    # La limite de 2 trades simultanés est déjà gérée dans _tick() ligne 345–347.

    # ─── Boucle principale ───────────────────────────────────────────────────

    def run(self):
        logger.info(f"⏱  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | CTRL+C pour arrêter\n")
        _err_count = 0
        _last_tick_ok = time.monotonic()  # Dead-man switch timer
        _DEAD_MAN_TIMEOUT = 300  # 5 minutes sans tick réussi = alerte critique

        while bot_running:
            try:
                # Dead-man switch check — AVANT le tick, sur le dernier tick réussi
                _elapsed = time.monotonic() - _last_tick_ok
                if _elapsed > _DEAD_MAN_TIMEOUT:
                    logger.critical(
                        f"🚨 DEAD-MAN SWITCH : {_elapsed:.0f}s depuis le dernier tick OK "
                        f"(limite={_DEAD_MAN_TIMEOUT}s) — boucle potentiellement bloquée !"
                    )
                self._tick()
                _err_count = 0
                _last_tick_ok = time.monotonic()  # Reset APRÈS tick réussi
                # Phase 1.1: Heartbeat ping after successful tick
                try:
                    self.watchdog.ping()
                except Exception:
                    pass
                # T1: State Sync — full broker ↔ local reconciliation every 5 min
                try:
                    if not hasattr(self, '_last_state_sync'):
                        self._last_state_sync = 0
                    if time.monotonic() - self._last_state_sync > 300:
                        self._last_state_sync = time.monotonic()
                        self.state_sync.reconcile(self.positions)
                        # T1b: Orphan position detector — appelé après chaque reconciliation
                        try:
                            self._detect_orphan_positions()
                        except Exception:
                            pass
                        # CHANTIER 3: Dead-Man Switch watchdog (vérifie que les ticks arrivent)
                        try:
                            self._check_dead_man_switch()
                        except Exception:
                            pass
                except Exception:
                    pass
                # T2: Monthly DD reset — le 1er de chaque mois à 01h UTC
                try:
                    _now_utc = datetime.now(timezone.utc)
                    if _now_utc.day == 1 and _now_utc.hour == 1:
                        if not hasattr(self, '_last_monthly_reset') or self._last_monthly_reset.month != _now_utc.month:
                            _fresh_bal = self.broker.get_balance() if self.broker.available else 0.0
                            if _fresh_bal > 0:
                                self.risk.reset_monthly(_fresh_bal)
                                self._last_monthly_reset = _now_utc
                except Exception:
                    pass
            except Exception as e:
                _err_count += 1
                # Dead-man check also on error path
                elapsed = time.monotonic() - _last_tick_ok
                if elapsed > _DEAD_MAN_TIMEOUT:
                    logger.critical(
                        f"🚨 DEAD-MAN SWITCH : {elapsed:.0f}s sans tick OK | "
                        f"erreur #{_err_count} : {e}"
                    )
                bal = 0.0
                try:
                    bal = self.broker.get_balance() if self.broker.available else 0.0
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

        # ─── HEARTBEAT — Dead-Man Switch pulse ───────────────────────────────
        # Mis à jour au début de chaque tick réussi. Utilisé par bot_monitor
        # pour détecter si le bot est freezé (aucun tick depuis >5min).
        self._last_tick_ts = time.monotonic()

        # ─── A-2: Refresh stale OHLCV cache (only expired instruments) ────
        try:
            self.ohlcv_cache.refresh_stale(CAPITAL_INSTRUMENTS, strategy=self.strategy)
        except Exception as _cache_e:
            logger.debug(f"OHLCVCache refresh: {_cache_e}")

        try:
            balance = self.broker.get_balance() if self.broker.available else 0.0
            open_trades = []
            for instr, state in self.positions.items():
                if state is None:
                    continue
                name  = CAPITAL_NAMES.get(instr, instr)
                entry = state.get("entry", 0.0)
                # PnL non-réalisé en temps réel (prix actuel vs entrée)
                unrealized_pnl = 0.0
                try:
                    px = (self.broker.get_current_price(instr)
                          if self.broker.available else None) \
                         or self.capital.get_current_price(instr)
                    if px:
                        mid = px["mid"]
                        direction = state.get("direction", "BUY")
                        n_refs = sum(1 for r in state.get("refs", []) if r)
                        unrealized_pnl = round((mid - entry) * (1 if direction == "BUY" else -1) * n_refs, 2)
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
            # ── Snapshot équité pour Chart.js (max 200 points) ────────────────
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
        # Reset polluted equity data once after fresh deploy
        if not getattr(self, '_equity_reset_done', False):
            self._equity_reset_done = True
            self.equity.reset_history(keep_last=1)
            self._dd_paused = False
            # Reset risk manager daily balance to current balance on fresh deploy
            self.risk.reset_daily(balance)
            self._daily_start_balance = balance  # Also reset the tick-level DD check
            self._equity_warmup_ticks = 0  # Skip equity circuit breaker for first 5 ticks
            # Reset compteur trades journaliers par instrument
            if hasattr(self, '_daily_inst_trades'):
                self._daily_inst_trades = {s: 0 for s in self._daily_inst_trades}
            logger.info(f"🔄 Equity curve + risk manager nettoyés (fresh deploy) — daily_start={balance:.2f}")

        if balance > 0:
            self.equity.record(balance)
            # Skip equity circuit breaker for first 5 ticks after deploy (data polluted)
            warmup = getattr(self, '_equity_warmup_ticks', 0)
            if warmup < 5:
                self._equity_warmup_ticks = warmup + 1
            elif self.equity.is_below_ma(ma_period=20) and not self._dd_paused:
                logger.warning("⏸️  EquityCurve sous MA20 — circuit breaker déclenché")
                self._dd_paused = True
                pnl_pct = self.equity.total_pnl_pct()
                # → Discord #monitoring uniquement (via INSERT alerts → monitor.py)
                try:
                    self.db._execute(
                        "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                        ("CIRCUIT_BREAKER",
                         f"⏸️ Circuit Breaker — Equity sous MA20 — trading en pause. Balance: {balance:,.2f}€ | PnL: {pnl_pct:+.1f}%")
                    )
                except Exception:
                    pass

        # ── Reset quotidien (minuit UTC) ─────────────────────────────────────
        if today != self._last_reset_day:
            self._last_reset_day = today
            self._capital_closed_today.clear()
            self._dd_paused = False

            # ─── Module 3: EoD Reconciliation — CRON 00h00 UTC ──────────────
            if getattr(self, '_last_eod_date', None) != today:
                self._last_eod_date = today
                threading.Thread(
                    target=self.eod.run,
                    daemon=True,
                    name="eod_reconciliation",
                ).start()
                logger.info("📋 EoD Reconciliation lancée (00h00 UTC)")

        # ─── Module 2: Quarantine refresh toutes les 15 min ──────────────────
        _qrefresh_delta = (now - getattr(self, '_last_quarantine_refresh',
                                         datetime.min.replace(tzinfo=timezone.utc))).total_seconds()
        if _qrefresh_delta >= 900:  # 15 min
            self._last_quarantine_refresh = now
            threading.Thread(
                target=self.quarantine.refresh_from_db,
                daemon=True,
                name="quarantine_refresh",
            ).start()

            # C-4: Clear persisted dd_paused on new day
            try:
                self.db.save_bot_state("dd_paused", "0")
            except Exception:
                pass
            self.reporter.reset_for_new_day()  # remet rapport à zéro
            # BUG FIX #2 : met à jour le solde de début de journée pour le DD journalier
            if self.broker.available:
                self._daily_start_balance = self.broker.get_balance() or self._daily_start_balance
            # Reset compteur trades journaliers par instrument (minuit UTC)
            if hasattr(self, '_daily_inst_trades'):
                self._daily_inst_trades = {s: 0 for s in self._daily_inst_trades}
                logger.info("♻️  Max trades/instrument reset (nouveau jour)")
            # Reset alertes DD (pour que les seuils se redéclenchent demain)
            self._dd_alerts_sent = set()
            # ── Challenge Tracker : affiche progression vers objectif Prop Firm ──
            _bal_now = self.broker.get_balance() if self.broker.available else 0.0
            if _bal_now > 0 and self.initial_balance > 0:
                _challenge_pct = (_bal_now - self.initial_balance) / self.initial_balance * 100
                _target_pct = 10.0  # Prop Firm Phase 1 : +10% profit target
                _progress = min(_challenge_pct / _target_pct * 100, 100)
                _remaining = _target_pct - _challenge_pct
                logger.info(
                    f"🏆 Challenge Tracker : {_challenge_pct:+.2f}% / {_target_pct:.0f}% "
                    f"({_progress:.0f}% accompli | reste {_remaining:.2f}%)"
                )
            logger.info("🔄 Reset quotidien — stats journalières effacées")
            self._last_session_push = ""    # reset push session pour le nouveau jour


            # ── Reset mensuel & Drawdown Mensuel ─────────────────────────────
            cur_month = now.month
            if cur_month != self._last_reset_month:
                self._last_reset_month      = cur_month
                self._monthly_dd_paused     = False
                self._monthly_start_balance = self.broker.get_balance() or self._monthly_start_balance
                self._capital_closed_month.clear()  # F-6: Reset monthly list on new month
                logger.info("📅 Reset mensuel — drawdown mensuel remis à zéro")
            else:
                # Vérification DD mensuel (toujours dans le même mois)
                if self._monthly_start_balance > 0 and not self._monthly_dd_paused:
                    bal_now = self.broker.get_balance() or 0
                    monthly_dd_pct = (self._monthly_start_balance - bal_now) / self._monthly_start_balance * 100
                    if monthly_dd_pct >= 15:
                        self._monthly_dd_paused = True
                        self._dd_paused = True
                        logger.critical(f"🚨 DD MENSUEL CRITIQUE {monthly_dd_pct:.1f}% ≥ 15% — pause totale")
                        # → Discord #monitoring uniquement
                        try:
                            self.db._execute(
                                "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                                ("DD_MENSUEL", f"🚨 DD MENSUEL CRITIQUE {monthly_dd_pct:.1f}% — pause totale")
                            )
                        except Exception:
                            pass
                    elif monthly_dd_pct >= 10:
                        self._dd_paused = True
                        logger.warning(f"⚠️ DD mensuel {monthly_dd_pct:.1f}% ≥ 10% — pause 48h")
                        # → Discord #monitoring uniquement
                        try:
                            self.db._execute(
                                "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                                ("DD_MENSUEL", f"⚠️ DD Mensuel {monthly_dd_pct:.1f}% — pause 48h")
                            )
                        except Exception:
                            pass

        # ── SPRINT 4 : Backup Supabase automatique (toutes les 5 min) ──────────
        # Survie aux redémarrages Docker sans perdre l'état des positions.
        elapsed_backup = (now - self._last_backup_time).total_seconds()
        if elapsed_backup >= 300:  # 5 minutes
            self._last_backup_time = now
            try:
                for inst, state in self.positions.items():
                    if state is not None:
                        self.db.save_position(inst, state)
                logger.debug("💾 Backup Supabase — états positions sauvegardés")
            except Exception as _bk_e:
                logger.debug(f"Backup Supabase: {_bk_e}")

        # ── SPRINT 4 : Drift Auto-Size Reduction ─────────────────────────────
        # Si concept drift détecté → réduire automatiquement de 50% pendant 48h
        try:
            drift_result = self.drift.check_drift()
            if drift_result.get("drift") and not self._drift_size_reduced:
                self._drift_size_reduced  = True
                self._drift_reduced_until = now + timedelta(hours=48)
                logger.warning("🔴 Drift détecté → taille réduite de 50% pour 48h")
                if self.telegram.router:
                    self.telegram.router.send_risk(
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

        # ── SPRINT 4 : Auto-Optimisation Hebdomadaire (Dimanche 2h UTC) ─────
        if (now.weekday() == 6 and now.hour == 2 and now.minute < 5):
            cur_week = now.isocalendar()[1]
            if self._last_hyperopt_week != cur_week:
                self._last_hyperopt_week = cur_week
                logger.info(f"⚙️  Auto-Optimisation hebdo S{cur_week} — lancement...")
                # F-5: Only LSTM training and AB weekly report are active.
                def _run_weekly_tasks():
                    # Feature P : Entraîner le LSTM sur chaque instrument
                    try:
                        for _inst in CAPITAL_INSTRUMENTS:
                            df_train = (self.broker.fetch_ohlcv(_inst, timeframe="5m", count=400)
                                        if self.broker.available else None) \
                                       or self.capital.fetch_ohlcv(_inst, timeframe="5m", count=400)
                            if df_train is not None and len(df_train) >= 100:
                                df_train = self.strategy.compute_indicators(df_train)
                                ok = self.lstm.train(df_train)
                                if ok:
                                    logger.info(f"🧠 LSTM Predictor entraîné sur {_inst}")
                                    # FIX MAJEUR: charger le modèle après entraînement sinon inutile
                                    try:
                                        self.lstm.load()
                                    except Exception:
                                        pass
                    except Exception as _lstm_e:
                        logger.warning(f"LSTM training: {_lstm_e}")

                    # Feature U : Rapport A/B hebdomadaire
                    try:
                        report = self.ab.weekly_report()
                        winner = self.ab.global_winner()
                        if self.telegram.router:
                            self.telegram.router.send_performance(
                                f"{report}\n🏆 Variante globale : <b>{winner}</b>"
                            )
                    except Exception as _ab_e:
                        logger.debug(f"AB weekly: {_ab_e}")

                threading.Thread(target=_run_weekly_tasks, daemon=True).start()


        # ── Auto-push Telegram : ouverture de session ─────────────────────────

        h_utc = now.hour
        # Détecte début de session London (8h UTC) et NY (13h UTC)
        current_session = ""
        if h_utc == 8:   current_session = "London"
        elif h_utc == 13: current_session = "NY"

        if current_session and current_session != self._last_session_push:
            self._last_session_push = current_session
            try:
                bal_push = self.broker.get_balance() if self.broker.available else 0.0
                pnl_push = round(bal_push - self.initial_balance, 2) if bal_push > 0 else 0.0
                pnl_pct_push = (pnl_push / self.initial_balance * 100) if self.initial_balance > 0 else 0.0
                session_icon = "🇬🇧" if current_session == "London" else "🇺🇸"
                if self.telegram.router:
                    self.telegram.router.send_dashboard(
                        f"{session_icon} <b>Session {current_session} ouverte</b>\n\n"
                        f"💰 Balance : <b>{bal_push:,.2f}€</b>\n"
                        f"📊 PnL total : <b>{pnl_push:+.2f}€ ({pnl_pct_push:+.1f}%)</b>\n"
                        f"🤖 Bot : 🟢 ACTIF — scanning {len(CAPITAL_INSTRUMENTS)} instruments"
                    )
                logger.info(f"{session_icon} Session {current_session} ouverte — alerte Telegram envoyée")
            except Exception as _e:
                logger.debug(f"Auto-push session : {_e}")

        # ── Auto-push Telegram : heartbeat via Hub refresh (zéro spam) ────────
        in_session = h_utc in SESSION_HOURS
        since_last = (now - self._last_heartbeat_push).total_seconds()
        if in_session and since_last >= 1800:  # 30 minutes
            self._last_heartbeat_push = now
            try:
                bal_hb = self.broker.get_balance() if self.broker.available else 0.0
                pnl_today_hb = sum(t.get("pnl", 0) for t in self._capital_closed_today)
                open_count = sum(1 for s in self.positions.values() if s is not None)
                equity_vals = [e["v"] for e in self._equity_history[-12:]] if hasattr(self, '_equity_history') and self._equity_history else []
                conf = self.telegram.gamification.confidence_score() if self.telegram.gamification else None
                # Wave 15: Pass system_stats to Hub
                _sys_stats = self.get_system_stats() if hasattr(self, 'get_system_stats') else None
                if self.telegram.hub:
                    self.telegram.hub.refresh_hub(
                        balance=bal_hb,
                        pnl_today=round(pnl_today_hb, 2),
                        open_positions=open_count,
                        equity_data=equity_vals,
                        confidence=conf,
                        system_stats=_sys_stats,
                    )
            except Exception as _e:
                logger.debug(f"Hub refresh heartbeat : {_e}")

        # ── Monthly leaderboard → Stats (1er du mois à 10h UTC) ──────────
        if now.day == 1 and h_utc == 10 and today != getattr(self, '_last_leaderboard_day', ''):
            self._last_leaderboard_day = today
            try:
                lb = self.telegram.gamification.build_monthly_leaderboard(
                    trades_this_month=self._capital_closed_month,  # F-6: use monthly data
                )
                if self.telegram.router:
                    self.telegram.router.send_stats(lb, silent=False)
            except Exception as _lb_e:
                logger.debug(f"Monthly leaderboard: {_lb_e}")

        # ── R-4: Update VIX synthetic from cached ATR values ──────────────
        try:
            atr_values = {}
            for instr in CAPITAL_INSTRUMENTS:
                cache_entry = self.ohlcv_cache._store.get(instr)
                if cache_entry and cache_entry.get("df") is not None:
                    _df = cache_entry["df"]
                    if "atr" in _df.columns and len(_df) > 0:
                        atr_val = float(_df.iloc[-1]["atr"])
                        close_val = float(_df.iloc[-1]["close"])
                        if atr_val > 0 and close_val > 0:
                            atr_values[instr] = (atr_val, close_val)
            if atr_values:
                self.risk.update_vix_synthetic(atr_values)
        except Exception as _vix_e:
            logger.debug(f"VIX synthetic update: {_vix_e}")

        # ── Vérification drawdown journalier (R-4: dynamic limit) ─────────
        if self.broker.available:
            cur_bal = self.broker.get_balance()
            if cur_bal > 0 and self._daily_start_balance > 0:
                dd_pct = (self._daily_start_balance - cur_bal) / self._daily_start_balance * 100

                # ── DD progression alerts (Prop Firm risk awareness) ─────────
                # Track which alert levels already notified (reset each day)
                if not hasattr(self, '_dd_alerts_sent'):
                    self._dd_alerts_sent = set()

                _DD_LEVELS = [
                    (4.5, "🔴 ALERTE CRITIQUE", "⛔ Arrêt imminent du trading si DD continue"),
                    (4.0, "🟠 ALERTE HAUTE",    "⚠️ Plus que 1% avant suspension Prop Firm"),
                    (3.0, "🟡 ALERTE MOYENNE",  "📉 Drawdown franchit le seuil de vigilance"),
                    (2.0, "🟢 AVERTISSEMENT",   "📊 Drawdown journalier en hausse"),
                ]
                for lvl, title, msg in _DD_LEVELS:
                    if dd_pct >= lvl and lvl not in self._dd_alerts_sent:
                        self._dd_alerts_sent.add(lvl)
                        _prof_pct = (cur_bal - self.initial_balance) / self.initial_balance * 100
                        _alert_msg = (
                            f"{title} — NEMESIS\n"
                            f"DD Journalier : <code>{dd_pct:.2f}%</code> / {self.DAILY_DD_LIMIT:.0f}% max\n"
                            f"{msg}\n"
                            f"Balance : <code>{cur_bal:,.2f}$</code> | PnL total : <code>{_prof_pct:+.2f}%</code>"
                        )
                        logger.warning(f"⚠️ DD {dd_pct:.1f}% — Alerte seuil {lvl}%")
                        try:
                            if self.telegram:
                                self.telegram.router.send_report(_alert_msg)
                        except Exception:
                            pass
                        break  # Ne déclenche qu'un seul niveau à la fois

                _dd_limit = self.risk.dynamic_dd_limit
                if not self._dd_paused and dd_pct >= _dd_limit:
                    self._dd_paused = True
                    # C-4: Persist across Docker restarts
                    try:
                        self.db.save_bot_state("dd_paused", "1")
                        self.db.save_bot_state("dd_paused_date", today.isoformat())
                    except Exception:
                        pass
                    try:
                        self.db._execute(
                            "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                            ("DD_JOURNALIER",
                             f"🚨 DRAWDOWN JOURNALIER {dd_pct:.1f}% ≥ {_dd_limit:.1f}% — trading suspendu. Balance: {cur_bal:,.2f}€")
                        )
                    except Exception:
                        pass
                    logger.warning(f"🚨 DD journalier {dd_pct:.1f}% ≥ {_dd_limit:.1f}% — trading suspendu")

        # ── Morning Brief (07h00 UTC) ─────────────────────────────────────────
        if self.context.should_send_brief():
            balance = self.broker.get_balance() if self.broker.available else 0.0
            _, reason = self.calendar.should_pause_trading()
            brief = self.context.build_morning_brief(balance, reason or None)
            self.telegram.notify_morning_brief(brief, nb_instruments=len(CAPITAL_INSTRUMENTS))
            self.context.mark_brief_sent()

        # ── Fear & Greed refresh (1×/heure) + Regime-change detection ─────
        _old_regime = getattr(self, '_last_known_regime', 'NEUTRAL')
        self.context.refresh_fear_greed()
        _new_regime = self.context.regime
        if _new_regime != _old_regime and _old_regime != 'NEUTRAL':
            self._last_known_regime = _new_regime
            try:
                tgc.notify_regime_change(
                    old_regime=_old_regime,
                    new_regime=_new_regime,
                    fg_value=self.context._fg_value or 0,
                )
                logger.info(f"🌍 Regime change: {_old_regime} → {_new_regime}")
            except Exception as _rc_e:
                logger.debug(f"Regime change notif: {_rc_e}")
        self._last_known_regime = _new_regime

        # ── Wallet stats (toutes les 30 min) ─────────────────────────────
        wallet_interval = timedelta(minutes=30)
        if now - self._last_wallet_post >= wallet_interval:
            balance_w = self.broker.get_balance() if self.broker.available else 0.0
            if balance_w > 0:
                self._post_wallet_stats(balance_w)
            self._last_wallet_post = now

        # ── Rapport journalier (20h UTC) + hebdo (21h UTC) ───────────────
        if self.reporter.should_send_report():
            if self.telegram.router:
                _ml = self.ml_scorer if hasattr(self, 'ml_scorer') else None
                _ctx = self.context if hasattr(self, 'context') else None
                _rpt = self.reporter.build_report(ml_scorer=_ml, context=_ctx)
                # Append Prop Firm challenge progress to daily report
                try:
                    _bal = self.broker.get_balance() if self.broker.available else 0.0
                    if _bal > 0 and self.initial_balance > 0:
                        _c_pct = (_bal - self.initial_balance) / self.initial_balance * 100
                        _prog  = min(max(_c_pct / 10.0 * 100, 0), 100)
                        _bar   = "█" * int(_prog / 10) + "░" * (10 - int(_prog / 10))
                        _hwm   = getattr(self, '_equity_hwm', _bal)
                        _rpt += (
                            f"\n\n🏆 <b>Challenge Prop Firm — Phase 1</b>\n"
                            f"  {_bar} <b>{_prog:.0f}%</b> vers +10%\n"
                            f"  Gain : <code>{_c_pct:+.2f}%</code> | Cible : <code>+10%</code>\n"
                            f"  Reste : <code>{max(10.0 - _c_pct, 0):.2f}%</code> | HWM : <code>{_hwm:,.2f}$</code>"
                        )
                except Exception:
                    pass
                self.telegram.router.send_performance(_rpt)
            self.reporter.mark_report_sent()

        if self.reporter.should_send_weekly():
            if self.telegram.router:
                self.telegram.router.send_performance(self.reporter.build_weekly_report())
            self.reporter.mark_weekly_sent()

        # ── Rapport mensuel (1er du mois 10h UTC) ─────────────────────────
        try:
            if hasattr(self, 'monthly_reporter') and self.monthly_reporter.should_send():
                threading.Thread(
                    target=self.monthly_reporter.send,
                    daemon=True, name="monthly_report"
                ).start()
                logger.info("📅 Rapport mensuel envoyé en thread")
        except Exception:
            pass

        # ── Sprint 5 : Rapport visuel PNG journalier (20h UTC) ────────────
        if h_utc == 20 and today != self._last_daily_report_day:
            self._last_daily_report_day = today
            try:
                threading.Thread(target=self._send_daily_report, daemon=True).start()
            except Exception as _rp_e:
                logger.debug(f"Daily report: {_rp_e}")

        # ── Résumé fin de journée → Dashboard (22h UTC) ──────────────────
        if h_utc == 22 and today != getattr(self, '_last_eod_summary_day', ''):
            self._last_eod_summary_day = today
            try:
                bal_eod = self.broker.get_balance() if self.broker.available else 0.0
                pnl_eod = sum(t.get("pnl", 0) for t in self._capital_closed_today)
                nb_trades = len(self._capital_closed_today)
                wins_eod = sum(1 for t in self._capital_closed_today if t.get("pnl", 0) > 0)
                wr_eod = (wins_eod / nb_trades * 100) if nb_trades > 0 else 0
                gain_pct = ((bal_eod - self.initial_balance) / self.initial_balance * 100) if self.initial_balance > 0 else 0
                trend = "📈" if pnl_eod >= 0 else "📉"

                # R-4: Fallback if renderer module absent
                try:
                    from nemesis_ui.renderer import NemesisRenderer as _R
                    header = _R.box_header('🌙 FIN DE JOURNÉE')
                except Exception:
                    header = '🌙 <b>FIN DE JOURNÉE</b>'
                eod_text = (
                    f"{header}\n\n"
                    f"💰 Capital : <b>{bal_eod:,.2f}€</b>  ({gain_pct:+.2f}%)\n"
                    f"{trend} PnL du jour : <b>{pnl_eod:+.2f}€</b>\n"
                    f"📋 Trades : {nb_trades}  ·  WR : <b>{wr_eod:.0f}%</b>\n\n"
                    f"🟢 Bot en veille — reprise London 08h UTC 🇬🇧\n"
                    f"<i>Bonne nuit ! 🌙</i>"
                )
                # FIN DE JOURNÉE → Discord webhook admin (pas sur QUANT Access Bot Telegram)
                import requests as _req, os as _os
                _webhook_eod = _os.getenv("DISCORD_WEBHOOK_MONITORING", "")
                if _webhook_eod:
                    try:
                        _eod_discord = eod_text.replace("<b>", "**").replace("</b>", "**")
                        _eod_discord = _eod_discord.replace("<i>", "*").replace("</i>", "*")
                        _eod_discord = _eod_discord.replace("<code>", "`").replace("</code>", "`")
                        _req.post(_webhook_eod, json={"content": _eod_discord[:2000]}, timeout=10)
                    except Exception as _e:
                        logger.debug(f"EOD Discord: {_e}")
                # NE PAS envoyer sur Telegram router (c'est le bot d'accès VIP, pas le monitoring)
            except Exception as _eod_e:
                logger.debug(f"EOD summary: {_eod_e}")


        # ─── Moteur de trading Capital.com ───────────────────────────────────

        # ── Time-Stop : Ferme positions sans TP1 après TIME_STOP_HOURS ──────
        TIME_STOP_HOURS = float(os.getenv("TIME_STOP_HOURS", "12"))
        try:
            for _inst, _state in list(self.positions.items()):
                if _state is None:
                    continue
                if _state.get("tp1_hit", False):
                    continue  # TP1 atteint → trailing stop prend le relais
                _open_time = _state.get("open_time")
                if _open_time is None:
                    continue
                _elapsed_h = (now - _open_time).total_seconds() / 3600
                if _elapsed_h >= TIME_STOP_HOURS:
                    _refs = _state.get("refs", [])
                    _deal = _refs[0] if _refs else None
                    _dir  = _state.get("direction", "?")
                    logger.warning(
                        f"⏱️ TIME-STOP {_inst} {_dir}: "
                        f"{_elapsed_h:.1f}h ouvert sans TP1 → fermeture"
                    )
                    if _deal and self.broker.available:
                        try:
                            self.broker.close_position(str(_deal))
                        except Exception:
                            pass
                    # aussi fermer via capital si MT5 non dispo
                    try:
                        self.capital.close_position(_deal or "")
                    except Exception:
                        pass
                    self.positions[_inst] = None
                    try:
                        self.db._execute(
                            "INSERT INTO alerts(type,message) VALUES(%s,%s)",
                            ("TIME_STOP", f"⏱️ Time-Stop {_inst} {_dir} après {_elapsed_h:.1f}h")
                        )
                    except Exception:
                        pass
        except Exception as _ts_e:
            logger.debug(f"Time-stop check: {_ts_e}")

        # Pause manuelle ou drawdown
        if self._manual_pause or self._dd_paused:
            logger.info("⏸️  Trading en pause (manuel ou DD) — skip ce tick")
            return

        # Capital.com non disponible → rien à faire
        # FIX MAJEUR: si MT5 est broker actif, on continue même si Capital.com est down
        # (Capital.com n'est utilisé que pour les données, pas les ordres)
        if not self.capital.available and not self.broker.available:
            logger.warning("⚠️  Aucun broker disponible — skip ce tick")
            return

        # ── Surveillance des positions ouvertes ──────────────────────────
        self._monitor_capital_positions()

        # ── Vérification session : désormais per-instrument dans la boucle ci-dessous ──

        # ── Pause calendrier économique ───────────────────────────────────
        should_pause, reason = self.calendar.should_pause_trading()
        if should_pause:
            logger.info(f"📅 Trading suspendu : {reason}")
            return

        # ── Limite exposition (max 10 CFD simultanées) ───────────────────────
        active_count = sum(1 for s in self.positions.values() if s is not None)
        if active_count >= MAX_OPEN_TRADES:
            logger.debug(f"🔒 Positions max atteint ({active_count}/{MAX_OPEN_TRADES}) — skip ce tick")
            return  # Plafond atteint — on surveille mais on n'ouvre rien

        # ── Scan des instruments Capital.com ─────────────────────────────────
        balance = self.broker.get_balance() if self.broker.available else 0.0
        if balance <= 0:
            logger.warning("⚠️  Balance = 0 ou inaccessible — skip ce tick")
            return

        # ── High Water Mark (HWM) — nouveau sommet d'équité ──────────────────
        _hwm = getattr(self, '_equity_hwm', self.initial_balance)
        if balance > _hwm * 1.001:  # +0.1% pour éviter le bruit
            self._equity_hwm = balance
            _hwm_gain = balance - self.initial_balance
            _hwm_pct  = (_hwm_gain / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            logger.info(f"🏆 Nouveau High Water Mark : {balance:,.2f}$ (+{_hwm_pct:.2f}%)")
            try:
                msg = (
                    f"🏆 <b>Nouveau Sommet — NEMESIS</b>\n"
                    f"High Water Mark : <code>{balance:,.2f}$</code>\n"
                    f"Gain depuis début : <code>+{_hwm_gain:,.2f}$ ({_hwm_pct:+.2f}%)</code>"
                )
                if self.telegram:
                    self.telegram.router.send_report(msg)
            except Exception:
                pass

        # F-4: per_instrument removed (was calculated but never used)

        # ── Heartbeat visible : confirme que la boucle tourne ──────────────────
        logger.info(
            f"🔍 Scan {len(CAPITAL_INSTRUMENTS)} instruments | "
            f"Balance={balance:,.0f}€ | Positions={active_count}/{MAX_OPEN_TRADES} | "
            f"{now.hour}h{now.minute:02d} UTC"
        )

        signals_found = 0
        _scan_lock  = threading.Lock()   # FIX CRITIQUE: race condition sur signals_found
        _scan_sem   = threading.Semaphore(8)  # A-1: max 8 concurrent API calls

        # FIX MINEUR: cache drift result pour éviter double appel check_drift()
        _drift_result_cache = None

        def _scan_instrument(instrument):
            """A-1: Scan a single instrument (runs in thread pool)."""
            nonlocal signals_found
            if sum(1 for s in self.positions.values() if s is not None) >= MAX_OPEN_TRADES:
                return
            _cat = ASSET_PROFILES.get(instrument, {}).get("cat", "forex")
            if not self.strategy.is_session_ok_for(instrument, _cat):
                return
            _scan_sem.acquire()
            try:
                _open_before = sum(1 for s in self.positions.values() if s is not None)
                # ⏱️ Latency Tracker: mesure le cycle complet par instrument
                with self.latency.measure(instrument):
                    self._process_capital_symbol(instrument, balance)
                _open_after = sum(1 for s in self.positions.values() if s is not None)
                if _open_after > _open_before:
                    with _scan_lock:  # FIX CRITIQUE: race condition — int += 1 pas atomique
                        signals_found += 1
            except Exception as e:
                logger.error(f"❌ _process_capital_symbol {instrument} : {e}")
            finally:
                _scan_sem.release()


        # A-1: Parallel scan — 48 instruments in ~2-3s instead of ~14.4s
        from concurrent.futures import ThreadPoolExecutor, as_completed
        _scan_t0 = time.time()
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="A1_scan") as scan_pool:
            futures = [scan_pool.submit(_scan_instrument, instr) for instr in CAPITAL_INSTRUMENTS]
            for f in as_completed(futures, timeout=60):
                try:
                    f.result()
                except Exception as e:
                    logger.debug(f"A-1 scan future: {e}")
        _scan_elapsed = time.time() - _scan_t0
        logger.debug(f"⚡ A-1 scan complete: {len(CAPITAL_INSTRUMENTS)} instruments in {_scan_elapsed:.1f}s")

        # ── Slippage Discord Alert (vérifié après chaque scan) ──────────────
        try:
            st = getattr(self, 'slippage_tracker', None)
            if st:
                st.check_discord_alert(window=5, threshold_pips=3.0)
        except Exception:
            pass

        # ── S-3: Micro-Timeframe Scan (5m/15m — additional signals) ──────
        if MICRO_TF_PROFILES and not self._dd_paused and not self._manual_pause:
            for micro_key, micro_profile in MICRO_TF_PROFILES.items():
                if sum(1 for s in self.positions.values() if s is not None) >= MAX_OPEN_TRADES:
                    break
                epic = micro_profile.get("epic", micro_key.split("_")[0])
                _cat = micro_profile.get("cat", "forex")
                if not self.strategy.is_session_ok_for(epic, _cat):
                    continue
                # Rate-limit per micro-TF instrument
                _max_per_h = micro_profile.get("max_per_hour", 3)
                _micro_key_count = sum(
                    1 for t in self._capital_closed_today
                    if t.get("instrument") == epic
                    and t.get("micro_tf") == micro_profile.get("tf")
                )
                if _micro_key_count >= _max_per_h:
                    continue
                try:
                    # Fetch micro-TF data directly (not from main cache)
                    _mtf = micro_profile.get("tf", "5m")
                    _count = {"5m": 300, "15m": 250}.get(_mtf, 200)
                    df_micro = (self.broker.fetch_ohlcv(epic, timeframe=_mtf, count=_count)
                                if self.broker.available else None) \
                               or self.capital.fetch_ohlcv(epic, timeframe=_mtf, count=_count)
                    if df_micro is not None and len(df_micro) >= 50:
                        df_micro = self.strategy.compute_indicators(df_micro)
                        # Use micro profile for signal generation
                        _open_before = sum(1 for s in self.positions.values() if s is not None)
                        self._process_micro_signal(epic, micro_key, df_micro, micro_profile, balance)
                        _open_after = sum(1 for s in self.positions.values() if s is not None)
                        if _open_after > _open_before:
                            signals_found += 1
                except Exception as e:
                    logger.debug(f"Micro-TF {micro_key}: {e}")
                time.sleep(0.2)

        # ── Alerte "scan sans signal" — supprimée en v3.0 (visible via Dashboard) ──
        # Si aucun signal trouvé, on l'enregistre dans les logs uniquement
        if signals_found == 0:
            elapsed_ns = (now - self._last_no_signal_alert).total_seconds()
            if elapsed_ns >= 600:  # 10 minutes
                self._last_no_signal_alert = now
                session_str = "London" if now.hour < 13 else "NY"
                logger.info(
                    f"🔍 Scan {session_str} — aucun breakout sur "
                    f"{len(CAPITAL_INSTRUMENTS)} instruments — surveillance continue…"
                )

        # ── A-4: Set breakout levels for WS instant detection ──────────
        if hasattr(self, 'capital_ws') and self.capital_ws:
            for instrument in CAPITAL_INSTRUMENTS:
                if self.positions.get(instrument) is not None:
                    continue  # Already has a position
                _profile = ASSET_PROFILES.get(instrument, {})
                if _profile.get("strat") != "BK":
                    continue  # Only BK strategy uses breakout levels
                df = self.ohlcv_cache.get(instrument, strategy=self.strategy)
                if df is None or len(df) < 10:
                    continue
                try:
                    _range = self.strategy.compute_session_range(
                        df, range_lookback=_profile.get("range_lb", 6)
                    )
                    _margin = _range["size"] * _profile.get("bk_margin", 0.03)
                    self.capital_ws.set_breakout_levels(
                        instrument, _range["high"], _range["low"], margin=_margin
                    )
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════════════════════════
    #  A-4: WS BREAKOUT CALLBACK
    # ═══════════════════════════════════════════════════════════════════════

    def _on_ws_breakout(self, epic: str, direction: str, price: float):
        """
        A-4: Called by WebSocket when a breakout is detected.
        Triggers _process_capital_symbol immediately (latency <500ms).
        """
        if self._dd_paused or self._manual_pause:
            return
        if self.positions.get(epic) is not None:
            return  # Already has a position
        if sum(1 for s in self.positions.values() if s is not None) >= MAX_OPEN_TRADES:
            return

        logger.info(f"🚀 A-4 WS BREAKOUT → trigger {epic} {direction} @ {price:.5f}")
        balance = self.broker.get_balance() if self.broker.available else 0.0
        if balance <= 0:
            return

        try:
            self._process_capital_symbol(epic, balance)
        except Exception as e:
            logger.error(f"❌ WS breakout process {epic}: {e}")
