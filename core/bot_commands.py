"""
bot_commands.py — Commandes Telegram + status/trades
"""
from .imports import *


class BotCommandsMixin:

    def _force_close(self, instrument: str) -> str:
        """Ferme de force une position Capital.com via commande Telegram."""
        state = self.capital_trades.get(instrument)
        if state is None:
            return f"⚠️ Aucune position ouverte sur {instrument}"
        try:
            refs = state.get("refs", [])
            closed = 0
            for ref in refs:
                if ref:
                    ok = self.capital.close_position(ref)
                    if ok:
                        closed += 1
            name = CAPITAL_NAMES.get(instrument, instrument)
            self.capital_trades[instrument] = None
            self.capital_ws.unwatch(instrument)
            try:
                self.db.close_capital_trade(instrument)
            except Exception:
                pass
            try:
                dash_close(symbol=name, pnl=0.0, result="MANUAL", side=state.get("direction","?"))
            except Exception:
                pass
            return f"✅ {name} fermé manuellement ({closed} positions)"
        except Exception as e:
            return f"❌ Erreur fermeture {instrument} : {e}"

    def _force_be(self, instrument: str) -> str:
        """Active manuellement le Break-Even sur une position Capital.com."""
        state = self.capital_trades.get(instrument)
        if state is None:
            return f"⚠️ Aucune position ouverte sur {instrument}"
        entry = state.get("entry", 0)
        refs  = state.get("refs", [])
        ok_count = 0
        for ref in refs[1:]:   # TP2 + TP3
            if ref:
                try:
                    if self.capital.modify_position_stop(ref, entry):
                        ok_count += 1
                except Exception:
                    pass
        name = CAPITAL_NAMES.get(instrument, instrument)
        return f"✅ BE activé sur {name} ({ok_count} positions)" if ok_count else f"❌ BE échec {name}"

    def _do_pause(self) -> str:
        """Met le bot en pause manuelle."""
        self._manual_pause = True
        return "⏸️ Bot mis en pause."

    def _do_resume(self) -> str:
        """Reprend le trading après pause manuelle."""
        self._manual_pause = False
        return "▶️ Trading repris."

    def _do_brief(self) -> str:
        """Envoie la matinale à la demande."""
        try:
            import threading
            balance = self.capital.get_balance() if self.capital.available else 0.0
            _, reason = self.calendar.should_pause_trading()
            brief = self.context.build_morning_brief(balance, reason or None)
            self.telegram.notify_morning_brief(brief, nb_instruments=len(CAPITAL_INSTRUMENTS))
            return "☀️ Matinale envoyée."
        except Exception as e:
            return f"❌ Matinale : {e}"

    def _do_backtest(self, symbol: str = None, days: int = 30) -> str:
        """Backtest désactivé (backtester supprimé lors du nettoyage)."""
        return "⚠️ Backtest non disponible — backtester supprimé lors du nettoyage de code."

    # ── Sprint 3 : Commandes premium Telegram ─────────────────────────────────

    def _cmd_best_pair(self) -> str:
        """Retourne l'instrument le plus profitable sur la session courante."""
        pnl_by_inst: dict = {}
        for t in self._capital_closed_today:
            sym = t.get("symbol", "?")
            pnl_by_inst[sym] = pnl_by_inst.get(sym, 0) + t.get("pnl", 0)
        if not pnl_by_inst:
            return (
                "🏆 <b>Meilleur Instrument</b>\n"
                "<code>Aucun trade fermé aujourd'hui.</code>"
            )
        ranked = sorted(pnl_by_inst.items(), key=lambda x: x[1], reverse=True)
        lines = "\n".join(
            f"  {'🥇' if i==0 else '🥈' if i==1 else '🥉' if i==2 else '  '}"
            f" {sym}: <b>{pnl:+.2f}€</b>"
            for i, (sym, pnl) in enumerate(ranked)
        )
        winner = ranked[0]
        return (
            f"🏆 <b>Meilleur Instrument — {winner[0]}</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{lines}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>"
        )

    def _cmd_risk(self) -> str:
        """Résumé de l'exposition et du drawdown actuel."""
        balance = self.capital.get_balance() if self.capital.available else 0.0
        open_count = sum(1 for s in self.capital_trades.values() if s is not None)
        daily_dd = 0.0
        if self._daily_start_balance > 0 and balance > 0:
            daily_dd = (self._daily_start_balance - balance) / self._daily_start_balance * 100
        monthly_dd = 0.0
        if self._monthly_start_balance > 0 and balance > 0:
            monthly_dd = (self._monthly_start_balance - balance) / self._monthly_start_balance * 100
        paused_str = "⏸️ PAUSED" if self._dd_paused or self._manual_pause else "🟢 ACTIF"
        return (
            f"🛡️ <b>Risk Summary</b>\n"
            f"<code>━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  Statut       : {paused_str}\n"
            f"  Balance      : {balance:,.2f}€\n"
            f"  Positions    : {open_count}/{MAX_OPEN_TRADES}\n"
            f"  DD Journalier: {daily_dd:+.2f}% (limite {self.DAILY_DD_LIMIT:.0f}%)\n"
            f"  DD Mensuel   : {monthly_dd:+.2f}% (10%=48h | 15%=stop)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━</code>"
        )

    def _cmd_regime(self) -> str:
        """Retourne le régime HMM pour les instruments avec positions ouvertes."""
        REGIME_EMOJI = {0: "⬛ RANGING", 1: "🟢 TREND_UP", 2: "🔴 TREND_DOWN"}
        # Seulement les instruments avec position ouverte (pas les 39)
        active_instruments = [inst for inst, st in self.capital_trades.items() if st is not None]
        if not active_instruments:
            return "🧠 <b>Régimes HMM</b>\nAucune position ouverte."
        lines = []
        for inst in active_instruments:
            try:
                df = self.capital.fetch_ohlcv(inst, timeframe="5m", count=50)
                if df is None or len(df) < 20:
                    lines.append(f"  {inst}: <i>données insuffisantes</i>")
                    continue
                df = self.strategy.compute_indicators(df)
                res = self.hmm.detect_regime(df, symbol=inst)
                regime_name = REGIME_EMOJI.get(res["regime"], res["name"])
                conf = res["confidence"]
                lines.append(f"  {inst}: {regime_name} ({conf:.0%})")
            except Exception as e:
                lines.append(f"  {inst}: ⚠️ {str(e)[:30]}")
        body = "\n".join(lines)
        return (
            f"🧠 <b>Régimes HMM ({len(active_instruments)} positions)</b>\n\n"
            f"<code>{body}</code>"
        )

    # ──────────────────────────────────────────────────────────────────────────

    def _status_text(self) -> str:
        balance = self.capital.get_balance() if self.capital.available else 0.0
        pnl_total = round(balance - self.initial_balance, 2) if balance > 0 else 0.0
        pnl_pct   = (pnl_total / self.initial_balance * 100) if self.initial_balance > 0 else 0.0
        bal_str   = f"{balance:,.2f}€"

        paused = "⏸️ PAUSED" if (self._manual_pause or self.handler.is_paused()) else "🟢 ACTIF"

        cap_lines = ""
        cap_open  = 0
        total_unrealized = 0.0
        for epic, state in self.capital_trades.items():
            if state is None:
                continue
            cap_open += 1
            name  = CAPITAL_NAMES.get(epic, epic)
            entry = state.get("entry", 0.0)
            direction = state.get("direction", "?")
            tp1_icon  = "✅" if state.get("tp1_hit") else "○"
            unrealized = 0.0
            try:
                px = self.capital.get_current_price(epic)
                if px:
                    mid = px["mid"]
                    unrealized = round((mid - entry) * (1 if direction == "BUY" else -1) * 3, 2)
                    total_unrealized += unrealized
            except Exception:
                pass
            pnl_icon = "🟢" if unrealized >= 0 else "🔴"
            cap_lines += (
                f"  • <b>{name}</b> {direction} | éntrée: <code>{entry:.5f}</code> "
                f"| PnL: {pnl_icon} <b>{unrealized:+.2f}€</b> TP1{tp1_icon}\n"
            )

        equity_pct = self.equity.total_pnl_pct()
        max_dd     = self.equity.max_drawdown()
        cb_status  = "🔴 Sous MA20" if self.equity.is_below_ma() else "🟢 OK"

        ctx = self.context.get_context_line() if hasattr(self.context, 'get_context_line') else ""
        return (
            f"⚡ <b>NEMESIS — Statut</b>\n\n"
            f"💰 Balance : <b>{bal_str}</b>\n"
            f"  PnL total  : <b>{pnl_total:+.2f}€ ({pnl_pct:+.1f}%)</b>\n"
            f"  Non-réalisé : <b>{total_unrealized:+.2f}€</b>\n\n"
            f"📊 Positions ouvertes : <b>{cap_open}/{len(CAPITAL_INSTRUMENTS)}</b>\n"
            f"{cap_lines}"
            f"\n📈 Equity : PnL={equity_pct:+.1f}%  MaxDD={max_dd:.1f}%  CB={cb_status}\n"
            f"🤖 État : {paused}\n"
            f"{ctx}"
        )

    def _trades_text(self):
        """Retourne le texte + markup des positions Capital.com actives."""
        lines, markup_epic = [], None

        for epic, state in self.capital_trades.items():
            if state is None:
                continue
            name     = CAPITAL_NAMES.get(epic, epic)
            entry    = state.get("entry", 0)
            direction = state.get("direction", "?")
            tp1_icon  = "✅" if state.get("tp1_hit") else "○"

            price_data = self.capital.get_current_price(epic)
            if price_data:
                price   = price_data["mid"]
                pip     = CAPITAL_PIP.get(epic, 0.0001)
                pnl_pips = round((price - entry) / pip) if direction == "BUY" else round((entry - price) / pip)
                price_line = f"\n  📍 Prix : <code>{price:.5f}</code>  ({pnl_pips:+.0f} pips)"
            else:
                price_line = ""

            lines.append(
                f"<b>{name}</b> {direction} TP1{tp1_icon}\n"
                f"  📍 Entrée : <code>{entry:.5f}</code>{price_line}\n"
                f"  🛑 SL : <code>{state.get('sl', 0):.5f}</code>"
            )
            markup_epic = epic

        if not lines:
            return "📋 <b>Aucune position ouverte.</b>", None

        text   = "📋 <b>Positions actives :</b>\n\n" + "\n\n".join(lines)
        markup = TelegramBotHandler.trade_keyboard(markup_epic) if markup_epic else None
        return text, markup

    # ── Hub v3.0 — Page callbacks ─────────────────────────────────────────────

    def _hub_data(self) -> tuple:
        """Returns (balance, pnl_today) for Hub display."""
        balance = self.capital.get_balance() if self.capital.available else 0.0
        pnl_today = sum(t.get("pnl", 0) for t in self._capital_closed_today)
        return balance, pnl_today
    # ── Hub pages removed — multi-channel uses URL buttons now ──────────────

