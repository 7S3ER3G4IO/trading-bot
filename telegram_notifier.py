"""
telegram_notifier.py — Notifications ⚡ AlphaTrader avec boutons inline.
"""
import os
import io
import asyncio
from typing import Optional
from telegram import Bot, InlineKeyboardMarkup
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "⚡ AlphaTrader"
BINANCE_FEE_RATE = 0.001  # 0.1% par ordre


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

    def _run(self, coro):
        """Exécute une coroutine Telegram de façon sûre (thread-safe)."""
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

    def notify(self, text: str, markup: Optional[InlineKeyboardMarkup] = None):
        if not self.bot:
            return
        async def _send():
            await self.bot.send_message(
                chat_id=self.chat_id, text=text,
                parse_mode="Markdown",
                reply_markup=markup,
            )
        self._run(_send())

    def send_photo(self, image_bytes: bytes, caption: str,
                   markup: Optional[InlineKeyboardMarkup] = None):
        if not self.bot or not image_bytes:
            return
        async def _send():
            buf = io.BytesIO(image_bytes)
            buf.name = "chart.png"
            await self.bot.send_photo(
                chat_id=self.chat_id, photo=buf,
                caption=caption, parse_mode="Markdown",
                reply_markup=markup,
            )
        self._run(_send())

    # ─── Messages ─────────────────────────────────────────────────────────────

    def notify_start(self, balance: float, symbols: list):
        pairs = " • ".join([s.replace("/USDT", "") for s in symbols])
        self.notify(
            f"*{BOT_NAME}* — Démarrage 🟢\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Marchés : `{pairs}`\n"
            f"⏱  Timeframe : `15min`\n"
            f"🧠 Stratégie : `6 filtres EMA+RSI+MACD+ADX+VOL+HTF`\n"
            f"🎯 3 TP + Break Even + Trailing Stop\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ Risk : 1%/trade | -5%/j → pause\n"
            f"🟢 *Surveillance active 24/7*"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list,
        context_line: str = "",
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        emoji  = "🟢" if side == "BUY" else "🔴"
        action = "J'ACHÈTE" if side == "BUY" else "JE VENDS"
        pair   = symbol.replace("/USDT", "")
        sl_pts = abs(entry - sl)

        # Frais estimés (2 ordres)
        fees_est = round(entry * amount * BINANCE_FEE_RATE * 2, 2)

        # PnL potentiel net par TP (1/3 de la position)
        frac = amount / 3
        def pnl_net(target, fraction):
            gross = abs(target - entry) * fraction
            fees  = entry * fraction * BINANCE_FEE_RATE * 2
            return gross - fees

        conf_lines = "\n".join(f"  ✅ {c}" for c in confirmations)
        ctx_block  = f"\n{context_line}" if context_line else ""

        msg = (
            f"*{BOT_NAME}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} *{action} {pair}*\n"
            f"💵 Entrée : `{entry:,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 TP1 : `{tp1:,.2f}` → +{sl_pts:.0f} pts · `{pnl_net(tp1,frac):+.2f} USDT net`\n"
            f"🎯 TP2 : `{tp2:,.2f}` → +{sl_pts*2:.0f} pts · `{pnl_net(tp2,frac):+.2f} USDT net`\n"
            f"🎯 TP3 : `{tp3:,.2f}` → +{sl_pts*3:.0f} pts · `{pnl_net(tp3,frac):+.2f} USDT net`\n"
            f"🔒 SL  : `{sl:,.2f}` → -{sl_pts:.0f} pts\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💸 Frais estimés : `{fees_est:.2f} USDT`\n"
            f"🧠 Confirmations `{score}/6` :\n"
            f"{conf_lines}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
            f"{ctx_block}\n"
            f"📦 Qté : `{amount:.5f} {pair}` | 💰 Capital : `{balance:,.2f} USDT`"
        )
        self.notify(msg, markup=markup)

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl_gross: float, fees: float, balance: float,
        remaining_qty: float, be_activated: bool = False,
        markup: Optional[InlineKeyboardMarkup] = None,
    ):
        pair    = symbol.replace("/USDT", "")
        pct     = abs(price - entry) / entry * 100
        pnl_net = pnl_gross - fees
        msg = (
            f"*{BOT_NAME}* — 🎯 TP{tp_num}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP{tp_num} TOUCHÉ !* — `{pair}`\n"
            f"💵 Prix : `{price:,.2f}` _(+{pct:.2f}%)_\n"
            f"💰 PnL brut : `{pnl_gross:+.2f} USDT`\n"
            f"💸 Frais : `-{fees:.2f} USDT`\n"
            f"✅ *PnL net : `{pnl_net:+.2f} USDT`*\n"
            f"📦 Reste : `{remaining_qty:.5f} {pair}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`"
        )
        if be_activated:
            msg += (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔒 *SL → Break Even*\n"
                f"✅ TP2 & TP3 sont maintenant *SANS RISQUE !*"
            )
        self.notify(msg, markup=markup)

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl_gross: float, fees: float, balance: float
    ):
        pair    = symbol.replace("/USDT", "")
        pnl_net = pnl_gross - fees
        pct     = pnl_net / balance * 100 if balance > 0 else 0

        if is_be:
            self.notify(
                f"*{BOT_NAME}* — 🛡️ Break Even\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ *SL BE touché* — `{pair}`\n"
                f"💵 Clôture : `{price:,.2f}`\n"
                f"✅ *Aucune perte !* (Break Even)\n"
                f"💸 Frais : `-{fees:.2f} USDT`\n"
                f"💰 PnL net : `{pnl_net:+.2f} USDT` _({pct:+.2f}%)_\n"
                f"💰 Capital : `{balance:,.2f} USDT`"
            )
        else:
            self.notify(
                f"*{BOT_NAME}* — 🛑 Stop-Loss\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛑 *Stop-Loss touché* — `{pair}`\n"
                f"💵 Entrée : `{entry:,.2f}` → `{price:,.2f}`\n"
                f"📉 PnL brut : `{pnl_gross:+.2f} USDT`\n"
                f"💸 Frais : `-{fees:.2f} USDT`\n"
                f"❌ *PnL net : `{pnl_net:+.2f} USDT` ({pct:+.2f}%)*\n"
                f"💰 Capital : `{balance:,.2f} USDT`"
            )

    def notify_trade_closed(
        self, symbol: str, reason: str,
        total_pnl_gross: float, total_fees: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, daily_summary: str
    ):
        pair    = symbol.replace("/USDT", "")
        net     = total_pnl_gross - total_fees
        pct_bal = net / initial_balance * 100 if initial_balance > 0 else 0
        move    = (exit_price - entry) / entry * 100
        emoji   = "✅" if net >= 0 else "❌"
        self.notify(
            f"*{BOT_NAME}* — {emoji} Trade Terminé\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{pair}* — {reason}\n"
            f"💵 `{entry:,.2f}` → `{exit_price:,.2f}` _({move:+.2f}%)_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 PnL brut : `{total_pnl_gross:+.2f} USDT`\n"
            f"💸 Frais : `-{total_fees:.2f} USDT`\n"
            f"{emoji} *PnL net : `{net:+.2f} USDT` ({pct_bal:+.2f}%)*\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Jour : {daily_summary}"
        )

    def notify_trailing_stop_update(self, symbol: str, old_sl: float, new_sl: float):
        pair = symbol.replace("/USDT", "")
        self.notify(
            f"*{BOT_NAME}* — 🔄 Trailing Stop\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 *Trailing Stop — `{pair}`*\n"
            f"SL : `{old_sl:,.2f}` → `{new_sl:,.2f}`\n"
            f"📈 Le Stop suit le prix — gains sécurisés"
        )

    def notify_daily_report(self, report: str):
        self.notify(report)

    def notify_weekly_report(self, report: str):
        self.notify(report)

    def notify_morning_brief(self, brief: str):
        self.notify(brief)

    def notify_news_pause(self, event_name: str, minutes: float):
        self.notify(
            f"*{BOT_NAME}* — ⏸️ Pause News\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 *{event_name}*\n"
            f"⏱  Dans `{abs(minutes):.0f} min`\n"
            f"🔇 Trading suspendu ±30min"
        )

    def notify_drawdown_alert(self, balance: float, pct: float):
        self.notify(
            f"*{BOT_NAME}* — ⛔ DRAWDOWN\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 Perte journalière : `{pct:.1%}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"🔒 *Bot en PAUSE jusqu'à demain*"
        )

    def notify_error(self, error: str):
        self.notify(f"*{BOT_NAME}* — ⚠️ Erreur\n```{error[:180]}```")
