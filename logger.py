"""
logger.py — Configuration du système de logs avec loguru.
"""
import os
import sys
from loguru import logger
from config import LOG_DIR, LOG_LEVEL


def setup_logger() -> None:
    """Configure loguru : console + fichier rotatif."""
    os.makedirs(LOG_DIR, exist_ok=True)

    # Supprimer le handler par défaut
    logger.remove()

    # ── Console (couleurs) ──────────────────────────────────────────────────
    logger.add(
        sys.stdout,
        level=LOG_LEVEL,
        colorize=True,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan> — <level>{message}</level>"
        ),
    )

    # ── Fichier rotatif (10 MB max, 7 jours de rétention) ──────────────────
    logger.add(
        f"{LOG_DIR}/bot.log",
        level=LOG_LEVEL,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} — {message}",
    )

    logger.info("📋 Logger initialisé.")
