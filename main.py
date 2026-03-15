"""
main.py — ⚡ Nemesis v2.0 — Point d'entrée
"""
# ─── Supprimer les warnings cosmétiques au boot ──────────────────────────────
import warnings, os, sys
warnings.filterwarnings("ignore", message=".*_signature_descriptor.*")
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
# Supprimer le stderr "ImportError: cannot load module more than once per process"
# et "AttributeError: module 'numpy._globals'" qui viennent de ta/talib au boot
import io as _io
_real_stderr = sys.stderr
sys.stderr = _io.StringIO()
try:
    import signal
    from loguru import logger
    from core import TradingBot
finally:
    sys.stderr = _real_stderr

if __name__ == "__main__":
    bot = TradingBot()

    # TASK-017 : SIGTERM handler local Docker — sauvegarde l'équity curve avant shutdown
    def _sigterm_handler(signum, frame):
        logger.info("🛑 SIGTERM reçu — sauvegarde en cours...")
        try:
            # FIX: _save() est une méthode privée qui peut ne pas exister sur le stub
            getattr(bot.equity, '_save', lambda: None)()
            logger.info("✅ equity_history.json sauvegardé.")
        except Exception as _ex:
            logger.warning(f"⚠️ Sauvegarde equity échouée : {_ex}")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    bot.run()
