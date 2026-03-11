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

        # ── SPRINT 4 : Backup Supabase automatique (toutes les 5 min) ──────────
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

        # ── SPRINT 4 : Drift Auto-Size Reduction ─────────────────────────────
        # Si concept drift détecté → réduire automatiquement de 50% pendant 48h
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

        # ── SPRINT 4 : Auto-Optimisation Hebdomadaire (Dimanche 2h UTC) ─────
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

        # ── Sprint 5 : Rapport visuel PNG journalier (20h UTC) ────────────
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

        # ── Limite exposition (max 10 CFD simultanées) ───────────────────────
        active_count = sum(1 for s in self.capital_trades.values() if s is not None)
        if active_count >= 10:
            logger.debug(f"🔒 Positions max atteint ({active_count}/10) — skip ce tick")
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
            f"Balance={balance:,.0f}€ | Positions={active_count}/10 | "
            f"{now.hour}h{now.minute:02d} UTC"
        )

        signals_found = 0
        for instrument in CAPITAL_INSTRUMENTS:
            # Ne pas ouvrir si limite atteinte entre deux itérations
            if sum(1 for s in self.capital_trades.values() if s is not None) >= 10:
                break
            try:
                _open_before = sum(1 for s in self.capital_trades.values() if s is not None)
                self._process_capital_symbol(instrument, per_instrument)
                _open_after  = sum(1 for s in self.capital_trades.values() if s is not None)
                if _open_after > _open_before:
                    signals_found += 1
            except Exception as e:
                logger.error(f"❌ _process_capital_symbol {instrument} : {e}")
            time.sleep(0.3)  # Rate limiting Capital.com API (max ~3 req/s)

        # ── Alerte Telegram "scan sans signal" toutes les 10 minutes ──────────
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
                        f"Aucun breakout détecté sur {len(CAPITAL_INSTRUMENTS)} instruments — surveillance continue…"
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
