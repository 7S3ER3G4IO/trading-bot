"""
monitor_week.py — Script de suivi pour la semaine d'observation testnet.
Affiche un résumé rapide du bot en temps réel depuis les logs et la BDD.
Usage : python3 monitor_week.py
"""
import os, sqlite3, json
from datetime import datetime, timezone, timedelta

DB_PATH     = "logs/alphatrader.db"
TRADES_JSON = "logs/daily_trades.json"

def fmt_pct(n, total):
    return f"{n/total*100:.0f}%" if total > 0 else "N/A"

def run():
    now = datetime.now(timezone(timedelta(hours=1)))
    print(f"\n{'='*55}")
    print(f"  ⚡ AlphaTrader — Monitoring Semaine  {now.strftime('%d/%m/%Y %H:%M')}")
    print(f"{'='*55}")

    # ─── Base de données ───────────────────────────────────────
    if not os.path.exists(DB_PATH):
        print("  ⚠️  Aucune BDD trouvée — bot jamais démarré localement")
    else:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.execute(
            "SELECT symbol, side, entry, current_sl, tp1_hit, tp2_hit, be_active, "
            "remaining, total_pnl, opened_at FROM trades WHERE status='OPEN'"
        )
        rows = cur.fetchall()
        print(f"\n  📊 TRADES OUVERTS : {len(rows)}")
        for r in rows:
            sym, side, entry, sl, tp1h, tp2h, be, rem, pnl, opened = r
            tp_status = f"TP1{'✅' if tp1h else '○'} TP2{'✅' if tp2h else '○'}"
            be_lbl    = " 🛡️BE" if be else ""
            print(f"     {side:<4} {sym:<10} @ {entry:>10,.2f} | {tp_status}{be_lbl} | PnL≈{pnl:+.2f}")

        # Stats fermés (7 derniers jours)
        cur2 = conn.execute(
            "SELECT COUNT(*), SUM(total_pnl - fees), "
            "SUM(CASE WHEN result NOT LIKE '%SL%' THEN 1 ELSE 0 END) "
            "FROM trades WHERE status='CLOSED' AND date(closed_at) >= date('now','-7 days')"
        )
        total, net_pnl, wins = cur2.fetchone()
        total  = total or 0
        net_pnl = net_pnl or 0
        wins   = wins or 0
        print(f"\n  📈 SEMAINE (7j) ")
        print(f"     Trades  : {total}")
        print(f"     Win rate: {fmt_pct(wins, total)}")
        print(f"     PnL net : {net_pnl:+.2f} USDT")
        conn.close()

    # ─── Journal journalier ────────────────────────────────────
    if os.path.exists(TRADES_JSON):
        with open(TRADES_JSON) as f:
            trades = json.load(f)
        print(f"\n  📋 AUJOURD'HUI ({len(trades)} trades enregistrés)")
        for t in trades:
            result_label = "(BE)" if t["result"] == "BE" else \
                           f"-{t['pips']:.0f} pips" if t["result"] == "SL" else \
                           f"+{t['pips']:.0f} pips"
            print(f"     {t['date_str']} {t['side']:<5} {t['symbol']:<5}  {result_label}  net={t['pnl_net']:+.2f}")
    else:
        print("\n  📋 Aucun trade aujourd'hui")

    print(f"\n{'='*55}")
    print("  ✅ Checklist semaine d'observation :")
    print("     [ ] Bot actif 24/7 sans crash (Railway)")
    print("     [ ] Pre-alerts reçus sur Telegram")
    print("     [ ] Signaux cohérents (pauses news respectées)")
    print("     [ ] SL/TP exécutés correctement")
    print("     [ ] Canal wallet mis à jour toutes les 30min")
    print(f"{'='*55}\n")

if __name__ == "__main__":
    run()
