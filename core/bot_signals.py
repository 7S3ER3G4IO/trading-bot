"""
bot_signals.py — TradingBot._process_capital_symbol() — analyse + ouverture de position
"""
from .imports import *


class BotSignalsMixin:

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

        # Données selon le timeframe du profil (4h ou 1d)
        _profile = ASSET_PROFILES.get(instrument, {})
        _tf = _profile.get("tf", "5m")   # V7=4h, V6=1d, fallback=5m
        _count = {"4h": 200, "1d": 100, "5m": 300}.get(_tf, 300)
        df = self.capital.fetch_ohlcv(instrument, timeframe=_tf, count=_count)
        if df is None or len(df) < 50:
            logger.warning(f"⚠️  {instrument}: OHLCV None ou insuffisant ({len(df) if df is not None else 'None'} bougies) — skip")
            return

        df = self.strategy.compute_indicators(df)
        _strat = _profile.get("strat", "BK")

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
                if atr_now > 0 and score < 2:
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
                        f"🔔 Breakout {instrument} {sig} score={score} | niveau={retest_level:.5f} "
                        f"| Attente retest (ATR={atr_now:.5f})…"
                    )
                    return
            logger.info(f"⚡ Entrée directe {instrument} [{_strat}] {sig} score={score}")


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
        _tp1_r  = _profile.get("tp1", 0.8)
        _tp2_r  = _profile.get("tp2", 1.8)
        _tp3_r  = _profile.get("tp3", 3.0)
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

        # Taille totale puis split en 3 (Kelly/8 = 1.25%)
        total_size = self.capital.position_size(
            balance=balance, risk_pct=0.0125, entry=entry, sl=sl, epic=instrument
        )
        min_sz = CapitalClient.MIN_SIZE.get(instrument.upper(), 0.01)
        size1 = max(min_sz, round(total_size / 3, 2))

        # ── SPRINT FINAL T : DRL Size Multiplier ──
        drl_mult = self.drl.get_multiplier()
        if drl_mult != 1.0:
            size1 = max(min_sz, round(size1 * drl_mult, 2))
            logger.debug(f"🎯 DRL size mult={drl_mult:.2f}× → size1={size1}")

        # Sprint 4 : Drift size reduction (50% si drift actif)
        if self._drift_size_reduced and self._drift_reduced_until and datetime.now(timezone.utc) < self._drift_reduced_until:
            size1 = max(min_sz, round(size1 * 0.5, 2))
            logger.debug(f"🔴 Drift reduction active — taille réduite à {size1}")

        # ── UPGRADE : Session Overlap Boost (13h-17h UTC = volume max) ─────
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

        # ── UPGRADE : HMM Regime Switching ───────────────────────────────
        regime_result = {"name": "RANGING", "regime": 0, "confidence": 0.5}
        try:
            regime_result = self.hmm.detect_regime(df, symbol=instrument)
            regime_name   = regime_result["name"]
            regime_conf   = regime_result["confidence"]
            logger.debug(f"🧠 HMM Regime {instrument} : {regime_name} (conf={regime_conf:.0%})")

            if regime_result["regime"] == 0 and regime_conf >= 0.6:
                size1 = max(min_sz, round(size1 * 0.5, 2))
                logger.info(f"🔶 HMM RANGING ({regime_conf:.0%}) — taille réduite à {size1}")
            elif regime_result["regime"] == 1 and sig == "SELL" and regime_conf >= 0.65:
                logger.info(f"⛔ HMM TREND_UP ({regime_conf:.0%}) bloque SELL sur {instrument}")
                return
            elif regime_result["regime"] == 2 and sig == "BUY" and regime_conf >= 0.65:
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
            "tp1_hit":   False,
            "tp2_hit":   False,
            "score":      score,
            "confirmations": confirmations,
            "regime":    regime_result.get("name", "RANGING"),
            "fear_greed": self.context._fg_value,
            "in_overlap": in_overlap,
            "adx_at_entry": adx_now,
            "open_time":  datetime.now(timezone.utc),
        }
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
