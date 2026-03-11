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
            state["tp1_hit"] = True
            # Use actual TP1 distance, not hardcoded 0.8
            tp1_price = state.get("tp1", state["entry"])
            pips_tp1 = round(abs(tp1_price - state["entry"]) / pip)
            logger.info(f"⚡ WS BE instant — {instrument} @ {entry_or_sl:.5f}")
            tgc.notify_tp1_be(
                name=name, instrument=instrument,
                entry=entry_or_sl, pips_tp1=pips_tp1, size=0,
            )
        elif event == "TP2":
            state["tp2_hit"] = True
            pips_tp2 = round(abs(state["entry"] - entry_or_sl) / pip)
            logger.info(f"⚡ WS TP2 trailing activé — {instrument} SL pos3 → {entry_or_sl:.5f}")
            if self.telegram.router:
                self.telegram.router.send_trade(
                    f"🎯 <b>TP2 touché — {name}</b>\n"
                    f"SL pos3 déplacé à TP1 (<code>{entry_or_sl:.5f}</code>)\n"
                    f"🟢 Gains TP1 verrouillés sur position 3 !"
                )

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
            tp1_hit  = state["tp1_hit"]

            ref1_open = refs[0] in open_refs if refs[0] else False
            ref2_open = refs[1] in open_refs if refs[1] else False
            ref3_open = refs[2] in open_refs if refs[2] else False

            # ── Time-based Stop Loss (max_hold from profile, default 12h) ──────
            if not state.get("tp1_hit"):
                open_time = state.get("open_time")
                if open_time:
                    age_minutes = (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                    from brokers.capital_client import ASSET_PROFILES
                    _prof = ASSET_PROFILES.get(instrument, {})
                    max_hold_min = _prof.get("max_hold", 12) * 60
                    if age_minutes > max_hold_min:
                        name_ts = CAPITAL_NAMES.get(instrument, instrument)
                        logger.warning(
                            f"⏱️  Time-Stop {instrument} — {age_minutes:.0f}min sans TP1 "
                            f"→ fermeture forcée"
                        )
                        for ref in refs:
                            if ref and ref in open_refs:
                                try:
                                    self.capital.close_position(ref)
                                except Exception as _ts_e:
                                    logger.debug(f"Time-stop close {ref}: {_ts_e}")
                        if self.telegram.router:
                            self.telegram.router.send_trade(
                                f"⏱️ <b>Time-Stop déclenché — {name_ts}</b>\n"
                                f"Ouvert depuis <b>{age_minutes:.0f} min</b> sans atteindre TP1.\n"
                                f"Fermeture de toutes les positions (trade zombie évité)."
                            )
                        self.capital_trades[instrument] = None
                        self._pending_retest[instrument] = None
                        continue


            # TP1 touché si ref1 a disparu (fallback polling)
            if not tp1_hit and refs[0] and not ref1_open:
                state["tp1_hit"] = True
                name = CAPITAL_NAMES.get(instrument, instrument)
                pip  = CAPITAL_PIP.get(instrument, 0.0001)
                # Use actual TP1 distance
                tp1_price = state.get("tp1", entry)
                pips_tp1 = round(abs(tp1_price - entry) / pip)
                logger.info(f"🎯 [POLL FALLBACK] TP1 touché {instrument} — activation Break-Even")

                for ref in [refs[1], refs[2]]:
                    if ref and ref in open_refs:
                        # BE at entry + 1 pip (covers spread)
                        be_price = entry + pip if state.get("direction") == "BUY" else entry - pip
                        self.capital.modify_position_stop(ref, be_price)

                try:
                    self.db.save_capital_trade(instrument, state)
                except Exception:
                    pass

                tgc.notify_tp1_be(name=name, instrument=instrument,
                                   entry=entry, pips_tp1=pips_tp1, size=0)

            # ── ATR Trailing Stop (après TP1 / BE activé) ───────────────────
            if state.get("tp1_hit"):
                try:
                    df_trail = self.capital.fetch_ohlcv(instrument, "5m", count=20)
                    if df_trail is not None and len(df_trail) >= 14:
                        df_trail = self.strategy.compute_indicators(df_trail)
                        atr = self.strategy.get_atr(df_trail)
                        if atr > 0:
                            px = self.capital.get_current_price(instrument)
                            if px:
                                mid = px["mid"]
                                direction = state.get("direction", "BUY")
                                if direction == "BUY":
                                    new_trail_sl = round(mid - atr * 1.5, 5)
                                    # Minimum = BE price (entry + 1pip), never below
                                    be_floor = entry + pip
                                    new_trail_sl = max(new_trail_sl, be_floor)
                                else:
                                    new_trail_sl = round(mid + atr * 1.5, 5)
                                    be_floor = entry - pip
                                    new_trail_sl = min(new_trail_sl, be_floor)

                                for ref in [refs[1], refs[2]]:
                                    if ref and ref in open_refs:
                                        self.capital.modify_position_stop(ref, new_trail_sl)
                                        time.sleep(0.3)  # R-2: rate-limit trailing API calls
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
                    # C-2: PnL = price_diff × size × n_positions
                    n_positions = sum(1 for r in refs if r)
                    pnl_est  = round(diff * size_per * n_positions, 4)
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
                    _risk_amt = abs(entry - state.get("sl", entry)) * size_per * n_positions
                    self.risk.record_trade_result(instrument, pnl_est, _risk_amt)
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
                        self.ab.record_result(instrument, ab_v, pnl_trade, won_trade)
                    except Exception:
                        pass
                    try:
                        self.lstm.notify_trade_result(won_trade)
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
                self.capital_ws.unwatch(instrument)
                self.capital_trades[instrument] = None
                self.risk.on_trade_closed(instrument=instrument)
                pnl_final = round(pnl_est, 2)
                self.protection.on_trade_closed(instrument, pnl_final)
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
