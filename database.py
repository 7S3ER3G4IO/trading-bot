"""
database.py — Persistance PostgreSQL via Supabase (remplace SQLite).
Les données survivent aux redéploiements Railway.
"""
import os
from datetime import datetime, timezone
from typing import Optional, List
from loguru import logger

try:
    import psycopg2
    import psycopg2.extras
    HAS_PG = True
except ImportError:
    HAS_PG = False

import sqlite3

# ─── URL de connexion ────────────────────────────────────────────────────────
# Variable d'environnement sur Railway : DATABASE_URL
# Format Supabase Session Pooler (IPv4 compatible) :
# postgresql://postgres.XXXX:[PWD]@aws-1-eu-west-1.pooler.supabase.com:5432/postgres
DATABASE_URL = os.getenv("DATABASE_URL")
_SQLITE_PATH = "logs/alphatrader.db"


class Database:
    """
    Base de données unifiée : PostgreSQL (Supabase) si DATABASE_URL est défini,
    SQLite local sinon (développement / fallback).
    """

    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self._pg = DATABASE_URL and HAS_PG
        if self._pg:
            self._conn = psycopg2.connect(DATABASE_URL, sslmode="require")
            self._conn.autocommit = False
            logger.info("🗄️  PostgreSQL Supabase connecté ✅")
        else:
            self._conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
            if not DATABASE_URL:
                logger.warning("⚠️  DATABASE_URL non défini — SQLite local (données non persistantes)")
            else:
                logger.warning("⚠️  psycopg2 absent — SQLite local utilisé")
        self._create_tables()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _execute(self, sql: str, params=(), fetch=False):
        """Exécute une requête compatible SQLite/PostgreSQL."""
        # SQLite utilise ? comme placeholder, PostgreSQL utilise %s
        if not self._pg:
            sql = sql.replace("%s", "?")
        cur = self._conn.cursor()
        cur.execute(sql, params)
        if not self._conn.autocommit:
            self._conn.commit()
        if fetch:
            return cur
        return cur

    def _create_tables(self):
        serial = "SERIAL" if self._pg else "INTEGER"
        text   = "TEXT"
        real   = "REAL"
        int_   = "INTEGER"

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS trades (
            id           {serial} PRIMARY KEY,
            symbol       {text}    NOT NULL,
            side         {text}    NOT NULL,
            entry        {real},
            exit_price   {real},
            amount       {real},
            sl           {real},
            tp1          {real},
            tp2          {real},
            tp3          {real},
            be           {real},
            current_sl   {real},
            remaining    {real},
            tp1_hit      {int_}    DEFAULT 0,
            tp2_hit      {int_}    DEFAULT 0,
            be_active    {int_}    DEFAULT 0,
            total_pnl    {real}    DEFAULT 0,
            status       {text}    DEFAULT 'OPEN',
            result       {text},
            fees         {real}    DEFAULT 0,
            opened_at    {text},
            closed_at    {text},
            sl_order_id  {text},
            tp1_order_id {text},
            tp2_order_id {text}
        )
        """)

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS daily_stats (
            id          {serial} PRIMARY KEY,
            date        {text}   UNIQUE,
            trades_won  {int_}   DEFAULT 0,
            trades_lost {int_}   DEFAULT 0,
            pnl_gross   {real}   DEFAULT 0,
            pnl_fees    {real}   DEFAULT 0,
            balance_end {real}   DEFAULT 0
        )
        """)

        self._execute(f"""
        CREATE TABLE IF NOT EXISTS weekly_stats (
            id           {serial} PRIMARY KEY,
            week_start   {text}   UNIQUE,
            trades_tot   {int_}   DEFAULT 0,
            winrate      {real}   DEFAULT 0,
            pnl_net      {real}   DEFAULT 0,
            best_trade   {real}   DEFAULT 0,
            worst_trade  {real}   DEFAULT 0,
            max_drawdown {real}   DEFAULT 0
        )
        """)

        # Migration des colonnes pour les BDD SQLite existantes
        if not self._pg:
            for col in ["sl_order_id TEXT", "tp1_order_id TEXT", "tp2_order_id TEXT"]:
                try:
                    self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
                    self._conn.commit()
                except Exception:
                    pass

    # ─── Trades ──────────────────────────────────────────────────────────────

    def save_trade_open(self, trade) -> int:
        """Sauvegarde un trade à l'ouverture. Retourne l'ID."""
        ph = "%s" if self._pg else "?"
        sql = f"""
            INSERT INTO trades
            (symbol, side, entry, amount, sl, tp1, tp2, tp3, be,
             current_sl, remaining, opened_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})
        """
        if self._pg:
            sql += " RETURNING id"
        cur = self._execute(sql, (
            trade.symbol, trade.side, trade.entry, trade.total_amount,
            trade.initial_sl, trade.tp1, trade.tp2, trade.tp3, trade.be,
            trade.current_sl, trade.remaining,
            datetime.now(timezone.utc).isoformat()
        ))
        if self._pg:
            trade_id = cur.fetchone()[0]
        else:
            trade_id = cur.lastrowid
        logger.debug(f"🗄️  Trade sauvegardé ID={trade_id}")
        return trade_id

    def update_trade(self, trade_id: int, **kwargs):
        """Met à jour les champs d'un trade."""
        if not kwargs:
            return
        ph = "%s" if self._pg else "?"
        cols = ", ".join(f"{k}={ph}" for k in kwargs)
        vals = list(kwargs.values()) + [trade_id]
        self._execute(f"UPDATE trades SET {cols} WHERE id={ph}", vals)

    def close_trade(self, trade_id: int, exit_price: float, result: str,
                    total_pnl: float, fees: float):
        """Marque un trade comme fermé."""
        ph = "%s" if self._pg else "?"
        self._execute(f"""
            UPDATE trades SET
                exit_price={ph}, result={ph}, total_pnl={ph}, fees={ph},
                status='CLOSED', closed_at={ph}
            WHERE id={ph}
        """, (exit_price, result, total_pnl, fees,
              datetime.now(timezone.utc).isoformat(), trade_id))

    def load_open_trades(self) -> List[dict]:
        """Charge les trades ouverts (reprise après redémarrage)."""
        cur = self._execute(
            "SELECT * FROM trades WHERE status='OPEN'",
            fetch=True
        )
        if self._pg:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        else:
            cur2 = self._conn.execute("SELECT * FROM trades WHERE status='OPEN'")
            cols = [d[0] for d in cur2.description]
            rows = [dict(zip(cols, row)) for row in cur2.fetchall()]
        if rows:
            logger.info(f"🔄 {len(rows)} trades ouverts restaurés depuis la BDD")
        return rows

    # ─── Stats ───────────────────────────────────────────────────────────────

    def get_closed_trades(self, days: int = 7) -> List[dict]:
        ph = "%s" if self._pg else "?"
        if self._pg:
            sql = f"""
                SELECT * FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '{days} days')
                ORDER BY closed_at DESC
            """
            cur = self._execute(sql, fetch=True)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            cur = self._conn.execute(f"""
                SELECT * FROM trades WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-{days} days')
                ORDER BY closed_at DESC
            """)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_weekly_pnl(self) -> float:
        if self._pg:
            cur = self._execute("""
                SELECT COALESCE(SUM(total_pnl - fees), 0) FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '7 days')
            """, fetch=True)
        else:
            cur = self._conn.execute("""
                SELECT COALESCE(SUM(total_pnl - fees), 0) FROM trades
                WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-7 days')
            """)
        return cur.fetchone()[0] or 0.0

    def get_total_fees(self, days: int = 30) -> float:
        if self._pg:
            cur = self._execute(f"""
                SELECT COALESCE(SUM(fees), 0) FROM trades
                WHERE status='CLOSED'
                AND closed_at::date >= (CURRENT_DATE - INTERVAL '{days} days')
            """, fetch=True)
        else:
            cur = self._conn.execute(f"""
                SELECT COALESCE(SUM(fees), 0) FROM trades
                WHERE status='CLOSED'
                AND date(closed_at) >= date('now', '-{days} days')
            """)
        return cur.fetchone()[0] or 0.0
