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
            if self.capital_trades.get(epic) is not None:
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
                bal = self.capital.get_balance() if self.capital.available else 0.0
                if bal > 0:
                    self._process_capital_symbol(epic, bal)
        except Exception as _ws_e:
            logger.debug(f"WS price_tick {epic}: {_ws_e}")

    def _on_ws_be_triggered(self, instrument: str, entry_or_sl: float, event: str = "TP1"):
        """
        Callback WebSocket — appelé en < 500ms quand TP1 ou TP2 est franchi.
        """
        state = self.capital_trades.get(instrument)
        if state is None:
            return

        name = CAPITAL_NAMES.get(instrument, instrument)
        pip  = CAPITAL_PIP.get(instrument, 0.0001)

        if event == "TP1":
            # In 1-TP mode, TP1 = full close. Mark and let poll detect it.
            state["tp1_hit"] = True
            tp1_price = state.get("tp1", state["entry"])
            pip_ws    = CAPITAL_PIP.get(instrument, 0.0001)
            pips_tp1  = round(abs(tp1_price - state["entry"]) / pip_ws)
            logger.info(f"🎯 WS TP touché — {instrument} | {pips_tp1} pips")

    def _monitor_capital_positions(self):
        """
        Surveille les positions ouvertes Capital.com.
        """
        if not self.capital.available:
            return

        open_pos  = self.capital.get_open_positions()
        open_refs = {
            p.get("position", {}).get("dealId")
            for p in open_pos
            if p.get("position", {}).get("dealId")
        }

        for instrument, state in list(self.capital_trades.items()):
            if state is None:
                continue

            refs     = state["refs"]
            entry    = state["entry"]
            tp1_hit  = state.get("tp1_hit", False)

            ref_open = refs[0] in open_refs if refs[0] else False

            # ── Time-based Stop Loss (max_hold, default 12h) ────────────────
            open_time = state.get("open_time")
            if open_time and ref_open:
                age_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                from brokers.capital_client import ASSET_PROFILES
                _prof = ASSET_PROFILES.get(instrument, {})
                max_hold_min = _prof.get("max_hold", 12) * 60
                if age_minutes > max_hold_min:
                    name_ts = CAPITAL_NAMES.get(instrument, instrument)
                    logger.warning(
                        f"⏱️  Time-Stop {instrument} — {age_minutes:.0f}min → fermeture forcée"
                    )
                    try:
                        self.capital.close_position(refs[0])
                    except Exception as _ts_e:
                        logger.debug(f"Time-stop close {refs[0]}: {_ts_e}")
                    if self.telegram.router:
                        self.telegram.router.send_trade(
                            f"⏱️ <b>Time-Stop — {name_ts}</b>\n"
                            f"Ouvert depuis <b>{age_minutes:.0f} min</b> → fermé."
                        )
                    self.capital_trades[instrument] = None
                    self._pending_retest[instrument] = None
                    continue

            # Position fermée (TP ou SL atteint) → reset
            if not ref_open:
                logger.info(f"✅ Capital.com {instrument} — toutes positions fermées")
                name_close = CAPITAL_NAMES.get(instrument, instrument)
                pip_close  = CAPITAL_PIP.get(instrument, 0.0001)
                close_px = entry
                pnl_est  = 0.0
                result   = "LOSS"
                size_per = state.get("size", 1.0)  # C-2: actual position size
                try:
                    current  = self.capital.get_current_price(instrument)
                    close_px = current["mid"] if current else entry
                    diff     = (close_px - entry) * (1 if state["direction"] == "BUY" else -1)
                    pips_pnl = round(diff / pip_close)
                    # 1-TP mode: 1 position only
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
                    self.db.close_capital_trade(instrument)
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
                    self.db.close_capital_trade_async(
                        instrument,
                        pnl=round(pnl_est, 2),
                        result=result,
                        close_price=round(close_px, 5),
                        duration_min=duration_min_close,
                    )
                except Exception:
                    pass
                self.capital_ws.unwatch(instrument)
                self.capital_trades[instrument] = None
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
                    fresh_bal = self.capital.get_balance() or self.initial_balance
                    pnl_real  = round(fresh_bal - self.initial_balance, 2)
                    icon = "🟢" if pnl_final >= 0 else "🔴"
                    logger.info(
                        f"{icon} {name_close} {result} | PnL trade : {pnl_final:+.2f}€ "
                        f"| Balance : {fresh_bal:,.2f}€ | PnL total : {pnl_real:+.2f}€"
                    )
                except Exception:
                    pass
