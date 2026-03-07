"""
telegram_notifier.py — Notifications ultra-complètes ⚡ AlphaTrader.
Chaque notification contient : PnL, %, tous les niveaux, chart.
"""
import os
import io
import asyncio
from typing import Optional
from telegram import Bot
from telegram.error import TelegramError
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

BOT_NAME = "⚡ AlphaTrader"


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
        """Exécute une coroutine Telegram de façon sûre."""
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

    def notify(self, text: str):
        if not self.bot:
            return
        async def _send():
            await self.bot.send_message(
                chat_id=self.chat_id, text=text, parse_mode="Markdown"
            )
        self._run(_send())

    def send_photo(self, image_bytes: bytes, caption: str):
        """Envoie un PNG dans Telegram avec une légende."""
        if not self.bot or not image_bytes:
            return
        async def _send():
            buf = io.BytesIO(image_bytes)
            buf.name = "chart.png"
            await self.bot.send_photo(
                chat_id=self.chat_id,
                photo=buf,
                caption=caption,
                parse_mode="Markdown"
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
            f"🎯 3 TP + Break Even\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡️ Risk : 1%/trade | -5%/j → pause\n"
            f"🟢 *Surveillance active 24/7*"
        )

    def notify_trade_open(
        self, side, symbol, entry, tp1, tp2, tp3, sl,
        amount, balance, score, confirmations: list
    ):
        """Alerte d'entrée avec tous les niveaux, confirmations et PnL potentiel."""
        emoji  = "🟢" if side == "BUY" else "🔴"
        action = "J'ACHÈTE" if side == "BUY" else "JE VENDS"
        pair   = symbol.replace("/USDT", "")
        sl_pts = abs(entry - sl)

        # PnL potentiel par TP
        def pnl(target):
            raw = abs(target - entry) * amount
            pct = abs(target - entry) / entry * 100
            sign = "+" if target > entry == (side=="BUY") else "+"
            return f"{sign}{raw:.2f} USDT ({pct:.2f}%)"

        # Confirmations détaillées
        conf_lines = "\n".join(f"  ✅ {c}" for c in confirmations)

        self.notify(
            f"*{BOT_NAME}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} *{action} {pair}*\n"
            f"💵 Entrée : `{entry:,.2f}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 TP1 : `{tp1:,.2f}`  _→ +{sl_pts:.0f} pts_\n"
            f"🎯 TP2 : `{tp2:,.2f}`  _→ +{sl_pts*2:.0f} pts_\n"
            f"🎯 TP3 : `{tp3:,.2f}`  _→ +{sl_pts*3:.0f} pts_\n"
            f"🔒 SL  : `{sl:,.2f}`   _→ -{sl_pts:.0f} pts_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 PnL TP1 : `+{sl_pts * amount * 1/3:.2f} USDT`\n"
            f"💰 PnL TP2 : `+{sl_pts*2 * amount * 1/3:.2f} USDT`\n"
            f"💰 PnL TP3 : `+{sl_pts*3 * amount * 1/3:.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🧠 Confirmations `{score}/6` :\n"
            f"{conf_lines}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Qté : `{amount:.5f} {pair}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`"
        )

    def notify_tp_hit(
        self, tp_num: int, symbol: str, price: float, entry: float,
        pnl: float, balance: float, remaining_qty: float, be_activated: bool = False
    ):
        pair = symbol.replace("/USDT", "")
        pct  = abs(price - entry) / entry * 100
        msg  = (
            f"*{BOT_NAME}* — 🎯 TP{tp_num}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *TP{tp_num} TOUCHÉ !* — `{pair}`\n"
            f"💵 Prix : `{price:,.2f}` _(+{pct:.2f}%)_\n"
            f"💰 PnL partiel : `{pnl:+.2f} USDT`\n"
            f"📦 Reste en position : `{remaining_qty:.5f} {pair}`\n"
            f"💰 Capital : `{balance:,.2f} USDT`"
        )
        if be_activated:
            msg += (
                f"\n━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔒 *SL → Break Even*\n"
                f"✅ TP2 & TP3 sont maintenant *SANS RISQUE !*"
            )
        self.notify(msg)

    def notify_sl_hit(
        self, symbol: str, price: float, entry: float,
        is_be: bool, pnl: float, balance: float
    ):
        pair = symbol.replace("/USDT", "")
        pct  = (pnl / balance * 100) if balance > 0 else 0
        if is_be:
            self.notify(
                f"*{BOT_NAME}* — 🛡️ Break Even\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛡️ *SL BE touché* — `{pair}`\n"
                f"💵 Clôture : `{price:,.2f}`\n"
                f"✅ *Aucune perte !* (Break Even)\n"
                f"💰 PnL : `{pnl:+.2f} USDT` _({pct:+.2f}%)_\n"
                f"💰 Capital : `{balance:,.2f} USDT`"
            )
        else:
            self.notify(
                f"*{BOT_NAME}* — 🛑 Stop-Loss\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🛑 *Stop-Loss touché* — `{pair}`\n"
                f"💵 Entrée : `{entry:,.2f}` → Clôture : `{price:,.2f}`\n"
                f"📉 PnL : `{pnl:+.2f} USDT` _({pct:+.2f}%)_\n"
                f"💰 Capital : `{balance:,.2f} USDT`"
            )

    def notify_trade_closed(
        self, symbol: str, reason: str, total_pnl: float,
        balance: float, initial_balance: float,
        entry: float, exit_price: float, trades_today: str
    ):
        pair     = symbol.replace("/USDT", "")
        pct_pnl  = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0
        move_pct = (exit_price - entry) / entry * 100
        emoji    = "✅" if total_pnl >= 0 else "❌"
        self.notify(
            f"*{BOT_NAME}* — {emoji} Trade Terminé\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"*{pair}* — {reason}\n"
            f"💵 Entrée : `{entry:,.2f}` → `{exit_price:,.2f}` _({move_pct:+.2f}%)_\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 PnL trade : `{total_pnl:+.2f} USDT` _({pct_pnl:+.2f}%)_\n"
            f"💰 Capital : `{balance:,.2f} USDT`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Jour : *{trades_today}*"
        )

    def notify_daily_report(self, report: str):
        self.notify(report)

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
