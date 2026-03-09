"""
database.py — Persistance PostgreSQL via Supabase (remplace SQLite).
Les données survivent aux redéploiements Railway.
"""
import os
import threading
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
_SQLITE_PATH = "logs/nemesis.db"


class Database:
    """
    Base de données unifiée : PostgreSQL (Supabase) si DATABASE_URL est défini,
    SQLite local sinon (développement / fallback).
    """

    def __init__(self):
        os.makedirs("logs", exist_ok=True)
        self._pg = DATABASE_URL and HAS_PG
        self._lock = threading.Lock()  # Thread-safety pour SQLite
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
        """Exécute une requête compatible SQLite/PostgreSQL (thread-safe)."""
        if not self._pg:
            sql = sql.replace("%s", "?")
        with self._lock:
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

        # Table Capital.com (persistance CFD trades après redéploiement Railway)
        self._execute(f"""
        CREATE TABLE IF NOT EXISTS capital_trades (
            id          {serial} PRIMARY KEY,
            instrument  {text}   NOT NULL UNIQUE,
            direction   {text},
            entry       {real},
            sl          {real},
            tp1         {real},
            tp2         {real},
            tp3         {real},
            ref1        {text},
            ref2        {text},
            ref3        {text},
            tp1_hit     {int_}   DEFAULT 0,
            tp2_hit     {int_}   DEFAULT 0,
            opened_at   {text},
            status      {text}   DEFAULT 'OPEN'
        )
        """)

        # Migration colonnes SQLite
        if not self._pg:
            for col in ["sl_order_id TEXT", "tp1_order_id TEXT", "tp2_order_id TEXT"]:
                try:
                    self._conn.execute(f"ALTER TABLE trades ADD COLUMN {col}")
                    self._conn.commit()
                except Exception:
                    pass

    # ─── Trades Binance ───────────────────────────────────────────────────────

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

    # ─── Capital.com trades ───────────────────────────────────────────

    def save_capital_trade(self, instrument: str, state: dict):
        """
        Sauvegarde (INSERT OR REPLACE) un trade Capital.com ouvert.
        Appelé à l'ouverture et lors des mises à jour tp1_hit / tp2_hit.
        """
        ph = "%s" if self._pg else "?"
        refs = state.get("refs", [None, None, None])
        try:
            if self._pg:
                sql = f"""
                    INSERT INTO capital_trades
                        (instrument, direction, entry, sl, tp1, tp2, tp3,
                         ref1, ref2, ref3, tp1_hit, tp2_hit, opened_at, status)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN')
                    ON CONFLICT (instrument) DO UPDATE SET
                        direction=EXCLUDED.direction, entry=EXCLUDED.entry, sl=EXCLUDED.sl,
                        tp1=EXCLUDED.tp1, tp2=EXCLUDED.tp2, tp3=EXCLUDED.tp3,
                        ref1=EXCLUDED.ref1, ref2=EXCLUDED.ref2, ref3=EXCLUDED.ref3,
                        tp1_hit=EXCLUDED.tp1_hit, tp2_hit=EXCLUDED.tp2_hit, status='OPEN'
                """
            else:
                sql = f"""
                    INSERT OR REPLACE INTO capital_trades
                        (instrument, direction, entry, sl, tp1, tp2, tp3,
                         ref1, ref2, ref3, tp1_hit, tp2_hit, opened_at, status)
                    VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},'OPEN')
                """
            self._execute(sql, (
                instrument,
                state.get("direction", ""),
                state.get("entry", 0.0),
                state.get("sl", 0.0),
                state.get("tp1", 0.0),
                state.get("tp2", 0.0),
                state.get("tp3", 0.0),
                refs[0] if len(refs) > 0 else None,
                refs[1] if len(refs) > 1 else None,
                refs[2] if len(refs) > 2 else None,
                int(state.get("tp1_hit", False)),
                int(state.get("tp2_hit", False)),
                datetime.now(timezone.utc).isoformat(),
            ))
        except Exception as e:
            logger.error(f"❌ DB save_capital_trade {instrument}: {e}")

    def load_open_capital_trades(self) -> List[dict]:
        """Charge les trades Capital.com ouverts (reprise après redémarrage)."""
        try:
            cur = self._execute(
                "SELECT * FROM capital_trades WHERE status='OPEN'",
                fetch=True
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            if rows:
                logger.info(f"🔄 {len(rows)} trades Capital.com restaurés depuis la BDD")
            return rows
        except Exception as e:
            logger.error(f"❌ DB load_capital_trades: {e}")
            return []

    def close_capital_trade(self, instrument: str):
        """Marque un trade Capital.com comme fermé."""
        ph = "%s" if self._pg else "?"
        try:
            self._execute(
                f"UPDATE capital_trades SET status='CLOSED' WHERE instrument={ph}",
                (instrument,)
            )
        except Exception as e:
            logger.error(f"❌ DB close_capital_trade {instrument}: {e}")
