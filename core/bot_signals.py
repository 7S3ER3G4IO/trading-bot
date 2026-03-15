"""
bot_signals.py — TradingBot._process_capital_symbol() — analyse + ouverture de position
"""
from .imports import *


class BotSignalsMixin:

    def _process_capital_symbol(self, instrument: str, balance: float):
        """
        Analyse un instrument Capital.com avec la stratégie London/NY Open Breakout.
        Ouvre 1 position avec TP unique (mode 1-TP anti-throttling).
        """
        state = self.positions.get(instrument)

        # Trade déjà ouvert — on ne re-entre pas
        if state is not None:
            return

        # ─── GOD MODE: HARD BAN — zero compute, instant reject ──────────────
        try:
            from god_mode import HARD_BAN
            if instrument in HARD_BAN:
                return  # Silently drop — these assets are mathematically untradeable
        except ImportError:
            pass

        # ─── Module 2: Dynamic Blacklist — vérification quarantaine ──────────
        if hasattr(self, 'quarantine') and self.quarantine.is_quarantined(instrument):
            logger.debug(f"🚫 {instrument} en quarantaine — skipped")
            return

        # ─── MOTEUR 9: VPIN Toxicity Shield ──────────────────────────────────
        if hasattr(self, 'vpin'):
            try:
                _toxic, _vpin_score, _vpin_level = self.vpin.is_toxic(instrument)
                if _toxic:
                    logger.info(
                        f"🛡️ VPIN {_vpin_level}: {instrument} score={_vpin_score:.3f} → entrée bloquée"
                    )
                    return
            except Exception as _vpin_e:
                logger.debug(f"VPIN {instrument}: {_vpin_e}")

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

        # ── VOLUME FILTER : évite les faux breakouts en faible liquidité ────────
        # Si le volume du dernier bar < 50% de la moyenne sur 20 bars → skip
        try:
            if "volume" in df.columns and len(df) >= 20:
                _vol_now = float(df["volume"].iloc[-1])
                _vol_avg = float(df["volume"].rolling(20).mean().iloc[-1])
                if _vol_avg > 0 and _vol_now < _vol_avg * 0.5:
                    logger.info(
                        f"⛔ S-6 Volume filter {instrument}: vol={_vol_now:.0f} < "
                        f"50% avg({_vol_avg:.0f}) — faible liquidité, skip"
                    )
                    return
        except Exception:
            pass

        # ── UPGRADE : Retest Entry (Anti-Fakeout) ────────────────────────────
        pending = self._pending_retest.get(instrument)
        if pending:
            try:
                px_now = self.broker.get_current_price(instrument)
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
                # ── Pre-signal: log only (disabled from Telegram for clean feed)
                pass
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
        balance_for_risk = self.broker.get_balance() or balance
        _cat = _profile.get("cat", "forex")
        if not self.risk.can_open_trade(balance_for_risk, instrument=instrument, category=_cat):
            logger.info(f"⛔ {instrument} bloqué par RiskManager (DD/Kill-Switch/catégorie)")
            return

        # Bug #7 fix: Portfolio heat check (secteur, corrélation, risque cumulé)
        _ph_ok, _ph_reason = self.risk.portfolio_heat_check(
            instrument=instrument, direction=sig,
            open_trades=self.positions, risk_pct=0.01,
        )
        if not _ph_ok:
            logger.info(f"⛔ Portfolio Heat {instrument}: {_ph_reason}")
            return

        # R-2: Currency exposure check
        if not self.risk.check_currency_exposure(instrument, sig, self.positions):
            return

        # Protection Model : blacklist après 3 SL consécutifs
        if self.protection.is_blocked(instrument):
            return

        # ─── CORRELATION FILTER : bloque si pair corrélée déjà ouverte ──────
        if hasattr(self, 'corr_filter'):
            try:
                _can_open, _corr_reason = self.corr_filter.can_open(instrument, self.positions)
                if not _can_open:
                    logger.info(f"⛔ CorrelationFilter {instrument}: {_corr_reason}")
                    return
                _dir_ok, _dir_reason = self.corr_filter.same_direction_check(
                    instrument, sig, self.positions, max_same_direction=3
                )
                if not _dir_ok:
                    logger.info(f"⛔ CorrelationFilter directionnel {instrument}: {_dir_reason}")
                    return
            except Exception as _cf_e:
                logger.debug(f"CorrelationFilter {instrument}: {_cf_e}")

        # ─── MAX TRADES PAR INSTRUMENT PAR JOUR ────────────────────────────
        _max_per_day = int(os.getenv("MAX_TRADES_PER_INST_DAY", "2"))
        _today_count = getattr(self, '_daily_inst_trades', {}).get(instrument, 0)
        if _today_count >= _max_per_day:
            logger.info(
                f"⛔ Max {_max_per_day} trades/jour atteint pour {instrument} "
                f"({_today_count}/{_max_per_day})"
            )
            return

        # S-2: MTF Confluence Scoring (replace binary blocking)

        mtf_bonus = self.mtf.score_confluence(instrument, sig)
        score = score + mtf_bonus  # Adjust score with MTF bonus/penalty
        if mtf_bonus < -0.05:
            logger.info(f"⛔ MTF contra-signal {instrument} {sig} — bonus={mtf_bonus:+.2f} → skip")
            return
        elif mtf_bonus > 0:
            confirmations.append(f"MTF{'+' if mtf_bonus > 0 else ''}{mtf_bonus:.2f}")
            logger.debug(f"✅ MTF bonus {instrument} {sig}: {mtf_bonus:+.2f}")

        # S-4: ML Score Adjustment (self-learning)
        _ml_features = {}
        if hasattr(self, 'ml_scorer') and self.ml_scorer:
            _spread_ratio = 0.0
            try:
                _px = self.broker.get_current_price(instrument)
                if _px:
                    _sprd = abs(_px["ask"] - _px["bid"])
                    _tp1_d = abs(float(df.iloc[-1]["close"]) - (float(df.iloc[-1]["close"]) + float(df.iloc[-1].get("atr", 0.001))))
                    _spread_ratio = _sprd / _tp1_d if _tp1_d > 0 else 0
            except Exception:
                pass
            _ml_features = self.ml_scorer.extract_features(
                df, instrument, score, mtf_bonus=mtf_bonus, spread_ratio=_spread_ratio
            )
            if _ml_features:
                score = self.ml_scorer.score_adjustment(score, _ml_features)
                confirmations.append(f"ML:{self.ml_scorer.predict_win_probability(_ml_features):.0%}")

        # ═══ SL / TP dynamiques par stratégie et actif ══════════════════════════════
        _sl_buf = _profile.get("sl_buffer", 0.10)
        _tp_r   = _profile.get("tp1", 1.5)   # Single TP mode
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
                tp1 = entry + sl_dist * _tp_r
            else:
                sl  = entry + sl_dist
                tp1 = entry - sl_dist * _tp_r
        else:
            # BK: SL = range low/high + buffer, TP = multiples du range
            sr  = self.strategy.compute_session_range(df)
            rng = sr["size"]
            if rng <= 0 or sr["pct"] < 0.08:
                return
            sl_dist = rng  # pour ADX adaptatif ci-dessous
            if sig == "BUY":
                sl  = sr["low"]  - rng * _sl_buf
                tp1 = entry + rng * _tp_r
            else:
                sl  = sr["high"] + rng * _sl_buf
                tp1 = entry - rng * _tp_r

        # ── V1 ULTIMATE: Multi-TP Levels (1.5R / 2.5R / Trail) ────────────
        _sl_distance = abs(entry - sl)
        if _sl_distance > 0:
            if sig == "BUY":
                _tp1_multi = entry + _sl_distance * 1.5   # TP1 = 1.5R
                _tp2_multi = entry + _sl_distance * 2.5   # TP2 = 2.5R
            else:
                _tp1_multi = entry - _sl_distance * 1.5
                _tp2_multi = entry - _sl_distance * 2.5
            # Broker order uses TP1 as initial target
            tp1 = _tp1_multi
        else:
            _tp1_multi = tp1
            _tp2_multi = tp1

        # ── M38 Convexity Gate : R:R minimum OBLIGATOIRE ──
        rr_valid, actual_rr = self.convexity.validate_rr(entry, sl, tp1, instrument)
        if not rr_valid:
            logger.warning(
                f"⛔ M38 Convexity: {instrument} R:R={actual_rr:.2f} < {MIN_RR_RATIO} — TRADE REJETÉ"
            )
            return
        # Ajuster TP si nécessaire pour garantir R:R minimum
        sl, tp1 = self.convexity.enforce_minimum_rr(entry, sl, tp1, direction, instrument)

        # R-1: Dynamic Kelly sizing (remplace risk_pct fixe à 0.5%)
        atr_20_avg = float(df["atr"].tail(20).mean()) if "atr" in df.columns and len(df) >= 20 else atr_val
        dynamic_risk = self.risk.compute_risk_pct(
            instrument=instrument, score=score,
            current_atr=atr_val, avg_atr=atr_20_avg
        )

        # Wave 12: Apply market regime multiplier to sizing
        regime_mult = self.context.get_regime_multiplier() if hasattr(self, 'context') else 1.0
        dynamic_risk *= regime_mult
        if regime_mult != 1.0:
            logger.info(f"🌍 Regime {self.context.regime} → sizing ×{regime_mult:.1f} → risk={dynamic_risk:.3f}")

        total_size = self.broker.position_size(
            balance=balance, risk_pct=dynamic_risk, entry=entry, sl=sl, epic=instrument
        )
        min_sz = self.broker.MIN_SIZE.get(instrument.upper(), 0.01)  # 0.01 lot min (MT5 standard)
        size1 = max(min_sz, round(total_size / 3, 2))

        # Cap max: chaque position ≤ 15% du balance en margin (Aggressive Growth)
        max_margin_per_pos = balance * 0.15
        max_size = max_margin_per_pos * 20 / max(entry, 0.01)
        if size1 > max_size:
            logger.info(f"📏 {instrument} size capped: {size1:.0f} → {max_size:.0f} (15% margin max)")
            size1 = round(max_size, 2)

        # Sprint 4 : Drift size reduction (50% si drift actif — safety net)
        if self._drift_size_reduced and self._drift_reduced_until and datetime.now(timezone.utc) < self._drift_reduced_until:
            size1 = max(min_sz, round(size1 * 0.5, 2))
            logger.debug(f"🔴 Drift reduction active — taille réduite à {size1}")

        # ── ADX tracking (for state) ─────────────
        adx_now = float(df.iloc[-1].get("adx", 0)) if "adx" in df.columns else 0

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

        # ─── MARGIN CHECK PRE-TRADE ───────────────────────────────────────────
        # SDK v29 fix: get_account_information() supprimé → terminal_state.account_information
        try:
            _acct_info = None
            if hasattr(self.broker, '_connection') and self.broker._connection:
                _ts = getattr(self.broker._connection, 'terminal_state', None)
                if _ts:
                    _acct_info = getattr(_ts, 'account_information', None)
            if _acct_info:
                _free_margin = float(_acct_info.get("freeMargin", _acct_info.get("equity", balance)))
                _cat = ASSET_PROFILES.get(instrument, {}).get("cat", "forex")
                from config import ASSET_MARGIN_REQUIREMENTS
                _margin_rate = ASSET_MARGIN_REQUIREMENTS.get(_cat, 0.0333)
                _margin_needed = size1 * entry * _margin_rate
                if _margin_needed > _free_margin * 0.80:
                    logger.warning(
                        f"⛔ MARGIN CHECK {instrument}: besoin={_margin_needed:.2f} "
                        f"> 80% du libre={_free_margin * 0.80:.2f} — ordre annulé"
                    )
                    return
        except Exception as _mc_e:
            logger.debug(f"Margin check {instrument}: {_mc_e}")

        # ─── S-5: Dynamic Spread Filter (anti-frais) ─────────────────────
        try:
            _px = self.broker.get_current_price(instrument)
            if _px:
                _spread = abs(_px["ask"] - _px["bid"])
                _tp1_dist = abs(tp1 - entry)
                if _tp1_dist > 0 and _spread / _tp1_dist > 0.25:
                    logger.info(
                        f"⛔ S-5 Spread filter {instrument} | spread={_spread:.5f} "
                        f"/ TP_dist={_tp1_dist:.5f} = {_spread/_tp1_dist:.0%} > 25% — skip"
                    )
                    return
        except Exception as _sp_e:
            logger.debug(f"Spread check {instrument}: {_sp_e}")

        # ─── MOTEUR 1: Volatility-Adjusted TP/SL ─────────────────────────────
        if hasattr(self, 'vol_adjuster'):
            try:
                adj_sl, adj_tp1, adj_size = self.vol_adjuster.adjust(
                    df=df, entry=entry, sl=sl, tp1=tp1,
                    direction=direction,
                    risk_pct=dynamic_risk,
                    balance=balance,
                )
                sl   = adj_sl   # SL ajusté à la volatilité
                tp1  = adj_tp1  # TP ajusté à la volatilité
                if adj_size is not None:
                    size1 = max(round(adj_size, 2), 0.1)
            except Exception as _va_e:
                logger.debug(f"VolAdj {instrument}: {_va_e}")

        # ─── MOTEUR 10: HMM Black-Litterman Kelly Multiplier ─────────────────
        if hasattr(self, 'hmm'):
            try:
                _hmm_mult = self.hmm.get_kelly_multiplier(instrument)
                _regime   = self.hmm.get_current_regime()
                size1     = max(round(size1 * _hmm_mult, 2), 0.01)
                logger.debug(
                    f"🎲 HMM [{_regime}] mult={_hmm_mult:.2f}x → size={size1}"
                )
            except Exception as _hmm_e:
                logger.debug(f"HMM {instrument}: {_hmm_e}")

        # ─── MOTEUR 2: Order Book Imbalance Guard (async, fail-open 0.5s) ───
        if hasattr(self, 'ob_guard'):
            try:
                _ob_allowed, _ob_reason = self.ob_guard.check(
                    instrument=instrument, direction=direction, df=df, entry=entry
                )
                if not _ob_allowed:
                    logger.info(f"🛡️ OBGuard: {instrument} {direction} bloqué — {_ob_reason}")
                    return
            except Exception as _ob_e:
                logger.debug(f"OBGuard {instrument}: {_ob_e}")

        # ─── MOTEUR 4: ML Predictive Score ───────────────────────────────────
        if hasattr(self, 'ml_engine'):
            try:
                _hour = datetime.now(timezone.utc).hour
                _ml_score = self.ml_engine.predict(df, direction, instrument)
                if _ml_score < 0.42:
                    logger.info(
                        f"🧠 ML Filter: {instrument} {direction} score={_ml_score:.2f} < 0.42 → skip"
                    )
                    return
                logger.debug(f"🧠 ML: {instrument} score={_ml_score:.2f} ✅")
            except Exception as _ml_e:
                logger.debug(f"ML {instrument}: {_ml_e}")

        # ─── MOTEUR 5: Alt-Data Sentiment Filter ─────────────────────────────
        if hasattr(self, 'alt_data'):
            try:
                _alt_blocked, _alt_reason = self.alt_data.should_block_entry(
                    instrument, direction
                )
                if _alt_blocked:
                    logger.info(f"📡 AltData: {instrument} {direction} bloqué — {_alt_reason}")
                    return
            except Exception as _alt_e:
                logger.debug(f"AltData {instrument}: {_alt_e}")

        # ─── MOTEUR 13: Meta-Agent Consensus ─────────────────────────────────
        if hasattr(self, 'meta'):
            try:
                _meta_signals = {
                    "technical_score":  score,
                    "ml_score":         getattr(self, '_last_ml_score', 0.5),
                    "rl_action":        getattr(self, '_last_rl_action', 1),
                    "rl_confidence":    getattr(self, '_last_rl_conf', 0.5),
                    "sentiment_score":  self.alt_data.get_sentiment(instrument) if hasattr(self, 'alt_data') else 0.0,
                    "hmm_kelly_mult":   self.hmm.get_kelly_multiplier(instrument) if hasattr(self, 'hmm') else 1.0,
                    "hmm_regime":       self.hmm.get_current_regime() if hasattr(self, 'hmm') else "RANGE_MID_VOL",
                    "vpin_score":       self.vpin.get_all_scores().get(instrument, {}).get("vpin", 0.0) if hasattr(self, 'vpin') else 0.0,
                }
                _meta_dec = self.meta.decide(instrument, direction, _meta_signals)
                if not _meta_dec.approved:
                    logger.info(f"🧬 MetaAgent: {instrument} {direction} bloqué — {_meta_dec.reason}")
                    return
                # Appliquer le multiplicateur de taille (conviction du consensus)
                size1 = max(round(size1 * _meta_dec.size_multiplier, 2), 0.01)
                logger.debug(f"🧬 MetaAgent: score={_meta_dec.score:.3f} mult={_meta_dec.size_multiplier}x → size={size1}")
            except Exception as _meta_e:
                logger.debug(f"MetaAgent {instrument}: {_meta_e}")



        # ─── SINGLE ORDER PLACEMENT (Moteur 7 TWAP + Moteur 12 MEV obfusqué) ─
        ref = None

        # Enregistrer le prix courant AVANT l'ordre (frontrun baseline)
        if hasattr(self, 'mev'):
            try:
                self.mev.record_price(instrument, entry)
                _decoy = self.mev.inject_decoy_delay()
                if _decoy > 0:
                    import time as _t; _t.sleep(_decoy)
            except Exception:
                pass

        if hasattr(self, 'smart_router') and size1 >= 0.5:
            # Ordres ≥0.5 lot → TWAP obfusqué via MEV Shield
            try:
                # Obfuscation: schedule log-normal (MEV) au lieu de fixed 8s
                if hasattr(self, 'mev') and hasattr(self.mev, 'get_twap_schedule'):
                    _mev_schedule = self.mev.get_twap_schedule(size1, base_interval=8)
                    _n_slices = len(_mev_schedule)
                    _avg_interval = sum(iv for _, iv in _mev_schedule) / _n_slices
                else:
                    _n_slices, _avg_interval = 3, 8

                twap_result = self.smart_router.execute_twap(
                    epic=instrument, direction=direction,
                    total_size=size1, num_slices=_n_slices,
                    interval_s=_avg_interval,
                    sl_price=sl, tp_price=tp1, blocking=False,
                )
                ref = twap_result.get("order_id", f"twap_{instrument}")
            except Exception as _twap_e:
                logger.debug(f"TWAP MEV {instrument}: {_twap_e} — fallback direct")

        if ref is None:
            # ─── CHANTIER 1: Vérification marge pré-trade (OrderGuardian) ──────
            if hasattr(self, 'guardian'):
                try:
                    if not self.guardian.check_margin(instrument, size1):
                        logger.warning(f"⛔ {instrument} bloqué — marge insuffisante (guardian)")
                        return
                except Exception as _mg_e:
                    logger.debug(f"Guardian check_margin {instrument}: {_mg_e}")

            ref = self.broker.place_market_order(
                epic=instrument, direction=direction,
                size=size1, sl_price=sl, tp_price=tp1,
            )
            # ─── ORDER CONFIRMATION LOOP ─────────────────────────────────────
            # Après place_order(), vérifier que la position existe côté broker
            if ref:
                _confirmed = False
                for _attempt in range(5):  # 5 essais × 1s = 5s max
                    try:
                        time.sleep(1)
                        _open_pos = self.broker.get_open_positions()
                        _open_ids = {
                            p.get("position", {}).get("dealId", "")
                            for p in _open_pos
                        }
                        if ref in _open_ids:
                            _confirmed = True
                            logger.debug(f"✅ Confirmation ordre {instrument} #{ref} ({_attempt+1}s)")
                            break
                    except Exception:
                        pass
                if not _confirmed:
                    logger.warning(
                        f"⚠️ ORDRE NON CONFIRMÉ {instrument} ref={ref} après 5s "
                        f"— position peut-être non ouverte. Abandon."
                    )
                    ref = None  # Abort — ne pas enregistrer de trade fantôme
            # Frontrun detection post-placement
            if hasattr(self, 'mev') and ref:
                try:
                    _exec_px = self.broker.get_current_price(instrument)
                    if _exec_px:
                        _mid = _exec_px.get("mid", entry)
                        _fr = self.mev.detect_frontrun(instrument, direction, _mid)
                        if _fr:
                            logger.warning(f"🥷 FrontRun confirmé sur {instrument}")
                except Exception:
                    pass

        # Memory Pool: push signal pour analytics
        if hasattr(self, 'mem') and self.mem:
            try:
                self.mem.push_signal(instrument, direction, score)
            except Exception:
                pass


        if not ref:
            logger.warning(f"⛔ {instrument} — ordre rejeté (marché fermé ou erreur)")
            return

        # ─── MOTEUR 3: Shadow Trading — enregistrer le trade fantôme ─────────
        if hasattr(self, 'shadow'):
            try:
                self.shadow.on_signal(
                    instrument=instrument, direction=direction,
                    entry=entry, sl=sl, tp1=tp1, score=score,
                )
            except Exception as _sh_e:
                logger.debug(f"Shadow {instrument}: {_sh_e}")


        # ─── WebSocket monitoring ────────────────────────────────────────
        self.capital_ws.watch(
            instrument=instrument,
            entry=entry,
            tp1=tp1,
            tp2=tp1,       # same as tp1 for 1-TP mode
            tp1_ref=ref,
            ref2="",
            ref3="",
        )

        # ─── Session context ─────────────────────────────────────────────
        _is_overlap = self.context.is_overlap() if hasattr(self, 'context') else False
        _session_quality = self.context.session_quality() if hasattr(self, 'context') else ""

        # Correlation warning
        if hasattr(self, 'context'):
            for open_inst in self.positions:
                if self.positions[open_inst] is not None and open_inst != instrument:
                    corr = self.context.get_correlation(instrument, open_inst)
                    if abs(corr) > 0.7:
                        logger.warning(
                            f"⚠️ Corrélation {instrument}↔{open_inst}: {corr:.2f} — positions corrélées"
                        )

        # ─── Étape 1: Reality Slippage Injector (mode DEMO seulement) ────────
        # Dégrade le prix d'entrée enregistré pour refléter le slippage réel
        _recorded_entry = entry
        if hasattr(self, 'slippage'):
            try:
                _ob_imb = 0.5  # imbalance neutre par défaut
                _recorded_entry = self.slippage.apply_market_slippage(
                    entry=entry, direction=direction, ob_imbalance=_ob_imb
                )
            except Exception as _slip_e:
                logger.debug(f"Slippage {instrument}: {_slip_e}")

        self.positions[instrument] = {
            "refs":      [ref, None, None],
            "entry":     _recorded_entry,   # Prix dégradé en DEMO, réel en LIVE
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       _tp2_multi,   # V1 Ultimate: TP2 = 2.5R
            "tp3":       _tp2_multi,   # V1 Ultimate: TP3 = trailing (same level as tp2, trail activates after tp2)
            "tp1_level": _tp1_multi,   # V1 Ultimate: exact TP1 price for monitor
            "tp2_level": _tp2_multi,   # V1 Ultimate: exact TP2 price for monitor
            "direction": direction,
            "size":      size1,
            "size_tp1":  round(size1 * 0.40, 2),  # 40% closed at TP1
            "size_tp2":  round(size1 * 0.40, 2),  # 40% closed at TP2
            "size_tp3":  round(size1 * 0.20, 2),  # 20% trailing
            "tp1_hit":   False,
            "tp2_hit":   False,
            "be_active": False,   # V1 Ultimate: Break-Even flag
            "score":     score,
            "confirmations": confirmations,
            "regime":    regime_result.get("name", "RANGING"),
            "fear_greed": self.context._fg_value,
            "in_overlap": _is_overlap,
            "session_quality": _session_quality,
            "adx_at_entry": adx_now,
            "open_time":  datetime.now(timezone.utc),
            "ml_features": _ml_features,
            "market_regime": self.context.regime if hasattr(self, 'context') else "NEUTRAL",
        }


        name    = CAPITAL_NAMES.get(instrument, instrument)
        hour    = datetime.now(timezone.utc).hour
        minute  = datetime.now(timezone.utc).minute

        session = "London" if (hour < 13 or (hour == 13 and minute < 30)) else "NY"
        tracker = self._london_tracker if session == "London" else self._ny_tracker
        tracker.record_entry(name=name, sig=sig, entry=entry, size=size1)

        self.risk.on_trade_opened(instrument=instrument)
        # Incrémenter compteur journalier par instrument
        if hasattr(self, '_daily_inst_trades'):
            self._daily_inst_trades[instrument] = self._daily_inst_trades.get(instrument, 0) + 1
        # M38: Enregistrer le trade pour le trailing stop dynamique
        self.convexity.register_trade(instrument, entry, sl, direction)

        try:
            self.db.save_position_async(instrument, self.positions[instrument])
        except Exception as exc:
            logger.warning(f"⚠️ DB save_position open: {exc}")

        _broker_name = "MT5 IC Markets" if hasattr(self.broker, '_ok') else "Capital.com"
        logger.info(f"✅ {_broker_name} {sig} {instrument} @ {entry:.5f} | SL={sl:.5f} TP={tp1:.5f}")

        # ─── Telegram QUANT Signals PRO — Station X ──────────────────────────
        if self.telegram and self.telegram.router:
            _tp1_sig = self.positions[instrument].get("tp1_level", tp1)
            _tp2_sig = self.positions[instrument].get("tp2_level", _tp2_multi)
            # Appel synchrone pour récupérer le message_id (reply chain TP)
            def _post_signal():
                msg_id = self.telegram.router.send_signal(
                    instrument=instrument, direction=direction,
                    entry=entry, sl=sl, tp1=_tp1_sig, tp2=_tp2_sig,
                    score=score, confirmations=list(confirmations),
                )
                # Stocker pour les replies TP1/TP2/TP3
                if msg_id and self.positions.get(instrument):
                    self.positions[instrument]["tg_signal_msg_id"] = msg_id
            threading.Thread(target=_post_signal, daemon=True).start()

        # ── Signal Card visuel — DISABLED pour affichage épuré ──
        # Graphiques supprimés du canal Trades (texte uniquement)

        if self.positions[instrument]:
            self.positions[instrument]["ab_variant"] = self.ab.get_variant(instrument)

        try:
            dash_open(symbol=CAPITAL_NAMES.get(instrument, instrument),
                      side=direction, entry=entry, qty=size1)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════════════════════
    #  S-3: MICRO-TIMEFRAME SIGNAL PROCESSING (5m / 15m)
    # ═══════════════════════════════════════════════════════════════════════

    def _process_micro_signal(self, epic: str, micro_key: str, df, profile: dict, balance: float):
        """
        Process a micro-TF signal using the adapted profile parameters.
        Uses the same signal generation pipeline but with micro-TF profiles.
        """
        # Skip if main TF already has an open trade on this instrument
        if self.positions.get(epic) is not None:
            return

        _strat = profile.get("strat", "MR")

        # Generate signal with micro-TF profile
        sig, score, confirmations = self.strategy.get_signal(
            df, epic, asset_profile=profile
        )

        if sig == "HOLD" or score < 0.40:
            return

        # Wave 17: Micro-TF overlap bonus — more permissive during peak liquidity
        _micro_overlap = False
        try:
            if self.context.is_overlap():
                _micro_overlap = True
                # Lower threshold during overlap — higher liquidity = better fills
                if sig == "HOLD" and score >= 0.35:
                    sig = "BUY" if score > 0 else sig  # already filtered above
        except Exception:
            pass

        logger.info(f"⚡ MICRO-TF {micro_key} [{_strat}] {sig} score={score:.2f}{' 🔥OVL' if _micro_overlap else ''}")

        # Risk and position checks
        balance_for_risk = self.broker.get_balance() or balance
        _cat = profile.get("cat", "forex")
        if not self.risk.can_open_trade(balance_for_risk, instrument=epic, category=_cat):
            return
        if not self.risk.check_currency_exposure(epic, sig, self.positions):
            return
        if self.protection.is_blocked(epic):
            return

        # SL / TP with micro-TF parameters
        _sl_buf = profile.get("sl_buffer", 0.8)
        _tp_r   = profile.get("tp1", 1.0)   # Single TP mode
        entry   = float(df.iloc[-1]["close"])
        atr_val = self.strategy.get_atr(df)

        if atr_val <= 0:
            return

        # Always ATR-based SL/TP for micro-TF
        sl_dist = atr_val * _sl_buf
        if sig == "BUY":
            sl  = entry - sl_dist
            tp1 = entry + sl_dist * _tp_r
        else:
            sl  = entry + sl_dist
            tp1 = entry - sl_dist * _tp_r

        # R:R guard
        tp1_dist = abs(tp1 - entry)
        sl_dist_real = abs(sl - entry)
        if sl_dist_real > 0 and tp1_dist / sl_dist_real < 1.0:
            return

        # S-5: Spread filter
        try:
            px = self.broker.get_current_price(epic)
            if px:
                _spread = abs(float(px.get("ask", 0)) - float(px.get("bid", 0)))
                if _spread > 0 and tp1_dist > 0 and (_spread / tp1_dist) > 0.25:
                    logger.info(f"⛔ S-5 Micro-TF {micro_key}: spread {_spread:.5f} > 25% TP1 {tp1_dist:.5f}")
                    return
        except Exception:
            pass

        # Reduced sizing for micro-TF (half of normal)
        dynamic_risk = self.risk.compute_risk_pct(
            instrument=epic, score=score,
            current_atr=atr_val,
            avg_atr=float(df["atr"].tail(20).mean()) if "atr" in df.columns else atr_val
        )
        total_size = self.broker.position_size(
            balance=balance, risk_pct=dynamic_risk * 0.5, entry=entry, sl=sl, epic=epic
        )
        min_sz = self.broker.MIN_SIZE.get(epic.upper(), 1.0)
        size1 = max(min_sz, round(total_size / 3, 2))

        if size1 <= 0:
            return

        # Round SL/TP
        dec = PRICE_DECIMALS.get(epic, 5)
        sl  = round(sl,  dec)
        tp1 = round(tp1, dec)

        direction = "BUY" if sig == "BUY" else "SELL"
        logger.info(
            f"🔥 MICRO-TF ORDER {micro_key} | {direction} {epic} | "
            f"Entry={entry:.5f} SL={sl:.5f} TP1={tp1:.5f} | Size={size1}"
        )

        refs = self.broker.place_market_order(
            epic=epic, direction=direction, size=size1,
            sl_price=sl, tp_price=tp1,
        )
        if refs:
            self.positions[epic] = {
                "direction": direction, "entry": entry, "sl": sl,
                "tp1": tp1, "tp2": tp1, "tp3": tp1,   # single TP
                "refs": [refs, None, None],  # FIX: liste requise par bot_monitor (refs[0])
                "size": size1,
                "open_time": datetime.now(timezone.utc),
                "micro_tf": profile.get("tf"),
            }
            self.risk.on_trade_opened(instrument=epic)
            logger.info(f"✅ MICRO-TF {micro_key} ouvert | refs={refs}")

