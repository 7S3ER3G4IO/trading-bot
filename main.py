"""
main.py — Boucle principale avec système 3 TP + Break Even.

Logique de sortie :
  TP1 touché → ferme 1/3, SL déplacé au Break Even (BE = prix d'entrée)
  TP2 touché → ferme 1/3, SL reste au BE
  TP3 touché → ferme tout, trade terminé
  SL touché  → ferme tout (perte limitée OU BE = sans perte si TP1 atteint)
"""

import time
import signal
import sys
from typing import Optional
from datetime import datetime, timezone
from loguru import logger

from logger import setup_logger
from config import SYMBOL, TIMEFRAME, LOOP_INTERVAL_SECONDS
from data_fetcher import DataFetcher
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from order_executor import OrderExecutor
from telegram_notifier import TelegramNotifier

# ─── Arrêt propre ─────────────────────────────────────────────────────────────
bot_running = True

def shutdown_handler(sig, frame):
    global bot_running
    logger.warning("🛑 Arrêt demandé — fermeture propre...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ─── État d'un trade ouvert ───────────────────────────────────────────────────
class TradeState:
    """Suivi complet d'un trade en cours avec ses 3 cibles."""
    def __init__(
        self,
        side:        str,
        entry:       float,
        total_amount:float,
        sl:          float,
        tp1:         float,
        tp2:         float,
        tp3:         float,
        be:          float,
    ):
        self.side          = side
        self.entry         = entry
        self.initial_sl    = sl
        self.current_sl    = sl     # Peut changer → BE après TP1
        self.tp1           = tp1
        self.tp2           = tp2
        self.tp3           = tp3
        self.be            = be     # Break Even = prix d'entrée

        self.total_amount  = total_amount
        self.remaining     = total_amount   # Diminue à chaque TP partiel
        self.tp1_hit       = False
        self.tp2_hit       = False
        self.be_active     = False  # True une fois TP1 touché

        self.total_pnl     = 0.0   # PnL cumulé des clôtures partielles

    def is_open(self) -> bool:
        return self.remaining > 0.000001

    def fraction_size(self, frac: float) -> float:
        """Retourne la quantité à fermer pour cette fraction."""
        return round(self.total_amount * frac, 5)


# ─── Bot principal ────────────────────────────────────────────────────────────
class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  🚀  BOT DE TRADING — 3 TP + Break Even")
        logger.info(f"  📊  {SYMBOL} | {TIMEFRAME}")
        logger.info("=" * 60)

        self.fetcher    = DataFetcher()
        self.strategy   = Strategy()
        self.executor   = OrderExecutor()
        self.telegram   = TelegramNotifier()

        balance_info        = self.fetcher.get_balance()
        self.risk_manager   = RiskManager(balance_info["free"])
        self.trade: Optional[TradeState] = None
        self.last_reset_day = datetime.now(timezone.utc).date()

        self.telegram.notify_start(balance_info["free"])

    def run(self):
        logger.info(f"⏱️  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | CTRL+C pour arrêter\n")
        while bot_running:
            try:
                self._tick()
            except Exception as e:
                logger.error(f"❌ Erreur boucle : {e}")
                self.telegram.notify_error(str(e))
            time.sleep(LOOP_INTERVAL_SECONDS)
        logger.info("✅ Bot arrêté proprement.")

    # ─── Tick principal ───────────────────────────────────────────────────────
    def _tick(self):
        now = datetime.now(timezone.utc)

        # Reset journalier à minuit UTC
        if now.date() != self.last_reset_day:
            bal = self.fetcher.get_balance()["free"]
            self.risk_manager.reset_daily(bal)
            self.last_reset_day = now.date()

        ticker  = self.fetcher.get_ticker()
        price   = ticker["last"]
        balance = self.fetcher.get_balance()["free"]

        logger.info(
            f"[{now.strftime('%H:%M:%S')}] "
            f"Prix={price:.2f} | Solde={balance:.2f} USDT | "
            f"Trade={'OUI' if self.trade else 'NON'}"
        )

        # ── Si trade en cours : surveiller SL / TPs ──────────────────────────
        if self.trade:
            self._monitor_trade(price, balance)
            return

        # ── Chercher un nouveau signal ────────────────────────────────────────
        df = self.fetcher.get_ohlcv()
        df = self.strategy.compute_indicators(df)
        signal_val = self.strategy.get_signal(df)

        if signal_val == SIGNAL_HOLD:
            return

        if not self.risk_manager.can_open_trade(balance):
            return

        # ── Calculer les niveaux ──────────────────────────────────────────────
        atr    = self.strategy.get_atr(df)
        levels = self.risk_manager.calculate_levels(price, atr, signal_val)
        amount = self.risk_manager.position_size(balance, price, levels["sl"])

        if amount <= 0:
            return

        # ── Exécuter l'ordre ──────────────────────────────────────────────────
        order = self._open_order(signal_val, amount)
        if not order:
            return

        # ── Créer l'état du trade ─────────────────────────────────────────────
        self.trade = TradeState(
            side=signal_val,
            entry=price,
            total_amount=amount,
            sl=levels["sl"],
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            be=levels["be"],
        )
        self.risk_manager.on_trade_opened()

        # ── Alerte Telegram (style Station X) ────────────────────────────────
        self.telegram.notify_trade_open(
            side=signal_val,
            symbol=SYMBOL,
            entry=price,
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            sl=levels["sl"],
            amount=amount,
            balance=balance,
        )

    # ─── Surveillance du trade ───────────────────────────────────────────────
    def _monitor_trade(self, price: float, balance: float):
        t = self.trade

        if t.side == SIGNAL_BUY:
            going_up   = lambda target: price >= target
            going_down = lambda target: price <= target
        else:
            going_up   = lambda target: price <= target
            going_down = lambda target: price >= target

        # ── Vérifier TP1 ─────────────────────────────────────────────────────
        if not t.tp1_hit and going_up(t.tp1):
            close_qty = t.fraction_size(1/3)
            self._close_partial(t.side, close_qty)
            pnl = (t.tp1 - t.entry) * close_qty * (1 if t.side == SIGNAL_BUY else -1)
            t.total_pnl += pnl
            t.remaining -= close_qty
            t.tp1_hit    = True

            # → Déplacer SL au Break Even
            t.current_sl = t.be
            t.be_active  = True

            self.telegram.notify_tp_hit(1, price, pnl, be_activated=True)
            logger.info(f"🎯 TP1 touché ! SL → BE ({t.be:.2f}) | Reste : {t.remaining:.5f} BTC")

        # ── Vérifier TP2 ─────────────────────────────────────────────────────
        elif t.tp1_hit and not t.tp2_hit and going_up(t.tp2):
            close_qty = t.fraction_size(1/3)
            self._close_partial(t.side, close_qty)
            pnl = (t.tp2 - t.entry) * close_qty * (1 if t.side == SIGNAL_BUY else -1)
            t.total_pnl += pnl
            t.remaining -= close_qty
            t.tp2_hit    = True

            self.telegram.notify_tp_hit(2, price, pnl)
            logger.info(f"🎯 TP2 touché ! SL reste au BE. Reste : {t.remaining:.5f} BTC")

        # ── Vérifier TP3 ─────────────────────────────────────────────────────
        elif t.tp1_hit and t.tp2_hit and going_up(t.tp3):
            pnl = (t.tp3 - t.entry) * t.remaining * (1 if t.side == SIGNAL_BUY else -1)
            self._close_partial(t.side, t.remaining)
            t.total_pnl += pnl
            t.remaining  = 0

            self.telegram.notify_tp_hit(3, price, pnl)
            self.telegram.notify_trade_closed("TP3 ✅", t.total_pnl, balance)
            self._close_trade()
            return

        # ── Vérifier SL (ou BE si TP1 touché) ────────────────────────────────
        if going_down(t.current_sl):
            pnl_remaining = (t.current_sl - t.entry) * t.remaining * (1 if t.side == SIGNAL_BUY else -1)
            total          = t.total_pnl + pnl_remaining
            self._close_partial(t.side, t.remaining)

            is_be = t.be_active
            self.telegram.notify_sl_hit(price, t.entry, is_be, total)
            self.telegram.notify_trade_closed("BE 🛡️" if is_be else "SL 🛑", total, balance)
            self._close_trade()

    # ─── Helpers ─────────────────────────────────────────────────────────────
    def _open_order(self, side: str, amount: float):
        if side == SIGNAL_BUY:
            return self.executor.buy_market(SYMBOL, amount)
        else:
            btc = self.executor.get_position("BTC")
            qty = min(amount, btc)
            if qty > 0.00001:
                return self.executor.sell_market(SYMBOL, qty)
            logger.warning("⚠️  Signal SELL mais pas de BTC — ignoré.")
            return None

    def _close_partial(self, side: str, qty: float):
        """Ferme une fraction de la position."""
        qty = round(qty, 5)
        if qty <= 0:
            return
        if side == SIGNAL_BUY:
            self.executor.sell_market(SYMBOL, qty)
        else:
            self.executor.buy_market(SYMBOL, qty)
        logger.info(f"💱 Clôture partielle : {qty:.5f} BTC")

    def _close_trade(self):
        """Remet à zéro l'état du trade."""
        self.trade = None
        self.risk_manager.on_trade_closed()
        logger.info("🔄 Trade terminé — prêt pour le prochain signal")


# ─── Point d'entrée ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
