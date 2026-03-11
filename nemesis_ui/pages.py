"""
pages.py — Nemesis Hub Pages
Builds content + inline keyboards for each navigable page.
"""
from .renderer import NemesisRenderer as R
try:
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None


def _btn(text, data):
    """Safe button constructor."""
    if InlineKeyboardButton is None:
        return None
    return InlineKeyboardButton(text, callback_data=data)


def _back_row():
    """Back to hub button row."""
    btn = _btn("🔙 Menu", "nav:hub")
    return [btn] if btn else []


def _markup(rows):
    """Build InlineKeyboardMarkup from rows, filtering None buttons."""
    if InlineKeyboardMarkup is None:
        return None
    clean = [[b for b in row if b is not None] for row in rows if row]
    clean = [row for row in clean if row]
    return InlineKeyboardMarkup(clean) if clean else None


class PageBuilder:
    """Builds text + markup for each Hub page."""

    # ── Hub Main ──────────────────────────────────────────────────────────────
    @staticmethod
    def build_hub(balance: float = 0.0, pnl_today: float = 0.0) -> tuple:
        """Returns (text, InlineKeyboardMarkup) for the main Hub."""
        header = R.box_header("⚡ NEMESIS COMMAND CENTER")
        status = "🟢 ONLINE" if balance > 0 else "⚪ STANDBY"

        text = (
            f"{header}\n"
            f"  {status}  ·  {R.utc_time()}\n\n"
            f"💰 {balance:,.2f}€  ·  📈 {R.format_pnl(pnl_today)} aujourd'hui"
        )

        rows = [
            [_btn("📊 Dashboard", "page:dashboard"), _btn("📋 Trades", "page:trades")],
            [_btn("📈 Performance", "page:performance"), _btn("☀️ Briefing", "page:briefing")],
            [_btn("🛡️ Risk", "page:risk"), _btn("🧠 Régime", "page:regime")],
            [_btn("🏆 Stats", "page:stats"), _btn("⚙️ Settings", "page:settings")],
        ]

        return text, _markup(rows)

    # ── Dashboard Page ────────────────────────────────────────────────────────
    @staticmethod
    def build_dashboard(
        balance: float, pnl_today: float, pnl_total: float,
        pnl_week: float = 0.0,
        positions: list = None, session: str = "",
        equity_data: list = None, nb_instruments: int = 8,
    ) -> tuple:
        """
        positions: [{name, direction, pnl, entry, sl, tp2, current_price}, ...]
        """
        header = R.box_header("📊 DASHBOARD LIVE")

        pnl_pct = (pnl_total / max(balance - pnl_total, 1)) * 100 if balance > 0 else 0

        pos_block = ""
        if positions:
            pos_block = f"\n🔄 POSITIONS ({len(positions)})\n"
            for p in positions:
                icon = "🟢" if p.get("pnl", 0) >= 0 else "🔴"
                bar = R.progress_bar(
                    p.get("current_price", p.get("entry", 0)),
                    p.get("sl", 0),
                    p.get("tp2", 0),
                    width=8,
                )
                pos_block += f" {icon} {p.get('name', '?'):<8} {R.format_pnl(p.get('pnl', 0))}  {bar}\n"
        else:
            pos_block = "\n📊 Aucune position ouverte\n"

        equity_spark = ""
        if equity_data and len(equity_data) >= 2:
            equity_spark = f"\n📈 {R.sparkline(equity_data, width=12)}  {R.format_pnl(pnl_week)} cette semaine"

        session_str = R.session_name() if not session else session

        text = (
            f"{header}\n\n"
            f"💰 {balance:,.2f}€  ·  📈 {R.format_pnl(pnl_today)} ({pnl_pct:+.1f}%)\n"
            f"{pos_block}"
            f"{equity_spark}\n\n"
            f"⏰ Session : {session_str}\n"
            f"📡 {nb_instruments} instruments en surveillance"
        )

        rows = [
            [_btn("🔄 Refresh", "page:dashboard")],
            _back_row(),
        ]

        return text, _markup(rows)

    # ── Trades Page ───────────────────────────────────────────────────────────
    @staticmethod
    def build_trades(positions: list = None, nb_instruments: int = 8) -> tuple:
        """
        positions: [{name, epic, direction, entry, sl, tp1, tp2, tp1_hit, current_price, pnl}, ...]
        """
        header = R.box_header("📋 POSITIONS ACTIVES")

        if not positions:
            text = f"{header}\n\n📊 Aucune position ouverte.\n\n🔍 {nb_instruments} instruments en surveillance"
            return text, _markup([_back_row()])

        lines = []
        btn_rows = []
        for p in positions:
            name = p.get("name", "?")
            epic = p.get("epic", "?")
            direction = p.get("direction", "?")
            entry = p.get("entry", 0)
            tp1_icon = "✅" if p.get("tp1_hit") else "○"
            pnl = p.get("pnl", 0)
            icon = "🟢" if pnl >= 0 else "🔴"

            price_line = ""
            if p.get("current_price"):
                bar = R.progress_bar(p["current_price"], p.get("sl", 0), p.get("tp2", 0), width=8)
                price_line = f"\n  📍 <code>{p['current_price']:,.5f}</code>  {bar}"

            lines.append(
                f"{icon} <b>{name}</b> {direction} TP1{tp1_icon}\n"
                f"  📍 Entrée: <code>{entry:,.5f}</code>{price_line}\n"
                f"  💰 PnL: {R.format_pnl(pnl)}"
            )

            btn_rows.append([
                _btn(f"❌ Fermer {name}", f"close:{epic}"),
                _btn(f"🟡 BE {name}", f"be:{epic}"),
            ])

        text = f"{header}\n\n" + "\n\n".join(lines)

        all_rows = btn_rows + [_back_row()]
        return text, _markup(all_rows)

    # ── Performance Page ──────────────────────────────────────────────────────
    @staticmethod
    def build_performance(
        wr: float, total_trades: int, wins: int,
        pnl_today: float, pnl_week: float, pnl_month: float,
        pnl_total: float, win_streak: int,
        best_win_streak: int = 0,
        max_dd: float = 0.0, best_day: float = 0.0,
        equity_data: list = None,
    ) -> tuple:
        header = R.box_header("📈 PERFORMANCE")

        equity_spark = ""
        if equity_data and len(equity_data) >= 2:
            equity_spark = R.sparkline(equity_data, width=12)

        streak = R.format_streak(win_streak)
        streak_line = f"│ Streak  {streak}\n" if streak else ""

        text = (
            f"{header}\n\n"
            f"📊 AUJOURD'HUI\n"
            f" Trades: {total_trades}  ·  WR: {wr:.0f}%\n"
            f" PnL: {R.format_pnl(pnl_today)}\n\n"
            f"╭── STATS ───────────────────╮\n"
            f"│ WR      {wr:.0f}% {R.wr_bar(wr)}\n"
            f"│ Total   {R.format_pnl(pnl_total)}\n"
            f"│ Semaine {R.format_pnl(pnl_week)}\n"
            f"│ Mois    {R.format_pnl(pnl_month)}\n"
            f"{streak_line}"
            f"│ Record  {best_win_streak} wins\n"
            f"│ Max DD  {max_dd:.1f}%\n"
            f"│ Best    {R.format_pnl(best_day)}\n"
            f"╰────────────────────────────╯"
            + (f"\n\n📈 Equity: {equity_spark}" if equity_spark else "")
        )

        return text, _markup([_back_row()])

    # ── Risk Page ─────────────────────────────────────────────────────────────
    @staticmethod
    def build_risk(
        balance: float, open_count: int, max_trades: int,
        dd_daily: float, dd_daily_limit: float,
        dd_monthly: float, paused: bool,
    ) -> tuple:
        header = R.box_header("🛡️ RISK SUMMARY")

        status = "⏸️ PAUSED" if paused else "🟢 ACTIF"
        dd_bar = R.wr_bar(min(abs(dd_daily), dd_daily_limit), dd_daily_limit, width=10)

        text = (
            f"{header}\n\n"
            f"╭── EXPOSITION ──────────────╮\n"
            f"│ Statut    : {status}\n"
            f"│ Balance   : {balance:,.2f}€\n"
            f"│ Positions : {open_count}/{max_trades}\n"
            f"╰────────────────────────────╯\n\n"
            f"╭── DRAWDOWN ─────────────────╮\n"
            f"│ Jour    : {dd_daily:+.2f}%\n"
            f"│ Limite  : {dd_bar} {dd_daily_limit:.0f}%\n"
            f"│ Mensuel : {dd_monthly:+.2f}%\n"
            f"╰────────────────────────────╯"
        )

        return text, _markup([_back_row()])

    # ── Regime Page ───────────────────────────────────────────────────────────
    @staticmethod
    def build_regime(regimes: list) -> tuple:
        """
        regimes: [{instrument, regime_name, confidence}, ...]
        """
        header = R.box_header("🧠 RÉGIMES HMM")

        if not regimes:
            text = f"{header}\n\nAucune position ouverte."
            return text, _markup([_back_row()])

        EMOJI = {"RANGING": "⬛", "TREND_UP": "🟢", "TREND_DOWN": "🔴"}
        lines = []
        for r in regimes:
            emoji = EMOJI.get(r.get("regime_name", ""), "⚪")
            lines.append(
                f" {emoji} <b>{r['instrument']}</b> — "
                f"{r.get('regime_name', '?')} ({r.get('confidence', 0):.0%})"
            )

        text = f"{header}\n\n" + "\n".join(lines)
        return text, _markup([_back_row()])

    # ── Stats / Gamification Page ─────────────────────────────────────────────
    @staticmethod
    def build_stats(
        stats_block: str, achievements_block: str,
        win_streak: int = 0,
    ) -> tuple:
        header = R.box_header("🏆 STATS & ACHIEVEMENTS")
        streak = R.format_streak(win_streak)

        text = (
            f"{header}\n\n"
            + (f"{streak}\n\n" if streak else "")
            + f"{stats_block}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{achievements_block}"
        )

        return text, _markup([_back_row()])

    # ── Settings Page ─────────────────────────────────────────────────────────
    @staticmethod
    def build_settings(paused: bool) -> tuple:
        header = R.box_header("⚙️ PARAMÈTRES")

        status = "⏸️ EN PAUSE" if paused else "🟢 ACTIF"

        text = (
            f"{header}\n\n"
            f"🤖 État du bot : <b>{status}</b>\n\n"
            f"Sélectionnez une action :"
        )

        if paused:
            rows = [
                [_btn("▶️ Reprendre le trading", "action:resume")],
                _back_row(),
            ]
        else:
            rows = [
                [_btn("⏸️ Mettre en pause", "action:pause")],
                _back_row(),
            ]

        return text, _markup(rows)

    # ── Briefing Page (compact) ───────────────────────────────────────────────
    @staticmethod
    def build_briefing_page() -> tuple:
        header = R.box_header("☀️ MORNING BRIEFING")

        text = (
            f"{header}\n\n"
            f"☕ <b>Génération du briefing en cours...</b>\n\n"
            f"<i>Analyse de chaque instrument avec graphiques.\n"
            f"Envoi dans quelques secondes.</i>"
        )

        return text, _markup([_back_row()])
