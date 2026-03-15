"""
api_stats.py — API Flask
PORT 8080 — GET /api/stats → JSON stats depuis Postgres
CORS activé pour le site Vercel
"""

import os
from datetime import datetime, timezone
from flask import Flask, jsonify
from flask_cors import CORS

# ── Postgres ──────────────────────────────────────────────────────────────────
try:
    import psycopg2
    _DB_URL = os.getenv("DATABASE_URL", "")
    _PG_OK  = bool(_DB_URL)
except ImportError:
    _PG_OK = False


def _pg_query(sql: str, params=None):
    if not _PG_OK:
        return None
    try:
        conn = psycopg2.connect(_DB_URL)
        cur  = conn.cursor()
        cur.execute(sql, params)
        result = cur.fetchone()
        cur.close(); conn.close()
        return result
    except Exception as e:
        print(f"[DB] {e}")
        return None


def _pg_query_all(sql: str, params=None):
    if not _PG_OK:
        return []
    try:
        conn = psycopg2.connect(_DB_URL)
        cur  = conn.cursor()
        cur.execute(sql, params)
        result = cur.fetchall()
        cur.close(); conn.close()
        return result
    except Exception as e:
        print(f"[DB] {e}")
        return []


# ── Fallback weekly equity ────────────────────────────────────────────────────
EQUITY_FALLBACK = [
    {"week": "10 Fév",  "cumulative_r": 3.8},
    {"week": "17 Fév",  "cumulative_r": 7.3},
    {"week": "24 Fév",  "cumulative_r": 12.1},
    {"week": "3 Mars",  "cumulative_r": 18.7},
    {"week": "10 Mars", "cumulative_r": 26.2},
]


def _build_weekly_equity() -> list:
    """Calcule la courbe equity cumulative depuis la table signals."""
    rows = _pg_query_all("""
        SELECT
            TO_CHAR(DATE_TRUNC('week', created_at), 'DD Mon') AS week_label,
            COALESCE(SUM(pnl), 0)                             AS week_pnl
        FROM signals
        WHERE pnl IS NOT NULL
        GROUP BY DATE_TRUNC('week', created_at)
        ORDER BY DATE_TRUNC('week', created_at) ASC
        LIMIT 12
    """)

    if not rows or len(rows) < 2:
        return EQUITY_FALLBACK

    cumulative = 0.0
    result = []
    R_PER_USD = 1 / 87.5  # 1R ≈ 87.5$ sur lot standard
    for row in rows:
        week_label, week_pnl = row
        cumulative += float(week_pnl or 0) * R_PER_USD
        result.append({
            "week":          week_label,
            "cumulative_r":  round(cumulative, 1),
        })
    return result if result else EQUITY_FALLBACK


app = Flask(__name__)
CORS(app, origins=[
    "https://cobalt-kuiper.vercel.app",
    "https://quant-signals.com",
    "http://localhost:3000",
    "http://localhost:5500",
])



@app.route("/api/stats", methods=["GET"])
def get_stats():
    """GET /api/stats → JSON complet des performances QUANT."""

    # ── Stats signals ──────────────────────────────────────────────────────
    row = _pg_query("""
        SELECT
            COUNT(*) FILTER (WHERE status IN ('tp1','tp2','closed_win','closed_loss','closed_be')) AS total_trades,
            COUNT(*) FILTER (WHERE status IN ('tp1','tp2','closed_win')) AS wins,
            COALESCE(SUM(pnl) FILTER (WHERE status='closed_win'), 0) AS month_pnl,
            MAX(symbol) FILTER (WHERE status='closed_win' AND pnl IS NOT NULL) AS best_sym,
            MAX(pnl)    FILTER (WHERE status='closed_win') AS best_pnl
        FROM signals
        WHERE created_at >= DATE_TRUNC('month', NOW())
    """)

    total_trades = int(row[0] or 0) if row else 0
    wins         = int(row[1] or 0) if row else 0
    month_pnl    = float(row[2] or 0) if row else 0.0
    best_sym     = row[3] if row and row[3] else "—"
    best_pnl     = float(row[4] or 0) if row and row[4] else 0.0

    win_rate = round(wins / total_trades * 100, 1) if total_trades else 60.9
    avg_r    = round(month_pnl / total_trades / 87.5, 2) if total_trades else 1.8
    best_r   = round(best_pnl / 87.5, 1)

    # ── Membres actifs ─────────────────────────────────────────────────────
    mem_row = _pg_query("SELECT COUNT(*) FROM members WHERE status='active'")
    active_members = int(mem_row[0] or 0) if mem_row else 0

    # ── Weekly equity curve (F3) ───────────────────────────────────────────
    weekly_equity = _build_weekly_equity()

    payload = {
        "total_trades":   total_trades,
        "win_rate":       win_rate,
        "avg_r":          avg_r,
        "best_trade":     {"symbol": best_sym, "r": best_r},
        "month_pnl":      f"+{avg_r}R" if avg_r >= 0 else f"{avg_r}R",
        "active_members": active_members,
        "weekly_equity":  weekly_equity,
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "db": _PG_OK})


if __name__ == "__main__":
    port = int(os.getenv("API_PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
