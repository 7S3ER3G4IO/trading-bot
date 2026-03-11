"""
bot_signals.py — TradingBot._process_capital_symbol() — analyse + ouverture de position
"""
from .imports import *


class BotSignalsMixin:

    def _process_capital_symbol(self, instrument: str, balance: float):
        """
        Analyse un instrument Capital.com avec la stratégie London/NY Open Breakout.
        Ouvre 3 positions (taille/3) avec 3 niveaux de TP :
          TP1 = range × 1.5   (sortie rapide + déclencheur BE)
          TP2 = range × 3.0   (objectif principal)
          TP3 = range × 5.0   (laisser courir)
        """
        state = self.capital_trades.get(instrument)

        # Trade déjà ouvert — on ne re-entre pas
        if state is not None:
            return

        # Données depuis le cache OHLCV (A-2: pas de fetch REST sauf si périmé)
        _profile = ASSET_PROFILES.get(instrument, {})
        _tf = _profile.get("tf", "1h")
        _strat = _profile.get("strat", "BK")
        df = self.ohlcv_cache.get(instrument, strategy=self.strategy)
        if df is None or len(df) < 50:
            logger.warning(f"⚠️  {instrument}: OHLCV cache vide ou insuffisant ({len(df) if df is not None else 'None'} bougies) — skip")
            return

        # Indicators déjà calculés dans le cache — recompute seulement si absent
        if "atr" not in df.columns:
            df = self.strategy.compute_indicators(df)

        # ── UPGRADE : Retest Entry (Anti-Fakeout) ────────────────────────────
        pending = self._pending_retest.get(instrument)
        if pending:
            try:
                px_now = self.capital.get_current_price(instrument)
                if px_now:
                    mid       = px_now["mid"]
                    p_sig     = pending["sig"]
                    p_level   = pending["retest_level"]
                    p_atr     = pending["atr"]
                    tolerance = p_atr * 0.5
                    ticks     = pending.get("ticks_waited", 0) + 1
                    pending["ticks_waited"] = ticks

                    in_retest_zone = abs(mid - p_level) <= tolerance

                    if ticks > 6:
                        logger.info(f"⏳ Retest {instrument} expiré ({ticks} ticks) — annulé")
                        self._pending_retest[instrument] = None
                        return

                    if in_retest_zone:
                        logger.info(
                            f"🔄 RETEST CONFIRMÉ {instrument} {p_sig} "
                            f"| prix={mid:.5f} ≈ niveau={p_level:.5f} (±{tolerance:.5f})"
                        )
                        self._pending_retest[instrument] = None
                        sig          = p_sig
                        score        = pending["score"]
                        confirmations = pending["confirmations"] + ["Retest✓"]
                    else:
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
            sig, score, confirmations = self.strategy.get_signal(df, symbol=instrument, asset_profile=_profile)
            if sig == "HOLD":
                logger.info(f"📊 {instrument} [{_strat}] → HOLD | score={score} | {confirmations[:2] if confirmations else '∅'}")
                # ── Pre-signal check: detect setup forming ────────────────
                try:
                    pre = self.strategy.check_pre_signal(df, symbol=instrument, asset_profile=_profile)
                    if pre:
                        import threading
                        threading.Thread(
                            target=lambda: tgc.notify_pre_signal_alert(pre),
                            daemon=True,
                        ).start()
                except Exception as _ps_e:
                    logger.debug(f"Pre-signal check: {_ps_e}")
                return

            # Retest uniquement pour BK (MR/TF entrent directement)
            if _strat == "BK":
                _rlb = _profile.get("range_lb", 6)
                sr_now  = self.strategy.compute_session_range(df, range_lookback=_rlb)
                atr_now = self.strategy.get_atr(df)
                if atr_now > 0 and score < 0.60:
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
                        f"🔔 Breakout {instrument} {sig} score={score:.2f} | niveau={retest_level:.5f} "
                        f"| Attente retest (ATR={atr_now:.5f})…"
                    )
                    return
            logger.info(f"⚡ Entrée directe {instrument} [{_strat}] {sig} score={score:.2f}")


        # BUG FIX #5 : Vérification RiskManager + Kill-Switches (R-3/R-4)
        balance_for_risk = self.capital.get_balance() or balance
        _cat = _profile.get("cat", "forex")
        if not self.risk.can_open_trade(balance_for_risk, instrument=instrument, category=_cat):
            logger.info(f"⛔ {instrument} bloqué par RiskManager (DD/Kill-Switch/catégorie)")
            return

        # R-2: Currency exposure check
        if not self.risk.check_currency_exposure(instrument, sig, self.capital_trades):
            return

        # Protection Model : blacklist après 3 SL consécutifs
        if self.protection.is_blocked(instrument):
            return

        # MTFFilter : confluence 1h + 4h avant d'entrer
        if not self.mtf.validate_signal(instrument, sig):
            return

        # ── UPGRADE : Filtre Corrélation (évite surexposition USD) ────────────
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

        # ═══ SL / TP dynamiques par stratégie et actif ══════════════════════════════
        _sl_buf = _profile.get("sl_buffer", 0.10)
        _tp1_r  = _profile.get("tp1", 1.5)
        _tp2_r  = _profile.get("tp2", 3.0)
        _tp3_r  = _profile.get("tp3", 5.0)
        entry   = float(df.iloc[-1]["close"])
        direction = "BUY" if sig == "BUY" else "SELL"
        atr_val = self.strategy.get_atr(df)

        if _strat in ("MR", "TF"):
            # MR/TF: SL = ATR × sl_buffer, TP = multiples du SL distance
            sl_dist = atr_val * _sl_buf if atr_val > 0 else 0
            if sl_dist <= 0:
                return
            if sig == "BUY":
                sl  = entry - sl_dist
                tp1 = entry + sl_dist * _tp1_r
                tp2 = entry + sl_dist * _tp2_r
                tp3 = entry + sl_dist * _tp3_r
            else:
                sl  = entry + sl_dist
                tp1 = entry - sl_dist * _tp1_r
                tp2 = entry - sl_dist * _tp2_r
                tp3 = entry - sl_dist * _tp3_r
        else:
            # BK: SL = range low/high + buffer, TP = multiples du range
            sr  = self.strategy.compute_session_range(df)
            rng = sr["size"]
            if rng <= 0 or sr["pct"] < 0.08:
                return
            sl_dist = rng  # pour ADX adaptatif ci-dessous
            if sig == "BUY":
                sl  = sr["low"]  - rng * _sl_buf
                tp1 = entry + rng * _tp1_r
                tp2 = entry + rng * _tp2_r
                tp3 = entry + rng * _tp3_r
            else:
                sl  = sr["high"] + rng * _sl_buf
                tp1 = entry - rng * _tp1_r
                tp2 = entry - rng * _tp2_r
                tp3 = entry - rng * _tp3_r

        # ── Guard R:R minimum : TP1 doit valoir au moins 1.2× SL ──
        tp1_dist = abs(tp1 - entry)
        sl_dist_real = abs(sl - entry)
        if sl_dist_real > 0 and tp1_dist / sl_dist_real < 1.2:
            logger.info(
                f"⛔ {instrument} R:R trop faible : TP1={tp1_dist:.5f} / SL={sl_dist_real:.5f} "
                f"= {tp1_dist/sl_dist_real:.2f}x (min 1.2x) — skip"
            )
            return

        # R-1: Dynamic Kelly sizing (remplace risk_pct fixe à 0.5%)
        atr_20_avg = float(df["atr"].tail(20).mean()) if "atr" in df.columns and len(df) >= 20 else atr_val
        dynamic_risk = self.risk.compute_risk_pct(
            instrument=instrument, score=score,
            current_atr=atr_val, avg_atr=atr_20_avg
        )
        total_size = self.capital.position_size(
            balance=balance, risk_pct=dynamic_risk, entry=entry, sl=sl, epic=instrument
        )
        min_sz = CapitalClient.MIN_SIZE.get(instrument.upper(), 1.0)
        size1 = max(min_sz, round(total_size / 3, 2))

        # Cap max: chaque position ≤ 5% du balance en margin (levier 20:1)
        max_margin_per_pos = balance * 0.05
        max_size = max_margin_per_pos * 20 / max(entry, 0.01)
        if size1 > max_size:
            logger.info(f"📏 {instrument} size capped: {size1:.0f} → {max_size:.0f} (5% margin max)")
            size1 = round(max_size, 2)

        # Sprint 4 : Drift size reduction (50% si drift actif — safety net)
        if self._drift_size_reduced and self._drift_reduced_until and datetime.now(timezone.utc) < self._drift_reduced_until:
            size1 = max(min_sz, round(size1 * 0.5, 2))
            logger.debug(f"🔴 Drift reduction active — taille réduite à {size1}")

        # ── R:R Adaptatif (ADX > 30 = tendance forte) ─────────────
        adx_now = float(df.iloc[-1].get("adx", 0)) if "adx" in df.columns else 0
        if adx_now > 30 and sl_dist > 0:
            rr_tp2 = 2.5
            rr_tp3 = 4.0
            logger.info(f"📈 ADX={adx_now:.0f} > 30 — R:R étendu : TP2=×2.5R, TP3=×4.0R")
            if sig == "BUY":
                tp2 = entry + sl_dist * rr_tp2
                tp3 = entry + sl_dist * rr_tp3
            else:
                tp2 = entry - sl_dist * rr_tp2
                tp3 = entry - sl_dist * rr_tp3

        # ── HMM Regime Switching (block only, no size reduction) ──
        regime_result = {"name": "RANGING", "regime": 0, "confidence": 0.5}
        try:
            regime_result = self.hmm.detect_regime(df, symbol=instrument)
            regime_name   = regime_result["name"]
            regime_conf   = regime_result["confidence"]
            logger.debug(f"🧠 HMM Regime {instrument} : {regime_name} (conf={regime_conf:.0%})")

            if regime_result["regime"] == 1 and sig == "SELL" and regime_conf >= 0.65:
                logger.info(f"⛔ HMM TREND_UP ({regime_conf:.0%}) bloque SELL sur {instrument}")
                return
            elif regime_result["regime"] == 2 and sig == "BUY" and regime_conf >= 0.65:
                logger.info(f"⛔ HMM TREND_DOWN ({regime_conf:.0%}) bloque BUY sur {instrument}")
                return
        except Exception as _hmm_e:
            logger.debug(f"HMM {instrument}: {_hmm_e}")

        if size1 <= 0:
            return

        # ─── R-1: Round SL/TP to instrument's required decimal precision ────
        _dec = PRICE_DECIMALS.get(instrument, 5)
        sl  = round(sl,  _dec)
        tp1 = round(tp1, _dec)
        tp2 = round(tp2, _dec)
        tp3 = round(tp3, _dec)

        # ─── S-5: Dynamic Spread Filter (anti-frais) ─────────────────────
        try:
            _px = self.capital.get_current_price(instrument)
            if _px:
                _spread = abs(_px["ask"] - _px["bid"])
                _tp1_dist = abs(tp1 - entry)
                if _tp1_dist > 0 and _spread / _tp1_dist > 0.25:
                    logger.info(
                        f"⛔ S-5 Spread filter {instrument} | spread={_spread:.5f} "
                        f"/ TP1_dist={_tp1_dist:.5f} = {_spread/_tp1_dist:.0%} > 25% — skip"
                    )
                    return
        except Exception as _sp_e:
            logger.debug(f"Spread check {instrument}: {_sp_e}")

        # ─── ORDRES SÉQUENTIELS (anti-throttling Capital.com) ────
        import time as _time
        def _place(tp):
            return self.capital.place_market_order(instrument, direction, size1, sl, tp)

        ref1 = _place(tp1)
        _time.sleep(0.5)
        ref2 = _place(tp2)
        _time.sleep(0.5)
        ref3 = _place(tp3)

        if not any([ref1, ref2, ref3]):
            logger.warning(f"⛔ {instrument} — tous les ordres rejetés (marché fermé ou erreur)")
            return

        # EC-1: If TP1 order (ref1) was rejected but ref2/ref3 succeeded,
        # TP1 poll detection will never fire (refs[0] is None).
        # → Immediately mark tp1_hit and log the partial failure.
        _partial_tp1_failed = (ref1 is None and any([ref2, ref3]))

        # ─── WebSocket monitoring BE temps réel ───
        self.capital_ws.watch(
            instrument=instrument,
            entry=entry,
            tp1=tp1,
            tp2=tp2,
            tp1_ref=ref1 or "",
            ref2=ref2 or "",
            ref3=ref3 or "",
        )

        # ─── Sauvegarde état ──────────────────────────────────────
        self.capital_trades[instrument] = {
            "refs":      [ref1, ref2, ref3],
            "entry":     entry,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "tp3":       tp3,
            "direction": direction,
            "size":      size1,  # F-2: stored for PnL calculation
            "tp1_hit":   _partial_tp1_failed,  # EC-1: auto-BE if TP1 order failed
            "tp2_hit":   False,
            "score":      score,
            "confirmations": confirmations,
            "regime":    regime_result.get("name", "RANGING"),
            "fear_greed": self.context._fg_value,
            "in_overlap": False,
            "adx_at_entry": adx_now,
            "open_time":  datetime.now(timezone.utc),
        }
        if _partial_tp1_failed:
            logger.warning(
                f"⚠️ {instrument} — ref1 (TP1) rejeté, tp1_hit=True auto — "
                f"BE immédiat sur pos2/pos3"
            )
        name    = CAPITAL_NAMES.get(instrument, instrument)
        hour    = datetime.now(timezone.utc).hour
        minute  = datetime.now(timezone.utc).minute
        session = "London" if (hour < 13 or (hour == 13 and minute < 30)) else "NY"
        tracker = self._london_tracker if session == "London" else self._ny_tracker
        tracker.record_entry(name=name, sig=sig, entry=entry, size=size1)

        self.risk.on_trade_opened(instrument=instrument)

        try:
            self.db.save_capital_trade(instrument, self.capital_trades[instrument])
        except Exception as exc:
            logger.warning(f"⚠️ DB save_capital_trade open: {exc}")

        logger.info(f"✅ Capital.com {sig} {instrument} @ {entry:.5f} | SL={sl:.5f} TP1={tp1:.5f} TP2={tp2:.5f} TP3={tp3:.5f}")

        # ─── Telegram en background ────────
        import threading
        # sr disponible seulement pour BK, fallback pour MR/TF
        _range_pct  = sr["pct"]  if _strat == "BK" else 0.0
        _range_high = sr["high"] if _strat == "BK" else entry
        _range_low  = sr["low"]  if _strat == "BK" else entry
        _snap = dict(instrument=instrument, name=name, sig=sig,
                     entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                     size=size1, score=score, session=session,
                     range_pct=_range_pct, range_high=_range_high, range_low=_range_low,
                     confirmations=list(confirmations), df=df.copy())
        threading.Thread(target=lambda: tgc.notify_capital_entry(**_snap), daemon=True).start()

        # ── Signal Card visuel ──
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
                if img and self.telegram.router:
                    caption = (
                        f"📸 <b>{'🟢 BUY' if direction == 'BUY' else '🔴 SELL'} "
                        f"{name}</b>  |  Score {score}/7\n"
                        f"💰 Entry: {entry:.5f}  |  SL: {sl:.5f}\n"
                        f"🎯 TP1: {tp1:.5f}  TP2: {tp2:.5f}  TP3: {tp3:.5f}"
                    )
                    self.telegram.router.send_photo_to("trades", img, caption=caption)
            threading.Thread(target=_do_send_card, daemon=True).start()
        except Exception as _kex:
            logger.debug(f"Signal card: {_kex}")

        if self.capital_trades[instrument]:
            self.capital_trades[instrument]["ab_variant"] = self.ab.get_variant(instrument)

        try:
            dash_open(symbol=CAPITAL_NAMES.get(instrument, instrument),
                      side=direction, entry=entry, qty=size1)
        except Exception:
            pass
