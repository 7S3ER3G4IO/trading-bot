"""
bot_monitor.py — Surveillance des positions ouvertes + callbacks WebSocket
"""
from .imports import *


class BotMonitorMixin:

    def _on_ws_price_tick(self, epic: str, mid: float) -> None:
        """
        Feature R — Callback appelé par le WebSocket à chaque tick de prix.
        """
        try:
            if self.positions.get(epic) is not None:
                return
            retest = self._pending_retest.get(epic)
            if retest is None:
                return
            retest_level = retest.get("retest_level", 0)
            atr_now = retest.get("atr", 0)
            if atr_now <= 0:
                return
            if abs(mid - retest_level) <= atr_now * 0.5:
                logger.debug(
                    f"⚡ WS real-time trigger {epic} — prix {mid:.5f} ≈ retest {retest_level:.5f}"
                )
                bal = self.broker.get_balance() if self.broker.available else 0.0
                if bal > 0:
                    self._process_capital_symbol(epic, bal)
        except Exception as _ws_e:
            logger.debug(f"WS price_tick {epic}: {_ws_e}")

    def _on_ws_be_triggered(self, instrument: str, entry_or_sl: float, event: str = "TP1"):
        """
        Callback — appelé quand TP1 ou TP2 est franchi (WebSocket < 500ms ou poll fallback).

        Multi-TP Protocol (Prop Firm validé 5/5 seeds) :
          TP1 → close 40% de la position + activer Break-Even (SL → entry)
          TP2 → close 40% de la position + activer Trailing Stop sur les 20% restants
        """
        state = self.positions.get(instrument)
        if state is None:
            return

        name = CAPITAL_NAMES.get(instrument, instrument)
        direction = state.get("direction", "BUY")
        refs = state.get("refs", [None])

        # ─── TP1 : close 40% + Break-Even ────────────────────────────────────
        if event == "TP1" and not state.get("tp1_hit"):
            state["tp1_hit"] = True
            state["be_active"] = True

            size_tp1 = state.get("size_tp1", 0)
            tp1_price = state.get("tp1_level", state.get("tp1", state["entry"]))

            logger.info(
                f"🎯 TP1 touché — {instrument} @ {tp1_price:.5f} | "
                f"Partial close {size_tp1:.4f} lots (40%) + Break-Even"
            )

            # 1. Partial close 40%
            if size_tp1 > 0:
                try:
                    ok = self.broker.close_partial(instrument, direction, size_tp1)
                    if ok:
                        logger.info(f"  ✅ {instrument} TP1 partial close OK ({size_tp1:.4f} lots)")
                    else:
                        logger.warning(f"  ⚠️ {instrument} TP1 partial close FAILED — position reste entière")
                except Exception as _pc_e:
                    logger.debug(f"  TP1 partial close {instrument}: {_pc_e}")

            # 2. Move SL to Break-Even
            if refs and refs[0]:
                try:
                    _dec = PRICE_DECIMALS.get(instrument, 5)
                    be_sl = round(state["entry"], _dec)
                    self.broker.update_position(refs[0], stop_level=be_sl)
                    state["sl"] = be_sl
                    logger.info(f"  🛡️ BE activé {instrument}: SL → {be_sl:.{_dec}f} (entrée)")
                except Exception as _be_e:
                    logger.debug(f"  BE update {instrument}: {_be_e}")
            # 3. Telegram TP1 — reply au signal d'ouverture (Station X)
            if self.telegram and self.telegram.router:
                _entry   = state.get("entry", 0.0)
                _sl      = state.get("sl", 0.0)
                _tp2_lvl = state.get("tp2_level", state.get("tp2", 0.0))
                _msg_id  = state.get("tg_signal_msg_id")
                threading.Thread(
                    target=lambda: self.telegram.router.send_tp1(
                        instrument=instrument, entry=_entry, tp1=tp1_price,
                        sl=_sl, tp2=_tp2_lvl, direction=direction,
                        reply_to_message_id=_msg_id,
                    ),
                    daemon=True,
                ).start()

        elif event == "TP2" and state.get("tp1_hit") and not state.get("tp2_hit"):
            state["tp2_hit"] = True

            size_tp2 = state.get("size_tp2", 0)
            tp2_price = state.get("tp2_level", state.get("tp2", state["entry"]))

            logger.info(
                f"🎯 TP2 touché — {instrument} @ {tp2_price:.5f} | "
                f"Partial close {size_tp2:.4f} lots (40%) + Trailing 20% actif"
            )

            # 1. Partial close 40%
            if size_tp2 > 0:
                try:
                    ok = self.broker.close_partial(instrument, direction, size_tp2)
                    if ok:
                        logger.info(f"  ✅ {instrument} TP2 partial close OK ({size_tp2:.4f} lots)")
                    else:
                        logger.warning(f"  ⚠️ {instrument} TP2 partial close FAILED")
                except Exception as _pc2_e:
                    logger.debug(f"  TP2 partial close {instrument}: {_pc2_e}")

            # 2. Supprimer le limitLevel broker (laisser le trailing gérer la sortie)
            if refs and refs[0]:
                try:
                    self.broker.update_position(refs[0], limit_level=0)
                except Exception:
                    pass

            logger.info(
                f"  🔒 {instrument} — 20% restant géré par Trailing Stop (convexity_engine)"
            )

            # Telegram TP2 — reply au signal d'ouverture (Station X)
            if self.telegram and self.telegram.router:
                _entry2  = state.get("entry", 0.0)
                _msg_id2 = state.get("tg_signal_msg_id")
                threading.Thread(
                    target=lambda: self.telegram.router.send_tp2(
                        instrument=instrument, entry=_entry2, tp2=tp2_price,
                        direction=direction,
                        reply_to_message_id=_msg_id2,
                    ),
                    daemon=True,
                ).start()

    def _monitor_capital_positions(self):
        """
        Surveille les positions ouvertes Capital.com.
        """
        if not self.broker.available:
            return

        open_pos  = self.broker.get_open_positions()
        open_refs = {
            p.get("position", {}).get("dealId")
            for p in open_pos
            if p.get("position", {}).get("dealId")
        }

        for instrument, state in list(self.positions.items()):
            if state is None:
                continue

            refs     = state["refs"]
            entry    = state["entry"]
            tp1_hit  = state.get("tp1_hit", False)

            ref_open = refs[0] in open_refs if refs[0] else False

            # ── M40: Dead Capital Detection (remplace time stop inline) ─────
            open_time = state.get("open_time")
            if open_time and ref_open:
                from brokers.capital_client import ASSET_PROFILES
                _prof = ASSET_PROFILES.get(instrument, {})
                max_hold_min = _prof.get("max_hold", 12) * 60

                # Get current price for M40 + M38
                _cur = None
                try:
                    _cur = self.broker.get_current_price(instrument)
                except Exception:
                    pass
                current_mid = _cur["mid"] if _cur else entry

                # M40: Dead Capital — stagnation + time stop
                should_kill, kill_reason = self.dead_capital.check_stagnation(
                    instrument, current_mid, state, max_hold_min
                )
                if should_kill:
                    name_ts = CAPITAL_NAMES.get(instrument, instrument)
                    logger.warning(kill_reason)
                    try:
                        self.broker.close_position(refs[0])
                    except Exception as _ts_e:
                        logger.debug(f"M40 close {refs[0]}: {_ts_e}")
                    if self.telegram.router:
                        self.telegram.router.send_trade(
                            f"⏱️ <b>M40 DEAD CAPITAL — {name_ts}</b>\n"
                            f"{kill_reason}"
                        )
                    self.convexity.unregister_trade(instrument)
                    self.positions[instrument] = None
                    self._pending_retest[instrument] = None
                    continue

                # M38: Trailing Stop — déplace le SL dynamiquement
                new_sl = self.convexity.update_trailing(instrument, current_mid)
                if new_sl is not None:
                    try:
                        self.broker.update_position(refs[0], stop_level=round(new_sl, 5))
                        state["sl"] = new_sl
                        logger.info(
                            f"🔒 M38 Trailing — {instrument}: SL → {new_sl:.5f}"
                        )
                    except Exception as _trail_e:
                        logger.debug(f"M38 trailing update {instrument}: {_trail_e}")

                # ═══ V1 ULTIMATE: Multi-TP Price Level Check ═══════════════
                # Check if TP1 level reached (poll-based fallback for WebSocket)
                if not state.get("tp1_hit"):
                    _tp1_level = state.get("tp1_level", state.get("tp1", 0))
                    _reached = False
                    if state["direction"] == "BUY" and current_mid >= _tp1_level:
                        _reached = True
                    elif state["direction"] == "SELL" and current_mid <= _tp1_level:
                        _reached = True
                    if _reached:
                        self._on_ws_be_triggered(instrument, current_mid, event="TP1")

                # Check if TP2 level reached (after TP1 hit) → partial close 40% + trailing
                if state.get("tp1_hit") and not state.get("tp2_hit"):
                    _tp2_level = state.get("tp2_level", state.get("tp2", 0))
                    _reached2 = False
                    if state["direction"] == "BUY" and current_mid >= _tp2_level:
                        _reached2 = True
                    elif state["direction"] == "SELL" and current_mid <= _tp2_level:
                        _reached2 = True
                    if _reached2:
                        self._on_ws_be_triggered(instrument, current_mid, event="TP2")

            # Position fermée (TP ou SL atteint) → reset
            if not ref_open:
                logger.info(f"✅ Capital.com {instrument} — toutes positions fermées")
                name_close = CAPITAL_NAMES.get(instrument, instrument)
                pip_close  = CAPITAL_PIP.get(instrument, 0.0001)
                close_px = entry
                pnl_est  = 0.0
                result   = "LOSS"
                size_per = state.get("size_tp3", state.get("size", 1.0))  # Multi-TP: seul 20% reste à la fermeture
                try:
                    current  = self.broker.get_current_price(instrument)
                    close_px = current["mid"] if current else entry
                    diff     = (close_px - entry) * (1 if state["direction"] == "BUY" else -1)
                    pips_pnl = round(diff / pip_close)
                    # Multi-TP mode: TP1 (40%) + TP2 (40%) déjà fermés — seul TP3 (20%) reste
                    pnl_est  = round(diff * size_per, 4)
                    result   = "WIN" if pnl_est > 0 else "LOSS"
                    self._capital_closed_today.append({
                        "instrument": instrument,
                        "pnl": pnl_est,
                        "direction": state["direction"],
                        "symbol": name_close,
                    })
                    # R-3: Record loss for kill-switch tracking
                    if result == "LOSS":
                        _cat = ASSET_PROFILES.get(instrument, {}).get("cat", "forex")
                        self.risk.record_loss(instrument, category=_cat)
                    # R-1: Record trade result for Kelly tracker
                    _risk_amt = abs(entry - state.get("sl", entry)) * size_per
                    self.risk.record_trade_result(instrument, pnl_est, _risk_amt)
                    # S-4: Record ML outcome for self-learning
                    _ml_feats = state.get("ml_features", {})
                    if _ml_feats and hasattr(self, 'ml_scorer') and self.ml_scorer:
                        self.ml_scorer.record_outcome(_ml_feats, won=(pnl_est > 0))
                    # F-6: also track monthly for leaderboard
                    self._capital_closed_month.append({
                        "instrument": instrument,
                        "pnl": pnl_est,
                        "direction": state["direction"],
                        "symbol": name_close,
                    })

                    # C-1: Telegram close notification
                    try:
                        duration_min = 0
                        if state.get("open_time"):
                            duration_min = int((datetime.now(timezone.utc) - state["open_time"]).total_seconds() / 60)
                        pnl_today = sum(t.get("pnl", 0) for t in self._capital_closed_today)
                        icon = "🟢" if pnl_est >= 0 else "🔴"
                        result_txt = "✅ WIN" if result == "WIN" else "❌ LOSS"
                        if self.telegram.router:
                            self.telegram.router.send_trade(
                                f"{icon} <b>Trade Fermé — {name_close}</b>\n\n"
                                f"  Direction : <b>{state['direction']}</b>\n"
                                f"  Entrée : <code>{entry:.5f}</code>\n"
                                f"  Sortie : <code>{close_px:.5f}</code>\n"
                                f"  Résultat : <b>{result_txt}</b>\n"
                                f"  PnL : <b>{pnl_est:+.2f}€</b> ({pips_pnl:+.0f} pips)\n"
                                f"  Durée : {duration_min} min\n\n"
                                f"📊 Bilan du jour : <b>{pnl_today:+.2f}€</b>"
                            )
                    except Exception as _tg_close:
                        logger.debug(f"Telegram close: {_tg_close}")

                    # ── Heatmap ──
                    try:
                        h_heat = datetime.now(timezone.utc).hour
                        if instrument not in self._heatmap_data:
                            self._heatmap_data[instrument] = {}
                        self._heatmap_data[instrument].setdefault(h_heat, []).append(pnl_est)
                    except Exception:
                        pass

                    # Session tracker
                    h_close = datetime.now(timezone.utc).hour
                    m_close = datetime.now(timezone.utc).minute
                    tracker_close = self._london_tracker if (h_close < 13 or (h_close == 13 and m_close < 30)) else self._ny_tracker
                    tracker_close.record_close(name=name_close, pnl=pnl_est, result=result)

                    # ── Feedback DRL + AB + LSTM ──
                    pnl_trade = pnl_est
                    won_trade = pnl_trade > 0
                    rr_trade  = abs(pnl_trade) / max(abs(diff * size_per), 0.0001)
                    try:
                        self.drl.record_trade(pnl_trade, rr_trade, state["direction"])
                    except Exception:
                        pass
                    try:
                        ab_v = state.get("ab_variant", "A")
                        _ab_regime = state.get("market_regime", "")
                        _ab_overlap = state.get("in_overlap", False)
                        self.ab.record_result(
                            instrument, ab_v, pnl_trade, won_trade,
                            regime=_ab_regime, is_overlap=_ab_overlap,
                        )
                    except Exception:
                        pass
                    try:
                        self.lstm.notify_trade_result(won_trade)
                    except Exception:
                        pass
                    # Wave 14: Gamification with overlap/session tracking
                    try:
                        _is_overlap = state.get("in_overlap", False)
                        _session = "overlap" if _is_overlap else (
                            "asia" if (h_close < 8) else
                            "london" if (h_close < 13) else "ny"
                        )
                        is_tp3 = state.get("tp1_hit") and state.get("tp2_hit")
                        if hasattr(self, 'telegram') and self.telegram.gamification:
                            newly = self.telegram.gamification.on_trade_closed(
                                won=won_trade, pnl=pnl_trade,
                                is_tp3_complete=is_tp3,
                                is_overlap=_is_overlap, session=_session,
                            )
                            for ach in self.telegram.gamification.pop_new_achievements():
                                if self.telegram.router:
                                    from nemesis_ui.notifications import NotificationFormatter
                                    self.telegram.router.send_to("stats",
                                        NotificationFormatter.format_achievement_unlocked(
                                            ach["name"], ach["desc"]
                                        )
                                    )
                    except Exception as _gam_e:
                        logger.debug(f"Gamification: {_gam_e}")
                    # Wave 13: Record price change for correlation
                    try:
                        if hasattr(self, 'context') and close_px and entry:
                            pct_chg = (close_px - entry) / entry * 100
                            self.context.record_price_change(instrument, pct_chg)
                    except Exception:
                        pass

                except Exception:
                    pass
                try:
                    self.db.close_position(instrument)
                except Exception:
                    pass
                try:
                    dash_close(symbol=name_close,
                               pnl=round(pnl_est, 2), result=result,
                               side=state["direction"])
                    self.reporter.record_trade(
                        symbol=name_close,
                        side=state["direction"],
                        result=result,
                        pnl_gross=round(pnl_est, 2),
                        entry=state["entry"],
                        exit_price=close_px,
                    )
                except Exception:
                    pass
                # Persistance Supabase async (non-bloquant)
                try:
                    duration_min_close = int((datetime.now(timezone.utc) - state["open_time"]).total_seconds() / 60) if state.get("open_time") else 0
                    self.db.close_position_async(
                        instrument,
                        pnl=round(pnl_est, 2),
                        result=result,
                        close_price=round(close_px, 5),
                        duration_min=duration_min_close,
                    )
                except Exception:
                    pass
                self.capital_ws.unwatch(instrument)
                self.convexity.unregister_trade(instrument)  # M38: cleanup trailing
                self.positions[instrument] = None
                self.risk.on_trade_closed(instrument=instrument)
                pnl_final = round(pnl_est, 2)
                self.protection.on_trade_closed(instrument, pnl_final)
                # Module 2: Dynamic Blacklist — enregistrer le résultat
                if hasattr(self, 'quarantine'):
                    self.quarantine.record_result(instrument, won=(result == "WIN"))
                # Moteur 8: RL Agent — enregistrer la transition post-close
                if hasattr(self, 'rl'):
                    try:
                        _trade_dur = round(
                            (datetime.now(timezone.utc) - state.get("opened_at", datetime.now(timezone.utc))).total_seconds() / 60, 1
                        ) if isinstance(state, dict) else 30.0
                        _score_v   = state.get("score", 0.5) if isinstance(state, dict) else 0.5
                        _rl_state  = [_score_v, pnl_final / 100, min(_trade_dur / 60, 1.0),
                                      0.0, 0.0, 0.0, 0.0, 0.0]
                        _rl_action = ACTION_BUY if state.get("direction") == "BUY" else ACTION_SELL if isinstance(state, dict) else ACTION_HOLD
                        _rl_reward = self.rl.compute_reward(pnl_final, _trade_dur)
                        self.rl.record_transition(_rl_state, _rl_action, _rl_reward, _rl_state, done=True)
                    except Exception as _rl_e:
                        logger.debug(f"RL record {instrument}: {_rl_e}")
                self.drift.record_trade(
                    pnl=pnl_final,
                    win=(result == "WIN"),
                    symbol=name_close,
                )
                try:
                    fresh_bal = self.broker.get_balance() or self.initial_balance
                    pnl_real  = round(fresh_bal - self.initial_balance, 2)
                    icon = "🟢" if pnl_final >= 0 else "🔴"
                    logger.info(
                        f"{icon} {name_close} {result} | PnL trade : {pnl_final:+.2f}€ "
                        f"| Balance : {fresh_bal:,.2f}€ | PnL total : {pnl_real:+.2f}€"
                    )
                except Exception:
                    pass

    def _detect_orphan_positions(self):
        """
        Orphan Position Detector — appel toutes les 5 min via state_sync.
        Detecte les positions broker non trackees dans positions (desync post-crash).
        """
        if not self.broker.available:
            return
        # CHANTIER 1: Déléguer au guardian si disponible (plus complet)
        if hasattr(self, 'guardian'):
            try:
                orphans = self.guardian.scan_orphans(self.positions)
                if orphans:
                    logger.warning(f"🔍 OrderGuardian: {len(orphans)} orphan(s) détecté(s)")
                return
            except Exception as _g_e:
                logger.debug(f"Guardian scan_orphans: {_g_e}")
        try:
            broker_positions = self.broker.get_open_positions()
            if not broker_positions:
                return
            known_refs: set = set()
            for _instr, _state in self.positions.items():
                if _state is None:
                    continue
                for _ref in _state.get("refs", []):
                    if _ref:
                        known_refs.add(str(_ref))
            for pos in broker_positions:
                deal_id = str(pos.get("position", {}).get("dealId", ""))
                epic = pos.get("market", {}).get("epic", "?")
                if deal_id and deal_id not in known_refs:
                    logger.critical(
                        f"ORPHAN POSITION -- epic={epic} dealId={deal_id} "
                        f"-- Position broker non trackee ! Resync necessaire."
                    )
        except Exception as _orp_e:
            logger.debug(f"Orphan detector: {_orp_e}")

    def _check_dead_man_switch(self):
        """
        CHANTIER 3 — Dead-Man Switch watchdog.
        Si aucun tick en 5 minutes → alerte critique.
        Appelé depuis la boucle de monitoring principale.
        """
        DEAD_MAN_TIMEOUT = 300  # 5 minutes
        ALERT_COOLDOWN   = 600  # Ré-alerte max toutes les 10 min (anti-spam)

        last_tick = getattr(self, '_last_tick_ts', None)
        if last_tick is None:
            return  # Pas encore initialisé (boot récent)

        elapsed = time.monotonic() - last_tick
        if elapsed > DEAD_MAN_TIMEOUT:
            logger.critical(
                f"🚨 DEAD-MAN SWITCH: Aucun tick depuis {elapsed:.0f}s "
                f"(limite: {DEAD_MAN_TIMEOUT}s) — bot potentiellement freezé !"
            )
            # ── Anti-spam cooldown ────────────────────────────────────────
            _now = time.monotonic()
            _last_alert = getattr(self, '_dms_last_alert_ts', 0)
            if _now - _last_alert < ALERT_COOLDOWN:
                return  # Déjà alerté récemment — pas de spam
            self._dms_last_alert_ts = _now

            # Alerte dead-man → Discord MONITORING (admin uniquement)
            try:
                import os as _os, requests as _rq
                _wh = _os.getenv("DISCORD_WEBHOOK_MONITORING", "")
                _proxy = _os.getenv("HTTPS_PROXY", "") or _os.getenv("HTTP_PROXY", "")
                _px = {"https": _proxy, "http": _proxy} if _proxy else {}
                if _wh:
                    _rq.post(_wh, json={"content": (
                        f"🚨 **DEAD-MAN SWITCH**\n"
                        f"⏱ Aucun tick depuis **{elapsed:.0f}s**\n"
                        f"🤖 Bot potentiellement freezé !\n"
                        f"🔧 `docker logs nemesis_bot`"
                    )}, proxies=_px, timeout=8)
            except Exception:
                pass

