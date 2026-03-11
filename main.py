"""
main.py — ⚡ Nemesis v2.0 — Point d'entrée
"""
import signal
from loguru import logger
from core import TradingBot

if __name__ == "__main__":
    bot = TradingBot()

    # TASK-017 : SIGTERM handler Railway — sauvegarde l'équity curve avant shutdown
    def _sigterm_handler(signum, frame):
        logger.info("🛑 SIGTERM reçu — sauvegarde en cours...")
        try:
            bot.equity._save()
            logger.info("✅ equity_history.json sauvegardé.")
        except Exception as _ex:
            logger.warning(f"⚠️ Sauvegarde equity échouée : {_ex}")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    bot.run()
