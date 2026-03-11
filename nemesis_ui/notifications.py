"""
notifications.py — Nemesis Premium Push Notifications
Format premium pour les messages critiques envoyés en push.
"""
from .renderer import NemesisRenderer as R


class NotificationFormatter:
    """Formats premium push messages (trade open, TP, SL, reports)."""

    # ── Trade Open ────────────────────────────────────────────────────────────
    @staticmethod
    def format_trade_open(
        name: str, sig: str, entry: float, sl: float,
        tp1: float, tp2: float, tp3: float,
        score: int, confirmations: list,
        session: str, win_streak: int = 0,
        sparkline_data: list = None,
    ) -> str:
        direction = "🟢 LONG" if sig in ("BUY", "LONG") else "🔴 SHORT"
        score_bar = R.score_bar(score, 3)
        rr = R.format_rr(entry, sl, tp2)

        def pct(target):
            return abs((target - entry) / entry * 100)

        header = R.box_header(f"⚡ NEMESIS — SIGNAL ACTIF")
        spark = ""
        if sparkline_data:
            spark = f"\n▸ Tendance : {R.sparkline(sparkline_data)}"

        streak_str = ""
        if win_streak > 0:
            streak_str = f"  ·  {R.format_streak(win_streak)}"

        return (
            f"{header}\n\n"
            f"{direction}  ·  <b>{name}</b>  ·  {session}\n"
            f"{score_bar}  Score {score}/3  ·  {rr}\n\n"
            f"📍 <b>Niveaux</b>\n"
            f"  📍 Entrée  <code>{entry:,.5f}</code>\n"
            f"  🎯 TP1  <code>{tp1:,.5f}</code>  (+{pct(tp1):.2f}%)\n"
            f"  🎯 TP2  <code>{tp2:,.5f}</code>  (+{pct(tp2):.2f}%)\n"
            f"  🎯 TP3  Trailing\n"
            f"  🛑 SL   <code>{sl:,.5f}</code>  (-{pct(sl):.2f}%)\n\n"
            f"🔬 {' · '.join(confirmations[:4]) if confirmations else '—'}"
            f"{streak_str}"
            f"{spark}"
        )

    # ── TP Hit ────────────────────────────────────────────────────────────────
    @staticmethod
    def format_tp_hit(
        tp_num: int, name: str, entry: float, price: float,
        pnl_net: float, balance: float,
        be_activated: bool = False,
        win_streak: int = 0, wr: float = 0.0,
        sl: float = 0.0, tp2: float = 0.0,
    ) -> str:
        pct = abs(price - entry) / entry * 100 if entry > 0 else 0

        header = R.box_header(f"🎯 TP{tp_num} TOUCHÉ — {name}")

        bar = ""
        if sl > 0 and tp2 > 0 and tp_num < 3:
            bar = f"\n{R.progress_bar(price, sl, tp2, width=10)}\n"

        be_line = ""
        if be_activated:
            be_line = "↑ BE activé — risque 0\n"
        elif tp_num < 3:
            be_line = f"⏳ TP{tp_num + 1} en cours\n"

        streak_str = ""
        if win_streak > 0:
            streak_str = f"  ·  {R.format_streak(win_streak)}"

        return (
            f"{header}\n\n"
            f"<code>{entry:,.5f}</code> ➜ <code>{price:,.5f}</code>  (+{pct:.2f}%)\n\n"
            f"💰 {R.format_pnl(pnl_net)}  ·  💼 {balance:,.2f}€\n"
            f"{bar}"
            f"{be_line}"
            f"WR {wr:.0f}%{streak_str}"
        )

    # ── SL Hit ────────────────────────────────────────────────────────────────
    @staticmethod
    def format_sl_hit(
        name: str, entry: float, price: float,
        pnl_net: float, balance: float,
        portfolio_impact_pct: float = 0.0,
        wr: float = 0.0, win_streak: int = 0,
    ) -> str:
        pct = abs(price - entry) / entry * 100 if entry > 0 else 0

        header = R.box_header(f"🛑 STOP LOSS — {name}")

        recovery = max(1, int(abs(pnl_net) / max(balance * 0.01 * 1.8, 1)))

        return (
            f"{header}\n\n"
            f"<code>{entry:,.5f}</code> ➜ <code>{price:,.5f}</code>  (-{pct:.2f}%)\n"
            f"❌ {R.format_pnl(pnl_net)}  ·  💼 {balance:,.2f}€\n\n"
            f"📊 Impact portfolio : {portfolio_impact_pct:.2f}%\n"
            f"🔄 Recovery : ~{recovery} trades à WR {wr:.0f}%\n\n"
            f"🧠 Surveillance active · Score ≥2 requis"
        )

    # ── Break-Even ────────────────────────────────────────────────────────────
    @staticmethod
    def format_be_hit(name: str, balance: float) -> str:
        header = R.box_header(f"🟡 BREAK-EVEN — {name}")
        return (
            f"{header}\n\n"
            f"Sortie au prix d'entrée\n"
            f"<b>Capital 100% protégé</b> 💎\n"
            f"PnL : ±0€  ·  💼 {balance:,.2f}€\n\n"
            f"🔍 Prochaine opportunité en analyse..."
        )

    # ── Trade Complete (3/3 TP) ───────────────────────────────────────────────
    @staticmethod
    def format_trade_complete(
        name: str, entry: float, price: float,
        pnl_net: float, balance: float,
    ) -> str:
        pct = abs(price - entry) / entry * 100 if entry > 0 else 0
        header = R.box_header(f"🏆 TRADE PARFAIT — {name}")
        return (
            f"{header}\n\n"
            f"<code>{entry:,.5f}</code> ➜ <code>{price:,.5f}</code>  (+{pct:.2f}%)\n\n"
            f"💰 {R.format_pnl(pnl_net)}  ·  💼 {balance:,.2f}€\n"
            f"🔥🔥🔥 3/3 TP touchés — trade parfait !"
        )

    # ── Error ─────────────────────────────────────────────────────────────────
    @staticmethod
    def format_error(error: str, balance: float = 0.0, count: int = 1) -> str:
        severity = "🟠" if count < 3 else "🔴"
        level = "AVERTISSEMENT" if count < 3 else "CRITIQUE"
        header = R.box_header(f"{severity} ERREUR #{count} — {level}")
        return (
            f"{header}\n\n"
            f"⏰ {R.utc_time()}  ·  💼 {balance:,.2f}€\n\n"
            f"<code>{error[:300]}</code>\n\n"
            f"🤖 Bot en cours de récupération..."
        )

    # ── Crash ─────────────────────────────────────────────────────────────────
    @staticmethod
    def format_crash(error: str, consecutive: int) -> str:
        header = R.box_header("🚨 ALERTE CRITIQUE — BOT INSTABLE")
        return (
            f"{header}\n\n"
            f"⏰ {R.utc_time()}\n"
            f"🔄 <b>{consecutive} erreurs consécutives</b>\n\n"
            f"<code>{error[:200]}</code>\n\n"
            f"⚠️ Action requise : vérifier Railway"
        )

    # ── Session Open ──────────────────────────────────────────────────────────
    @staticmethod
    def format_session_open(
        session: str, balance: float, pnl: float, pnl_pct: float,
        nb_instruments: int,
    ) -> str:
        icon = "🇬🇧" if session == "London" else "🇺🇸"
        return (
            f"{icon} <b>Session {session} ouverte</b>\n"
            f"💰 {balance:,.2f}€  ·  📊 {R.format_pnl(pnl)} ({pnl_pct:+.1f}%)\n"
            f"🤖 Scanning {nb_instruments} instruments"
        )

    # ── Morning Brief ─────────────────────────────────────────────────────────
    @staticmethod
    def format_morning_brief_header(date_label: str, session: str) -> str:
        header = R.box_header(f"☀️ BRIEFING · {date_label.upper()}")
        return (
            f"{header}\n"
            f"  {session}\n"
        )

    @staticmethod
    def format_morning_overview(analyses: list) -> str:
        """
        analyses: [{ticker, price, bias_txt, sparkline_data}, ...]
        """
        header = R.box_header(f"☀️ BRIEFING · {R.date_label().upper()}")
        lines = [f"{header}\n"]

        for a in analyses:
            ticker = a.get("ticker", "?")
            price = a.get("price", 0)
            bias = a.get("bias_txt", "neutre")
            spark = R.sparkline(a.get("sparkline_data", []), width=8)

            if "haussier" in bias.lower():
                icon = "🟢"
            elif "baissier" in bias.lower():
                icon = "🔴"
            else:
                icon = "⚪"

            if price >= 1000:
                price_str = f"{price:,.0f}"
            elif price >= 10:
                price_str = f"{price:,.2f}"
            else:
                price_str = f"{price:,.4f}"

            lines.append(f"{ticker:<8} {spark}  {price_str}  {icon}")

        # Global bias
        bull = sum(1 for a in analyses if "haussier" in a.get("bias_txt", "").lower())
        bear = sum(1 for a in analyses if "baissier" in a.get("bias_txt", "").lower())

        if bull >= len(analyses) * 0.6:
            global_bias = f"🟢 HAUSSIER · {bull}/{len(analyses)} bullish"
        elif bear >= len(analyses) * 0.6:
            global_bias = f"🔴 BAISSIER · {bear}/{len(analyses)} bearish"
        else:
            global_bias = f"⚪ MIXTE · {bull} 🟢 / {bear} 🔴"

        lines.append("")
        lines.append(f"🌍 <b>BIAIS GLOBAL</b>")
        lines.append(f"  {global_bias}")
        lines.append(f"\n🤖 {len(analyses)} instruments · Score ≥2 requis")

        return "\n".join(lines)

    # ── Daily Report ──────────────────────────────────────────────────────────
    @staticmethod
    def format_daily_report(
        trades: list, balance: float, pnl_total: float,
        wr: float, win_streak: int, best_trade: str = "",
        equity_data: list = None, weekly_pnl: float = 0.0,
        weekly_wr: float = 0.0, achievement: str = "",
    ) -> str:
        """
        trades: [{ticker, pnl, result, rr}, ...]
        """
        header = R.box_header(f"📊 DAILY REPORT · {R.date_label().upper()}")

        trade_lines = []
        for t in trades:
            icon = "✅" if t.get("pnl", 0) >= 0 else "❌"
            ticker = t.get("ticker", "?")
            pnl = t.get("pnl", 0)
            result = t.get("result", "—")
            rr_str = f"R:R {t['rr']:.1f}" if t.get("rr") else "—"
            trade_lines.append(f" {icon} {ticker:<8} {R.format_pnl(pnl)}  {result}  {rr_str}")

        trades_block = "\n".join(trade_lines) if trade_lines else " — Aucun trade"

        wins = sum(1 for t in trades if t.get("pnl", 0) >= 0)
        total = len(trades)

        equity_spark = ""
        if equity_data and len(equity_data) >= 2:
            equity_spark = f"\n📈 EQUITY\n{R.sparkline(equity_data, width=12)}"

        pnl_pct = (pnl_total / balance * 100) if balance > 0 else 0
        streak_str = R.format_streak(win_streak)

        ach_line = ""
        if achievement:
            ach_line = f"\n🏅 Achievement : {achievement}"

        streak_line = f"  Streak {streak_str}\n" if streak_str else ""
        return (
            f"{header}\n\n"
            f"🏆 TRADES DU JOUR\n"
            f"{trades_block}\n\n"
            f"📊 <b>Performance</b>\n"
            f"  WR     {wr:.0f}% {R.wr_bar(wr)} {wins}/{total}\n"
            f"  PnL    {R.format_pnl(pnl_total)}  ({pnl_pct:+.2f}%)\n"
            f"{streak_line}"
            f"{equity_spark}\n\n"
            f"📊 Semaine : {R.format_pnl(weekly_pnl)} · WR {weekly_wr:.0f}%"
            f"{ach_line}"
        )

    # ── Achievement Unlocked ──────────────────────────────────────────────────
    @staticmethod
    def format_achievement_unlocked(name: str, desc: str) -> str:
        return (
            f"🏅 <b>Achievement Débloqué !</b>\n\n"
            f"{name}\n"
            f"<i>{desc}</i>"
        )
