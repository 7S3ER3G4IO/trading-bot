"""
backtester_oanda.py — Backtest London/NY Open Breakout sur données Capital.com.

Télécharge les données directement via CapitalClient (instruments réels :
GOLD, EURUSD, GBPUSD, USDJPY, US500, US100, DE40, OIL_BRENT),
puis simule la stratégie breakout sur les sessions London/NY.

Usage :
    python3 backtester_oanda.py
    python3 backtester_oanda.py --days 30 --rr 2.0 --symbol GOLD
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv; load_dotenv()

import pandas as pd
from datetime import datetime, timezone
from loguru import logger
logger.remove()

from strategy import Strategy, SIGNAL_HOLD
from brokers.capital_client import CapitalClient, CAPITAL_INSTRUMENTS

DEFAULT_DAYS = 30     # Capital.com : ~1000 bougies 5m max ≈ 3.5j
DEFAULT_RR   = 1.8
INITIAL      = 10_000.0
RISK_PCT     = 0.01
TF           = "5m"
WINDOW       = 60

# Heures London + NY en UTC
SESSIONS = [
    (8*60-15,  10*60),   # London  07h45 → 10h00
    (13*60+15, 16*60),   # NY      13h15 → 16h00
]

def in_session(ts: pd.Timestamp) -> bool:
    """Retourne True si le timestamp est dans une fenêtre de session."""
    t = ts.hour * 60 + ts.minute
    return any(start <= t <= end for start, end in SESSIONS)

def fetch(symbol: str, days: int, tf: str = TF) -> pd.DataFrame:
    """Télécharge les bougies depuis Capital.com (instrument réel)."""
    client = CapitalClient()
    if not client.available:
        raise RuntimeError("Capital.com non disponible — vérifiez les variables d'env")
    count = min(days * 288, 1000)
    df = client.fetch_ohlcv(symbol, timeframe=tf, count=count)
    if df is None or df.empty:
        raise ValueError(f"Aucune donnée Capital.com pour {symbol}")
    return df

def backtest_instrument(sym: str, name: str, days: int, rr: float) -> dict:
    """Backteste la stratégie breakout sur un instrument."""
    print(f"  📥 {name:<16}...", end=" ", flush=True)
    try:
        df = fetch(sym, days)
    except Exception as e:
        print(f"❌ {e}")
        return None

    strat   = Strategy()
    balance = INITIAL
    trades  = []
    in_trade = False

    for i in range(WINDOW, len(df)):
        ts = df.index[i]

        # Simuler le filtre session (breakout uniquement pendant London/NY)
        if not in_session(ts):
            continue

        if in_trade:
            continue

        w = df.iloc[i - WINDOW:i].copy()
        w = strat.compute_indicators(w)

        # Signal breakout avec futures_mode=True pour bypasser is_session_ok()
        # (on la gère manuellement avec in_session())
        sig, score, _ = strat.get_signal(w, min_score_override=2, futures_mode=True)
        if sig == SIGNAL_HOLD:
            continue

        entry = float(df.iloc[i]["close"])
        sr    = strat.compute_session_range(w)
        lvl   = strat.get_sl_tp(sig, entry, sr, rr=rr)
        sl, tp = lvl["sl"], lvl["tp"]

        if sl <= 0 or tp <= 0 or sr["size"] <= 0:
            continue

        qty = min(balance * RISK_PCT / sr["size"], balance * 0.9 / entry)
        if qty <= 0:
            continue

        # Simulation bougie par bougie jusqu'à SL ou TP
        result, pnl = "ENC", 0.0
        in_trade = True

        for j in range(i + 1, min(i + 200, len(df))):  # Max 200 bougies (~16h)
            hi = float(df.iloc[j]["high"])
            lo = float(df.iloc[j]["low"])
            if sig == "BUY":
                if hi >= tp: pnl = (tp - entry) * qty; result = "TP"; break
                if lo <= sl: pnl = (sl - entry) * qty; result = "SL"; break
            else:
                if lo <= tp: pnl = (entry - tp) * qty; result = "TP"; break
                if hi >= sl: pnl = (entry - sl) * qty; result = "SL"; break

        if result == "ENC":
            last = float(df.iloc[min(i + 200, len(df)-1)]["close"])
            pnl  = (last - entry) * qty * (1 if sig == "BUY" else -1)

        fee  = entry * qty * 0.0002 * 2  # OANDA ~0.02% spread pris à l'ouverture/fermeture
        net  = pnl - fee
        balance += net
        in_trade = False
        trades.append({"sig": sig, "result": result, "net": net})

    n    = len(trades)
    wins = sum(1 for t in trades if t["result"] == "TP")
    sls  = sum(1 for t in trades if t["result"] == "SL")
    wr   = wins / n * 100 if n else 0
    rend = (balance - INITIAL) / INITIAL * 100
    pnl_t = sum(t["net"] for t in trades)

    peak, bal, maxdd = INITIAL, INITIAL, 0.0
    for t in trades:
        bal += t["net"]
        if bal > peak: peak = bal
        dd = (peak - bal) / peak * 100
        if dd > maxdd: maxdd = dd

    icon    = "🟢" if rend >= 0 else "🔴"
    verdict = "✅ PROFITABLE" if (rend > 0 and wr >= 45) else ("⚠️  MOYEN" if rend >= 0 else "❌ PERTE")
    print(f"T={n:3d} | WR={wr:5.1f}% | PnL={pnl_t:+8.0f}$ | Rend={rend:+6.1f}% | DD={maxdd:4.1f}%  {icon} {verdict}")
    return {"name": name, "sym": sym, "n": n, "wr": wr, "pnl": pnl_t, "rend": rend, "dd": maxdd}

# ── Instruments Capital.com à backtester ─────────────────────────────────────
BACKTEST_LIST = [
    # epic Capital.com   nom lisible
    ("GOLD",      "Or / Gold"),
    ("EURUSD",    "EUR/USD"),
    ("GBPUSD",    "GBP/USD"),
    ("USDJPY",    "USD/JPY"),
    ("US500",     "S&P 500"),
    ("US100",     "NASDAQ 100"),
    ("DE40",      "DAX 40"),
    ("OIL_BRENT", "Brent Oil"),
]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--rr",   type=float, default=DEFAULT_RR)
    args = parser.parse_args()

    print(f"\n{'='*72}")
    print(f"  ⚡ BACKTEST BREAKOUT London/NY Open — {args.days}j | 5m | R:R 1:{args.rr}")
    print(f"  Sessions : London 07h45-10h00 UTC | NY 13h15-16h00 UTC")
    print(f"  Capital  : {INITIAL:,.0f}$ | Risk 1%/trade | Score ≥ 2/3")
    print(f"{'='*72}")
    print(f"  {'Instrument':<18} {'T':>3}     {'WR':>5}    {'PnL':>8}    {'Rend':>6}   {'DD':>5}")
    print(f"  {'─'*65}")

    results = []
    for sym, name in BACKTEST_LIST:
        r = backtest_instrument(sym, name, args.days, args.rr)
        if r:
            results.append(r)

    if results:
        print(f"\n{'='*72}")
        print(f"  📊 CLASSEMENT")
        print(f"{'='*72}")
        results.sort(key=lambda x: x["rend"], reverse=True)
        for i, r in enumerate(results, 1):
            e = "🟢" if r["rend"] >= 0 else "🔴"
            v = "✅" if (r["rend"] > 0 and r["wr"] >= 45) else ("⚠️" if r["rend"] >= 0 else "❌")
            print(f"  {i:2}. {e}{r['name']:<20} T={r['n']:3d}  WR={r['wr']:5.1f}%  Rend={r['rend']:+6.1f}%  {v}")

        profitable = [r for r in results if r["rend"] > 0 and r["wr"] >= 45]
        avg_wr   = sum(r["wr"]   for r in results) / len(results)
        avg_rend = sum(r["rend"] for r in results) / len(results)
        print(f"\n  Moyenne WR={avg_wr:.1f}% | Rend avg={avg_rend:+.1f}%")
        print(f"  Instruments rentables : {len(profitable)}/{len(results)}")
        print(f"{'='*72}\n")
