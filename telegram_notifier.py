"""
telegram_notifier.py — AlphaTrader v2.5
Style : Neural Signal — ultra-premium, futuriste, lisible par tous.
"""
import os
import io
import asyncio
from datetime import datetime, timezone
from typing import Optional
from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv
from config import WALLET_CHANNEL_URL

load_dotenv()

SEP  = "━" * 39
LINE = "─" * 39
VER  = "AT-v2.5"


class TelegramNotifier:

    def __init__(self):
        token        = os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID")
        self.bot     = None

        if not token or not self.chat_id:
            logger.warning("⚠️  Telegram désactivé — credentials manquants.")
            return
        try:
            self.bot = Bot(token=token)
            logger.info("📱 Telegram notifier initialisé.")
        except Exception as e:
            logger.error(f"❌ Telegram init : {e}")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _run(self, coro):
        if not self.bot:
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f"❌ Telegram send : {e}")
        finally:
            loop.close()

    def _wallet_button(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Wallet en direct", url=WALLET_CHANNEL_URL)
        ]])

    def _send(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        if not self.bot:
            return
        async def _go():
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
            )
        self._run(_go())

    def send_message(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        """Alias public pour envoyer un message brut."""
        self._send(text, markup)

    def _send_photo(self, image_bytes: bytes, caption: str,
                    markup: Optional[InlineKeyboardMarkup] = None):
        if not self.bot or not image_bytes:
            return
        async def _go():
            buf = io.BytesIO(image_bytes)
            buf.name = "chart.png"
            await self.bot.send_photo(
                chat_id=self.chat_id, photo=buf,
                caption=caption, parse_mode="HTML",
                reply_markup=markup,
            )
        self._run(_go())

    def _ticker(self, symbol: str) -> str:
        return symbol.replace("/USDT", "").replace(":USDT", "")

    def _utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")

    def _bar(self, value: int, maximum: int, width: int = 10) -> str:
        filled = round((value / maximum) * width) if maximum > 0 else 0
        return "█" * filled + "░" * (width - filled)

    def _session(self) -> str:
        h = datetime.now(timezone.utc).hour
        if 7 <= h < 11:  return "London 🇬🇧"
        if 13 <= h < 17: return "NY 🗽"
        return "Off-session"

    # ─── Messages ─────────────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list):
        pairs = "  ·  ".join([s.replace("/USDT", "") for s in symbols])
        self._send(
            f"⚡ <b>SYSTÈME ACTIF</b> {LINE}\n"
            f"🤖  <b>AlphaTrader {VER}</b>  ·  Online\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Marchés      {pairs}\n"
            f"  Timeframe    5 min\n"
            f"  Sessions     London 08h  ·  NY 14h\n"
            f"  Capital      {balance:,.2f} USDT\n"
            f"  Risk/trade   1%  ·  DD limit  -3%/j\n"
            f"\n"
            f"  Statut       {'█' * 12}  OPÉRATIONNEL\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · Surveillance 24/7 active"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        ticker    = self._ticker(symbol)
        direction = "▲ LONG" if side == "BUY" else "▼ SHORT"
        arrow     = "▲" if side == "BUY" else "▼"
        session   = self._session()

        # Calculs % TP et SL
        def pct(target):
            return abs((target - entry) / entry * 100)

        bar = self._bar(min(score, 8), 8)

        self._send(
            f"⚡ <b>SIGNAL DÉTECTÉ</b> {LINE}\n"
            f"<b>{direction}  ·  {ticker}/USDT  ·  Session {session}</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  ENTRÉE       {entry:,.4f} USDT\n"
            f"  ├─ 🎯 TP +1  {tp1:,.4f}  (+{pct(tp1):.1f}%)\n"
            f"  ├─ 🎯 TP +2  {tp2:,.4f}  (+{pct(tp2):.1f}%)\n"
            f"  └─ 🎯 TP +3  ∞  [Trailing actif]\n"
            f"\n"
            f"  └─ 🛑 SL     {sl:,.4f}  (-{pct(sl):.1f}%)\n"
            f"\n"
            f"  Confiance   {bar}  {score}/8\n"
            f"  Capital     1% ╱ {balance:,.2f} USDT\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}",
            markup=markup or self._wallet_button(),
        )

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        pct     = pips / entry * 100

        be_line = "  SL déplacé ──►  Break Even 🛡️\n" if be_activated else ""
        next_line = (
            f"  TP{tp_num + 1} toujours ouvert →  prochain objectif\n"
            if tp_num < 3 else
            "  🤖 Trailing Stop activé sur TP3\n"
        )

        self._send(
            f"✅ <b>OBJECTIF {tp_num} ATTEINT</b> {LINE}\n"
            f"<b>{ticker}/USDT</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Prix atteint    {price:,.4f} USDT\n"
            f"  Gain partiel   {pnl_net:+.2f} USDT  (+{pct:.1f}%)\n"
            f"  Position        {tp_num * 25}% clôturée\n"
            f"\n"
            f"{be_line}"
            f"{next_line}"
            f"\n"
            f"  Balance         {balance:,.2f} USDT\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}",
            markup=markup or self._wallet_button(),
        )

    def notify_tp3_closed(self, symbol: str, price: float, entry: float,
                          pnl_gross: float, fees: float, balance: float):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        pct     = pips / entry * 100
        self._send(
            f"🏆 <b>TRADE FERMÉ — VICTOIRE</b> {LINE}\n"
            f"<b>{ticker}/USDT  ·  3/3 TP atteints</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Sortie      {entry:,.4f}  →  {price:,.4f}\n"
            f"  Performance ▲ +{pips:.4f}  (+{pct:.1f}%)\n"
            f"\n"
            f"  PnL brut   {pnl_gross:+.2f} USDT\n"
            f"  Frais       {-fees:.2f} USDT\n"
            f"  PnL net    {pnl_net:+.2f} USDT  ✅\n"
            f"\n"
            f"  Balance     {balance:,.2f} USDT\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}",
            markup=self._wallet_button(),
        )

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl_gross: float, fees: float, balance: float
    ):
        ticker  = self._ticker(symbol)
        pnl_net = pnl_gross - fees
        pips    = abs(price - entry)
        pct     = pips / entry * 100

        if is_be:
            self._send(
                f"🛡️ <b>BREAK EVEN ACTIVÉ</b> {LINE}\n"
                f"<b>{ticker}/USDT</b>\n"
                f"<code>{SEP}\n"
                f"\n"
                f"  Sortie au prix d'entrée\n"
                f"  Résultat     ±0.00 USDT\n"
                f"\n"
                f"  Capital protégé — aucune perte 💎\n"
                f"\n"
                f"  Balance     {balance:,.2f} USDT  (inchangée)\n"
                f"{SEP}</code>\n"
                f"🤖 {VER} · {self._utc()}"
            )
        else:
            self._send(
                f"🛑 <b>STOP LOSS TOUCHÉ</b> {LINE}\n"
                f"<b>{ticker}/USDT</b>\n"
                f"<code>{SEP}\n"
                f"\n"
                f"  Entrée      {entry:,.4f}  →  {price:,.4f} SL\n"
                f"  Performance ▼ -{pips:.4f}  (-{pct:.1f}%)\n"
                f"\n"
                f"  PnL net    {pnl_net:+.2f} USDT  ❌\n"
                f"\n"
                f"  Balance     {balance:,.2f} USDT\n"
                f"  Prochaine opportunité en analyse...\n"
                f"{SEP}</code>\n"
                f"🤖 {VER} · {self._utc()}"
            )

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        ticker  = self._ticker(symbol)
        net     = total_pnl_gross - total_fees
        pips    = abs(exit_price - entry)
        pct     = pips / entry * 100
        emoji   = "✅" if net >= 0 else "❌"
        delta   = balance - initial_balance
        delta_p = delta / initial_balance * 100 if initial_balance > 0 else 0

        self._send(
            f"{emoji} <b>TRADE CLÔTURÉ — {ticker}</b> {LINE}\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  {entry:,.4f}  →  {exit_price:,.4f}\n"
            f"  Performance  {'+' if net >= 0 else ''}{pct:.1f}%\n"
            f"\n"
            f"  PnL net    {net:+.2f} USDT\n"
            f"  Raison     {reason}\n"
            f"\n"
            f"  Balance     {balance:,.2f} USDT\n"
            f"  ΔJour       {delta:+.2f}$  ({delta_p:+.2f}%)\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}"
        )

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        ticker = self._ticker(symbol)
        logger.info(f"🔄 Trailing Stop {ticker} : {old_sl:,.4f} → {new_sl:,.4f}")

    def notify_daily_report(self, report_lines: list, date_str: str):
        total     = len(report_lines)
        wins      = sum(1 for *_, r, __ in report_lines if r.startswith("+")) if report_lines and len(report_lines[0]) == 5 else 0
        total_net = sum(pnl for *_, pnl in report_lines) if report_lines and len(report_lines[0]) == 5 else 0
        wr        = wins / total * 100 if total > 0 else 0
        bar       = self._bar(wins, max(total, 1))

        lines = ""
        for row in report_lines:
            if len(row) == 5:
                date, side, ticker, result, pnl = row
                e = "✅" if result.startswith("+") else ("🛡️" if result == "BE" else "❌")
                lines += f"  {e} {ticker:<6}  {result:<8}  {pnl:+.2f}$\n"

        self._send(
            f"📊 <b>BILAN DU JOUR</b> {LINE}\n"
            f"<b>AlphaTrader  ·  {date_str}</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"{lines}"
            f"\n"
            f"  {LINE}\n"
            f"  Trades      {total}      Gagnants   {wins}/{total}\n"
            f"  Win Rate    {wr:.0f}%    {bar}\n"
            f"  PnL net    {total_net:+.2f} USDT\n"
            f"\n"
            f"  Balance     en cours...\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · Rapport {self._utc()}",
            markup=self._wallet_button(),
        )

    def notify_weekly_report(self, report: str):
        self._send(report)

    def notify_morning_brief(self, brief: str):
        self._send(brief)

    def notify_news_pause(self, event_name: str, minutes: float):
        self._send(
            f"⏸️ <b>TRADING SUSPENDU</b> {LINE}\n"
            f"<b>Événement économique imminent</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Événement   {event_name}\n"
            f"  Dans        {abs(minutes):.0f} minutes\n"
            f"  Impact      🔴 ÉLEVÉ\n"
            f"\n"
            f"  Le bot reprend automatiquement\n"
            f"  après la publication des données.\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        self._send(
            f"⛔ <b>PROTECTION ACTIVÉE</b> {LINE}\n"
            f"<b>Limite de perte journalière atteinte</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Perte du jour   {pct:.1%}  ATTEINT\n"
            f"  Seuil           -3.0%\n"
            f"\n"
            f"  Trading suspendu jusqu'à minuit UTC\n"
            f"  Capital protégé — reprise demain 🛌\n"
            f"\n"
            f"  Balance     {balance:,.2f} USDT\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · Protection active"
        )

    def notify_error(self, error: str):
        self._send(
            f"⚠️ <b>ERREUR SYSTÈME</b> {LINE}\n"
            f"<code>{SEP}\n"
            f"  {error[:200]}\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}"
        )

    def notify_pre_signal(self, side: str, symbol: str, price: float, score: int):
        ticker    = self._ticker(symbol)
        direction = "▲ LONG" if side == "BUY" else "▼ SHORT"
        bar       = self._bar(score, 8)
        self._send(
            f"⏳ <b>SETUP EN FORMATION</b> {LINE}\n"
            f"<b>{direction}  ·  {ticker}/USDT</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Prix cible  ≈ {price:,.4f} USDT\n"
            f"  Score       {bar}  {score}/8\n"
            f"  Manque      1-2 confirmations\n"
            f"\n"
            f"  Entrée probable dans les prochaines\n"
            f"  bougies — surveillance active...\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}"
        )

    def notify_setup_cancelled(self, symbol: str):
        ticker = self._ticker(symbol)
        self._send(
            f"❌ <b>SETUP ANNULÉ</b> {LINE}\n"
            f"<code>{SEP}\n"
            f"  {ticker}  —  Confirmation non obtenue\n"
            f"  Le signal s'est invalidé.\n"
            f"{SEP}</code>\n"
            f"🤖 {VER} · {self._utc()}"
        )

    def post_wallet_stats(
        self, balance: float, initial_balance: float,
        open_trades: list, daily_pnl: float, total_pnl: float,
        win_rate: float, nb_trades: int
    ):
        try:
            from config import WALLET_CHAT_ID
        except ImportError:
            return

        pct_day = (daily_pnl / initial_balance * 100) if initial_balance > 0 else 0
        pct_all = ((balance - initial_balance) / initial_balance * 100) if initial_balance > 0 else 0
        trend   = "📈" if daily_pnl >= 0 else "📉"
        bar_wr  = self._bar(int(win_rate), 100)

        trades_block = ""
        for t in open_trades:
            e = "🟢" if t["pnl"] >= 0 else "🔴"
            trades_block += f"  {e} {t['symbol'].replace('/USDT',''):<6}  {t['pnl']:+.2f} USDT\n"
        if not trades_block:
            trades_block = "  Aucun trade ouvert\n"

        msg = (
            f"⚡ <b>WALLET LIVE</b> {LINE}\n"
            f"<b>AlphaTrader {VER}</b>\n"
            f"<code>{SEP}\n"
            f"\n"
            f"  Capital       {balance:,.2f} USDT\n"
            f"  PnL du jour   {daily_pnl:+.2f} USDT  ({pct_day:+.2f}%)\n"
            f"  PnL total     {total_pnl:+.2f} USDT  ({pct_all:+.2f}%)\n"
            f"\n"
            f"  Positions ouvertes :\n"
            f"{trades_block}"
            f"\n"
            f"  Win Rate      {bar_wr}  {win_rate:.0f}%\n"
            f"  Trades        {nb_trades} total\n"
            f"{SEP}</code>\n"
            f"{trend} Mise à jour auto toutes les 30min"
        )

        if not self.bot:
            return
        async def _go():
            await self.bot.send_message(
                chat_id=WALLET_CHAT_ID,
                text=msg,
                parse_mode="HTML",
            )
        self._run(_go())

    # ─── Legacy aliases ────────────────────────────────────────────────────────

    def notify(self, text: str, markup=None):
        self._send(text, markup)

    def send_photo(self, image_bytes: bytes, caption: str, markup=None):
        self._send_photo(image_bytes, caption, markup)
