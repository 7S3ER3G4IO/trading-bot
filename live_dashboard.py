"""
live_dashboard.py — ⚡ Phase 4: Streamlit Live Dashboard

Real-time PnL monitoring, open positions, equity curve, heatmap.
No terminal reading needed — everything visualized on a web page.

Usage:
    streamlit run live_dashboard.py --server.port 8502

Reads from PostgreSQL (Supabase) and connects to the bot's state.
"""

import os
import time
from datetime import datetime, timezone, timedelta

try:
    import streamlit as st
    import pandas as pd
    import psycopg2
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

# ─── Database connection ────────────────────────────────────────────────────

def get_db():
    """Connect to Supabase PostgreSQL."""
    return psycopg2.connect(
        host=os.getenv("SUPABASE_HOST", "localhost"),
        port=int(os.getenv("SUPABASE_PORT", "5432")),
        dbname=os.getenv("SUPABASE_DB", "nemesis"),
        user=os.getenv("SUPABASE_USER", "nemesis"),
        password=os.getenv("SUPABASE_PASSWORD", ""),
    )


def query_df(sql: str, params=None) -> "pd.DataFrame":
    """Execute SQL and return DataFrame."""
    conn = get_db()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


# ─── Main Dashboard ─────────────────────────────────────────────────────────

def main():
    if not HAS_DEPS:
        print("❌ Install: pip install streamlit pandas psycopg2-binary")
        return

    st.set_page_config(
        page_title="⚡ Nemesis Trading Dashboard",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Dark theme CSS
    st.markdown("""
    <style>
    .stApp { background-color: #0e1117; color: #c9d1d9; }
    .metric-card {
        background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
        border: 1px solid #30363d;
        border-radius: 12px;
        padding: 20px;
        text-align: center;
    }
    .metric-value { font-size: 2em; font-weight: bold; }
    .win { color: #3fb950; }
    .loss { color: #f85149; }
    h1 { color: #58a6ff; }
    h2 { color: #c9d1d9; border-bottom: 1px solid #30363d; padding-bottom: 8px; }
    </style>
    """, unsafe_allow_html=True)

    st.title("⚡ Nemesis v2.0 — Live Dashboard")
    st.caption(f"Last refresh: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

    # ── Top metrics ──────────────────────────────────────────────────────

    try:
        equity = query_df(
            "SELECT balance, pnl_day, recorded_at FROM nemesis_equity "
            "ORDER BY recorded_at DESC LIMIT 1"
        )
        balance = equity.iloc[0]["balance"] if len(equity) > 0 else 0
        pnl_day = equity.iloc[0]["pnl_day"] if len(equity) > 0 else 0
    except Exception:
        balance, pnl_day = 0, 0

    try:
        trades_today = query_df(
            "SELECT COUNT(*) as cnt, SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins, "
            "COALESCE(SUM(pnl), 0) as total_pnl "
            "FROM capital_trades WHERE status='CLOSED' "
            "AND close_time > NOW() - INTERVAL '24 hours'"
        )
        cnt = int(trades_today.iloc[0]["cnt"]) if len(trades_today) > 0 else 0
        wins = int(trades_today.iloc[0]["wins"] or 0) if len(trades_today) > 0 else 0
        total_pnl = float(trades_today.iloc[0]["total_pnl"] or 0) if len(trades_today) > 0 else 0
        wr = round(wins / cnt * 100, 1) if cnt > 0 else 0
    except Exception:
        cnt, wins, total_pnl, wr = 0, 0, 0, 0

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("💰 Balance", f"{balance:,.2f}€")
    with col2:
        st.metric("📈 PnL Jour", f"{pnl_day:+,.2f}€",
                  delta=f"{total_pnl:+,.2f}€")
    with col3:
        st.metric("📋 Trades (24h)", f"{cnt}", delta=f"{wins}W / {cnt-wins}L")
    with col4:
        st.metric("🎯 Win Rate", f"{wr}%")

    st.divider()

    # ── Equity Curve ─────────────────────────────────────────────────────

    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("📈 Equity Curve")
        try:
            equity_hist = query_df(
                "SELECT balance, recorded_at FROM nemesis_equity "
                "ORDER BY recorded_at DESC LIMIT 500"
            )
            if len(equity_hist) > 0:
                equity_hist = equity_hist.sort_values("recorded_at")
                equity_hist["recorded_at"] = pd.to_datetime(equity_hist["recorded_at"])
                st.line_chart(
                    equity_hist.set_index("recorded_at")["balance"],
                    use_container_width=True,
                )
            else:
                st.info("Pas encore de données equity.")
        except Exception as e:
            st.warning(f"Erreur equity: {e}")

    with col_right:
        st.subheader("📊 Positions Ouvertes")
        try:
            open_trades = query_df(
                "SELECT instrument, direction, entry, sl, tp1, opened_at "
                "FROM capital_trades WHERE status='OPEN' "
                "ORDER BY opened_at DESC"
            )
            if len(open_trades) > 0:
                st.dataframe(open_trades, use_container_width=True, hide_index=True)
            else:
                st.info("Aucune position ouverte.")
        except Exception as e:
            st.warning(f"Erreur positions: {e}")

    st.divider()

    # ── Recent Trades ────────────────────────────────────────────────────

    st.subheader("📋 Trades Récents (7 jours)")
    try:
        recent = query_df(
            "SELECT instrument, direction, entry, close_price, pnl, result, "
            "duration_min, close_time "
            "FROM capital_trades WHERE status='CLOSED' "
            "AND close_time > NOW() - INTERVAL '7 days' "
            "ORDER BY close_time DESC LIMIT 50"
        )
        if len(recent) > 0:
            # Color PnL
            recent["pnl"] = recent["pnl"].apply(lambda x: round(x or 0, 2))
            st.dataframe(recent, use_container_width=True, hide_index=True)

            # PnL by day
            st.subheader("📊 PnL par Jour")
            recent["day"] = pd.to_datetime(recent["close_time"]).dt.date
            daily = recent.groupby("day")["pnl"].sum().reset_index()
            st.bar_chart(daily.set_index("day")["pnl"], use_container_width=True)
        else:
            st.info("Aucun trade fermé sur les 7 derniers jours.")
    except Exception as e:
        st.warning(f"Erreur trades: {e}")

    st.divider()

    # ── Engine Attribution ───────────────────────────────────────────────

    st.subheader("🧠 Performance par Moteur (30j)")
    try:
        from brokers.capital_client import ASSET_PROFILES
        engine_data = query_df(
            "SELECT instrument, pnl, result FROM capital_trades "
            "WHERE status='CLOSED' AND close_time > NOW() - INTERVAL '30 days'"
        )
        if len(engine_data) > 0:
            def get_engine(inst):
                p = ASSET_PROFILES.get(inst, {})
                return p.get("god_engine", p.get("strat", "?"))

            engine_data["engine"] = engine_data["instrument"].apply(get_engine)
            summary = engine_data.groupby("engine").agg(
                trades=("pnl", "count"),
                pnl_total=("pnl", "sum"),
                wr=("result", lambda x: round(sum(x == "WIN") / len(x) * 100, 1))
            ).reset_index()
            st.dataframe(summary, use_container_width=True, hide_index=True)
    except Exception as e:
        st.caption(f"Attribution error: {e}")

    # ── Health Status ────────────────────────────────────────────────────

    st.subheader("🏥 System Health")
    try:
        state = query_df(
            "SELECT key, value, updated_at FROM nemesis_bot_state "
            "ORDER BY updated_at DESC LIMIT 10"
        )
        if len(state) > 0:
            st.dataframe(state, use_container_width=True, hide_index=True)
    except Exception:
        st.info("Pas de données système.")

    # Auto-refresh
    time.sleep(30)
    st.rerun()


if __name__ == "__main__":
    main()
