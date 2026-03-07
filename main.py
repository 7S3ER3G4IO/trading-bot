"""
main.py — Boucle principale du bot de trading automatique.

Flux :
  1. Initialisation (connexion Binance + Telegram)
  2. Boucle toutes les N secondes :
     a. Récupérer les données OHLCV
     b. Calculer les indicateurs
     c. Générer un signal
     d. Vérifier le risque
     e. Exécuter l'ordre si signal valide
     f. Loguer et alerter Telegram
"""

import time
import signal
import sys
from datetime import datetime, timezone
from loguru import logger

from logger import setup_logger
from config import SYMBOL, TIMEFRAME, LOOP_INTERVAL_SECONDS
from data_fetcher import DataFetcher
from strategy import Strategy, SIGNAL_BUY, SIGNAL_SELL, SIGNAL_HOLD
from risk_manager import RiskManager
from order_executor import OrderExecutor
from telegram_notifier import TelegramNotifier


# ─── État global du bot ───────────────────────────────────────────────────────
bot_running = True

def shutdown_handler(sig, frame):
    """Capture CTRL+C pour arrêt propre."""
    global bot_running
    logger.warning("🛑 Arrêt demandé — fermeture propre du bot...")
    bot_running = False

signal.signal(signal.SIGINT,  shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# ─── Classe principale ────────────────────────────────────────────────────────

class TradingBot:
    def __init__(self):
        setup_logger()
        logger.info("=" * 60)
        logger.info("  🚀  BOT DE TRADING — Binance Testnet")
        logger.info(f"  📊  {SYMBOL} | {TIMEFRAME}")
        logger.info("=" * 60)

        self.fetcher    = DataFetcher()
        self.strategy   = Strategy()
        self.executor   = OrderExecutor()
        self.telegram   = TelegramNotifier()

        # Récupérer le solde initial
        balance_info        = self.fetcher.get_balance()
        initial_balance     = balance_info["free"]
        self.risk_manager   = RiskManager(initial_balance)

        # État du trade courant
        self.current_side   = None   # "BUY" ou "SELL" — trade en cours
        self.entry_price    = None
        self.stop_loss      = None
        self.take_profit    = None
        self.trade_amount   = None
        self.last_reset_day = datetime.now(timezone.utc).date()

        # Alerte de démarrage
        self.telegram.notify_start(initial_balance)

    def run(self):
        """Boucle principale — tourne indéfiniment jusqu'à arrêt."""
        logger.info(f"⏱️  Boucle toutes les {LOOP_INTERVAL_SECONDS}s | Appuyez CTRL+C pour arrêter\n")

        while bot_running:
            try:
                self._loop_tick()
            except Exception as e:
                logger.error(f"❌ Erreur dans la boucle principale : {e}")
                self.telegram.notify_error(str(e))

            # Attendre avant la prochaine itération
            time.sleep(LOOP_INTERVAL_SECONDS)

        logger.info("✅ Bot arrêté proprement.")

    def _loop_tick(self):
        """Un cycle complet du bot."""
        now = datetime.now(timezone.utc)

        # ── Remise à zéro journalière (à minuit UTC) ──────────────────────
        if now.date() != self.last_reset_day:
            balance_info = self.fetcher.get_balance()
            self.risk_manager.reset_daily(balance_info["free"])
            self.last_reset_day = now.date()

        # ── Récupérer les données ──────────────────────────────────────────
        df = self.fetcher.get_ohlcv()
        df = self.strategy.compute_indicators(df)

        # ── Prix et solde actuels ──────────────────────────────────────────
        ticker  = self.fetcher.get_ticker()
        price   = ticker["last"]
        balance = self.fetcher.get_balance()["free"]

        logger.info(
            f"[{now.strftime('%H:%M:%S')}] "
            f"Prix={price:.2f} USDT | "
            f"Solde={balance:.2f} USDT | "
            f"Trades ouverts={self.risk_manager.open_trades_count}"
        )

        # ── Vérifier si un trade ouvert doit être clôturé (SL/TP manuel) ──
        if self.current_side:
            self._check_exit(price, balance)
            return   # On ne cherche pas de nouveau signal si trade en cours

        # ── Générer un signal ──────────────────────────────────────────────
        signal_val = self.strategy.get_signal(df)

        if signal_val == SIGNAL_HOLD:
            return

        # ── Vérifier le risque ────────────────────────────────────────────
        if not self.risk_manager.can_open_trade(balance):
            return

        # ── Calculer SL / TP / taille ────────────────────────────────────
        atr = self.strategy.get_atr(df)
        sl, tp  = self.risk_manager.calculate_sl_tp(price, atr, signal_val)
        amount  = self.risk_manager.position_size(balance, price, sl)

        if amount <= 0:
            return

        # ── Exécuter l'ordre ──────────────────────────────────────────────
        order = None
        if signal_val == SIGNAL_BUY:
            order = self.executor.buy_market(SYMBOL, amount)
        elif signal_val == SIGNAL_SELL:
            # Sur Binance Spot Testnet on ne peut vendre que si on détient BTC
            btc_held = self.executor.get_position("BTC")
            sell_amount = min(amount, btc_held)
            if sell_amount > 0.00001:
                order = self.executor.sell_market(SYMBOL, sell_amount)
                amount = sell_amount
            else:
                logger.warning("⚠️  Signal SELL mais pas de BTC à vendre — ignoré.")
                return

        if not order:
            return

        # ── Mettre à jour l'état interne ─────────────────────────────────
        self.current_side  = signal_val
        self.entry_price   = price
        self.stop_loss     = sl
        self.take_profit   = tp
        self.trade_amount  = amount
        self.risk_manager.on_trade_opened()

        # ── Alerte Telegram ──────────────────────────────────────────────
        self.telegram.notify_order(
            side=signal_val,
            symbol=SYMBOL,
            amount=amount,
            entry=price,
            sl=sl,
            tp=tp,
            balance=balance,
        )

    def _check_exit(self, price: float, balance: float):
        """
        Simule le Stop-Loss et le Take-Profit sur le spot testnet.
        (Binance spot testnet ne supporte pas les ordres SL/TP natifs)
        """
        if not self.current_side:
            return

        hit_sl = hit_tp = False

        if self.current_side == SIGNAL_BUY:
            hit_sl = price <= self.stop_loss
            hit_tp = price >= self.take_profit
        else:  # SELL
            hit_sl = price >= self.stop_loss
            hit_tp = price <= self.take_profit

        reason = None
        if hit_tp:
            reason = "TAKE-PROFIT ✅"
        elif hit_sl:
            reason = "STOP-LOSS 🛑"

        if not reason:
            return

        # Clôturer le trade
        logger.info(f"💡 Clôture trade — {reason} | Prix : {price:.2f}")
        pnl = (price - self.entry_price) * self.trade_amount
        if self.current_side == SIGNAL_SELL:
            pnl = -pnl

        close_msg = (
            f"🔒 *Trade clôturé — {reason}*\n"
            f"📌 Paire : `{SYMBOL}`\n"
            f"💵 Clôture : `{price:.2f} USDT`\n"
            f"📊 PnL estimé : `{pnl:+.2f} USDT`\n"
            f"💰 Solde : `{balance:.2f} USDT`"
        )
        self.telegram.notify(close_msg)

        # Remettre à zéro l'état
        if self.current_side == SIGNAL_BUY:
            self.executor.sell_market(SYMBOL, self.trade_amount)
        else:
            self.executor.buy_market(SYMBOL, self.trade_amount)

        self.current_side  = None
        self.entry_price   = None
        self.stop_loss     = None
        self.take_profit   = None
        self.trade_amount  = None
        self.risk_manager.on_trade_closed()

        # Vérifier drawdown après clôture
        drawdown = (balance - self.risk_manager.daily_start_balance) / self.risk_manager.daily_start_balance
        if drawdown <= -0.05:
            self.telegram.notify_drawdown_alert(balance, drawdown)


# ─── Point d'entrée ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = TradingBot()
    bot.run()
