#!/usr/bin/env python3
"""
dynamic_pairlist.py — Dynamic Pairlist (#5)

Scanne Binance en temps réel et sélectionne automatiquement
les 4 actifs les plus volatils et liquides du moment.

Usage:
    python3 dynamic_pairlist.py               # affiche le top 4
    python3 dynamic_pairlist.py --update      # met à jour config.py
    python3 dynamic_pairlist.py --top 6       # top 6 actifs
"""
import sys, os, json, warnings, argparse
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
import ccxt
from loguru import logger

# Actifs toujours exclus (trop peu liquides / problèmes historiques)
BLACKLIST = {
    "USDC/USDT","BUSD/USDT","TUSD/USDT","USDP/USDT",
    "EUR/USDT","GBP/USDT","XAU:USDT","BTTC/USDT",
}

# Actifs prioritaires (confirmés rentables par nos backtests)
WHITELIST_PRIORITY = ["XRP/USDT", "ETH/USDT", "ADA/USDT", "DOGE/USDT"]


def get_top_volatile_pairs(top_n: int = 4, min_volume_usdt: float = 5_000_000) -> list:
    """
    Retourne les N paires USDT les plus volatiles (ATR% moyen 24h)
    avec un volume minimum de 5M USDT.
    """
    exc = ccxt.binance({"enableRateLimit": True})
    print(f"\n  📡 Scan Binance — top {top_n} actifs volatils...\n")

    try:
        tickers = exc.fetch_tickers()
    except Exception as e:
        print(f"  ❌ Erreur Binance: {e}")
        return WHITELIST_PRIORITY[:top_n]

    candidates = []
    for symbol, t in tickers.items():
        if not symbol.endswith("/USDT"):
            continue
        if symbol in BLACKLIST:
            continue
        if t.get("quoteVolume", 0) < min_volume_usdt:
            continue
        # Volatilité approximée : (high - low) / last × 100
        high = t.get("high", 0)
        low  = t.get("low",  0)
        last = t.get("last", 1) or 1
        vol_pct = (high - low) / last * 100 if last > 0 else 0
        vol_24h = t.get("quoteVolume", 0)

        candidates.append({
            "symbol":   symbol,
            "vol_pct":  vol_pct,
            "vol_24h":  vol_24h / 1e6,
        })

    # Trie par volatilité décroissante
    candidates.sort(key=lambda x: x["vol_pct"], reverse=True)

    print(f"  {'Rang':<5} {'Symbole':<14} {'Volatilité':<12} {'Vol 24h'}")
    print(f"  {'-'*50}")
    selected = []
    for i, c in enumerate(candidates[:top_n * 3]):
        sym = c["symbol"]
        priority = "⭐" if sym in WHITELIST_PRIORITY else "  "
        in_top = len(selected) < top_n
        if in_top:
            selected.append(sym)
        mark = "✅" if in_top else "  "
        print(f"  {mark}{priority} #{i+1:<3} {sym:<14} {c['vol_pct']:>7.2f}%     {c['vol_24h']:.0f}M $")
        if i > top_n * 2:
            break

    # Si pas assez → complète avec la whitelist prioritaire
    for p in WHITELIST_PRIORITY:
        if len(selected) >= top_n:
            break
        if p not in selected:
            selected.append(p)

    print(f"\n  🎯 Top {top_n} sélectionnés : {selected}")
    return selected[:top_n]


def update_config(pairs: list):
    """Met à jour dynamiquement config.py avec les nouvelles paires."""
    config_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(config_path) as f:
        content = f.read()

    import re
    new_symbols = "SYMBOLS = [\n"
    for p in pairs:
        new_symbols += f'    "{p}",\n'
    new_symbols += "]"

    updated = re.sub(
        r'SYMBOLS\s*=\s*\[[^\]]*\]',
        new_symbols,
        content,
        flags=re.DOTALL
    )

    with open(config_path, "w") as f:
        f.write(updated)
    print(f"\n  💾 config.py mis à jour avec {pairs}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top",    type=int, default=4)
    parser.add_argument("--update", action="store_true", help="Met à jour config.py")
    parser.add_argument("--min-vol", type=float, default=5_000_000)
    args = parser.parse_args()

    print(f"\n⚡ Dynamic Pairlist — AlphaTrader\n")
    pairs = get_top_volatile_pairs(args.top, args.min_vol)

    if args.update:
        update_config(pairs)
        print(f"\n  ⚠️  Relancer le bot pour appliquer les changements.")
    else:
        print(f"\n  💡 Ajoute --update pour appliquer automatiquement.")
    print()
