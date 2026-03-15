"""
export_trades.py — Export CSV des trades NEMESIS
Exporte la table `trades` (et `positions` ouvertes) en CSV.

Usage CLI :
    python export_trades.py                          # export 30 derniers jours
    python export_trades.py --days 90                # 90 derniers jours
    python export_trades.py --all                    # tous les trades
    python export_trades.py --output /tmp/trades.csv # fichier custom
    python export_trades.py --open                   # positions actuellement ouvertes
"""
import os
import csv
import argparse
from datetime import datetime, timezone, timedelta
from loguru import logger


def _get_db():
    from database import get_db
    return get_db()


def export_closed_trades(days: int = 30, output: str = "", export_all: bool = False) -> str:
    """Exporte les trades fermés. Retourne le chemin du fichier créé."""
    db = _get_db()
    output = output or f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        if export_all:
            rows = db._execute(
                "SELECT id, instrument, direction, entry, sl, tp1, pnl, result, "
                "slippage_pips, score, regime, opened_at, closed_at, duration_min, "
                "strategy, broker, ab_variant "
                "FROM trades WHERE status='CLOSED' ORDER BY opened_at",
                fetch=True
            ).fetchall()
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            rows = db._execute(
                "SELECT id, instrument, direction, entry, sl, tp1, pnl, result, "
                "slippage_pips, score, regime, opened_at, closed_at, duration_min, "
                "strategy, broker, ab_variant "
                "FROM trades WHERE status='CLOSED' AND opened_at >= %s ORDER BY opened_at",
                (cutoff,), fetch=True
            ).fetchall()
    except Exception as e:
        logger.error(f"export_trades: erreur DB — {e}")
        return ""

    headers = [
        "id", "instrument", "direction", "entry", "sl", "tp1", "pnl", "result",
        "slippage_pips", "score", "regime", "opened_at", "closed_at",
        "duration_min", "strategy", "broker", "ab_variant"
    ]

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

    logger.info(f"✅ Export CSV : {len(rows)} trades → {output}")
    return output


def export_open_positions(output: str = "") -> str:
    """Exporte les positions actuellement ouvertes."""
    db = _get_db()
    output = output or f"positions_open_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        rows = db.load_open_positions()
    except Exception as e:
        logger.error(f"export_positions: erreur DB — {e}")
        return ""

    if not rows:
        logger.info("Aucune position ouverte à exporter")
        return ""

    headers = list(rows[0].keys()) if rows else []
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"✅ Export CSV : {len(rows)} positions ouvertes → {output}")
    return output


def export_weekly_summary(output: str = "") -> str:
    """Exporte un résumé hebdomadaire par instrument."""
    db = _get_db()
    output = output or f"weekly_summary_{datetime.now().strftime('%Y%m%d')}.csv"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    try:
        rows = db._execute(
            "SELECT instrument, COUNT(*) as trades, "
            "SUM(CASE WHEN result IN ('WIN','TP1','TP2','TP3') THEN 1 ELSE 0 END) as wins, "
            "SUM(pnl) as total_pnl "
            "FROM trades WHERE status='CLOSED' AND opened_at >= %s "
            "GROUP BY instrument ORDER BY total_pnl DESC",
            (cutoff,), fetch=True
        ).fetchall()
    except Exception as e:
        logger.error(f"export_weekly_summary: {e}")
        return ""

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["instrument", "trades", "wins", "total_pnl"])
        writer.writerows(rows)

    logger.info(f"✅ Export résumé hebdo : {len(rows)} instruments → {output}")
    return output


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEMESIS — Export Trades CSV")
    parser.add_argument("--days",   type=int, default=30,  help="Période en jours (défaut: 30)")
    parser.add_argument("--all",    action="store_true",   help="Exporter tous les trades")
    parser.add_argument("--open",   action="store_true",   help="Exporter positions ouvertes")
    parser.add_argument("--weekly", action="store_true",   help="Résumé hebdo par instrument")
    parser.add_argument("--output", type=str, default="",  help="Fichier de sortie")
    args = parser.parse_args()

    if args.open:
        path = export_open_positions(args.output)
    elif args.weekly:
        path = export_weekly_summary(args.output)
    else:
        path = export_closed_trades(days=args.days, output=args.output, export_all=args.all)

    if path:
        print(f"✅ Export terminé : {path}")
    else:
        print("❌ Export échoué — vérifier les logs")
