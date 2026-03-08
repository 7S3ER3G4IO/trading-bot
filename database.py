"""
database.py — Persistance SQLite des trades.
Si Railway redémarre, les trades ouverts sont retrouvés automatiquement.
"""
import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional, List
from loguru import logger

DB_PATH = "logs/alphatrader.db"


class Database:
    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._create_tables()
        logger.info(f"🗄️  Base de données initialisée : {DB_PATH}")

    def _create_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            entry       REAL,
            exit_price  REAL,
            amount      REAL,
            sl          REAL,
            tp1         REAL,
            tp2         REAL,
            tp3         REAL,
            be          REAL,
            current_sl  REAL,
            remaining   REAL,
            tp1_hit     INTEGER DEFAULT 0,
            tp2_hit     INTEGER DEFAULT 0,
            be_active   INTEGER DEFAULT 0,
            total_pnl   REAL DEFAULT 0,
            status      TEXT DEFAULT 'OPEN',
            result      TEXT,
            fees        REAL DEFAULT 0,
            opened_at   TEXT,
            closed_at   TEXT,
            sl_order_id  TEXT,
            tp1_order_id TEXT,
            tp2_order_id TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT UNIQUE,
            trades_won  INTEGER DEFAULT 0,
            trades_lost INTEGER DEFAULT 0,
            pnl_gross   REAL DEFAULT 0,
            pnl_fees    REAL DEFAULT 0,
            balance_end REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS weekly_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start  TEXT UNIQUE,
            trades_tot  INTEGER DEFAULT 0,
            winrate     REAL DEFAULT 0,
            pnl_net     REAL DEFAULT 0,
            best_trade  REAL DEFAULT 0,
            worst_trade REAL DEFAULT 0,
            max_drawdown REAL DEFAULT 0
        );
        """)
        # Migration pour les BDD existantes (ajoute les colonnes si absentes)
        for col in ["sl_order_id", "tp1_order_id", "tp2_order_id"]:
            try:
                self.conn.execute(f"ALTER TABLE trades ADD COLUMN {col} TEXT")
                self.conn.commit()
            except Exception:
                pass  # Colonne déjà existante

    # ─── Trades ──────────────────────────────────────────────────────────────

    def save_trade_open(self, trade) -> int:
        """Sauvegarde un trade à l'ouverture. Retourne l'ID."""
        cur = self.conn.execute("""
            INSERT INTO trades
            (symbol, side, entry, amount, sl, tp1, tp2, tp3, be,
             current_sl, remaining, opened_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade.symbol, trade.side, trade.entry, trade.total_amount,
            trade.initial_sl, trade.tp1, trade.tp2, trade.tp3, trade.be,
            trade.current_sl, trade.remaining,
            datetime.now(timezone.utc).isoformat()
        ))
        self.conn.commit()
        trade_id = cur.lastrowid
        logger.debug(f"🗄️  Trade sauvegardé ID={trade_id}")
        return trade_id

    def update_trade(self, trade_id: int, **kwargs):
        """Met à jour les champs d'un trade."""
        if not kwargs:
            return
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [trade_id]
        self.conn.execute(f"UPDATE trades SET {cols} WHERE id=?", vals)
        self.conn.commit()

    def close_trade(self, trade_id: int, exit_price: float, result: str,
                    total_pnl: float, fees: float):
        """Marque un trade comme fermé."""
        self.conn.execute("""
            UPDATE trades SET
                exit_price=?, result=?, total_pnl=?, fees=?,
                status='CLOSED', closed_at=?
            WHERE id=?
        """, (exit_price, result, total_pnl, fees,
              datetime.now(timezone.utc).isoformat(), trade_id))
        self.conn.commit()

    def load_open_trades(self) -> List[dict]:
        """Charge les trades ouverts (pour reprise après redémarrage)."""
        cur = self.conn.execute(
            "SELECT * FROM trades WHERE status='OPEN'"
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        if rows:
            logger.info(f"🔄 {len(rows)} trades ouverts retrouvés en BDD")
        return rows

    # ─── Stats ───────────────────────────────────────────────────────────────

    def get_closed_trades(self, days: int = 7) -> List[dict]:
        cur = self.conn.execute("""
            SELECT * FROM trades
            WHERE status='CLOSED'
            AND date(closed_at) >= date('now', ?)
            ORDER BY closed_at DESC
        """, (f"-{days} days",))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_weekly_pnl(self) -> float:
        cur = self.conn.execute("""
            SELECT COALESCE(SUM(total_pnl - fees), 0)
            FROM trades WHERE status='CLOSED'
            AND date(closed_at) >= date('now', '-7 days')
        """)
        return cur.fetchone()[0] or 0.0

    def get_total_fees(self, days: int = 30) -> float:
        cur = self.conn.execute("""
            SELECT COALESCE(SUM(fees), 0) FROM trades
            WHERE status='CLOSED'
            AND date(closed_at) >= date('now', ?)
        """, (f"-{days} days",))
        return cur.fetchone()[0] or 0.0
