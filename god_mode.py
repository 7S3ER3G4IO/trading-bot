"""
god_mode.py — ⚡ GOD MODE RULES LOADER
Charge et fusionne les résultats de 4 phases de R&D :
  - optimized_rules.json  (Alpha Factory  : 26 actifs BK/TF/MR)
  - black_ops_rules.json  (Black Ops      : 9 actifs M51 Stat Extremes)
  - lazarus_rules.json    (Lazarus Lab    : 5 actifs M52 ML + 2 paires M53)

Exporte :
  GOD_MODE_RULES : dict {symbol: {engine, strat, tf, threshold, rr, ...}}
  HARD_BAN       : set des 8 actifs définitivement bannis
  apply_god_mode(): override les ASSET_PROFILES au boot
"""

import os, json
from loguru import logger

_DIR = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════════════════════
# HARD BAN — Zero compute, zero log, total rejection
# ═══════════════════════════════════════════════════════════════════════════════
# V1 ULTIMATE: HARD_BAN cleared — all 10 elite assets are backtest-proven winners.
# Previously banned: GBPUSD, AUDUSD, EURGBP etc — but now we only trade the 10 elite.
# Any non-elite asset is simply not in CAPITAL_INSTRUMENTS, so no ban needed.
HARD_BAN = frozenset()

# ═══════════════════════════════════════════════════════════════════════════════
# JSON LOADING
# ═══════════════════════════════════════════════════════════════════════════════
def _load_json(filename: str) -> dict:
    path = os.path.join(_DIR, filename)
    if not os.path.exists(path):
        logger.warning(f"⚠️ GOD_MODE: {filename} not found at {path}")
        return {}
    with open(path) as f:
        return json.load(f)


def _merge_rules() -> dict:
    """Fusionne les 3 fichiers JSON en un dico unifié {symbol: config}."""
    rules = {}

    # Phase 1: Alpha Factory (26 actifs — BK/TF/MR classiques)
    alpha = _load_json("optimized_rules.json")
    for sym, cfg in alpha.items():
        rules[sym] = {
            "source": "alpha_factory",
            "engine": cfg.get("strat", "BK"),
            "strat":  cfg.get("strat", "BK"),
            "tf":     cfg.get("tf", "1h"),
            "threshold": cfg.get("threshold", 0.60),
            "rr":     cfg.get("rr", 1.5),
            "time_stop": cfg.get("time_stop", 48),
            "cat":    cfg.get("cat", "forex"),
            "pnl_backtest": cfg.get("pnl_net", 0),
        }

    # Phase 2: Black Ops (9 actifs — M51 Statistical Extremes)
    black = _load_json("black_ops_rules.json")
    for sym, cfg in black.items():
        rules[sym] = {
            "source": "black_ops",
            "engine": cfg.get("engine", "M51_STAT"),
            "strat":  "M51",
            "tf":     cfg.get("tf", "4h"),
            "z_threshold": cfg.get("z_threshold", 3.0),
            "z_window": cfg.get("z_window", 100),
            "cat":    _infer_cat(sym),
            "pnl_backtest": cfg.get("pnl_net", 0),
        }

    # Phase 3: Lazarus (5 actifs M52 ML + 2 paires M53)
    lazarus = _load_json("lazarus_rules.json")
    for sym, cfg in lazarus.items():
        engine = cfg.get("engine", "M52_ML")
        if engine == "M52_ML":
            rules[sym] = {
                "source": "lazarus",
                "engine": "M52_ML",
                "strat":  "ML",
                "tf":     cfg.get("tf", "1d"),
                "n_est":  cfg.get("n_est", 50),
                "depth":  cfg.get("depth", 3),
                "horizon": cfg.get("horizon", 5),
                "cat":    _infer_cat(sym),
                "pnl_backtest": cfg.get("pnl_net", 0),
            }
        elif engine == "M53_PAIRS":
            rules[sym] = {
                "source": "lazarus",
                "engine": "M53_PAIRS",
                "strat":  "PAIRS",
                "tf":     cfg.get("tf", "1d"),
                "pair":   cfg.get("pair", ""),
                "sym_a":  cfg.get("sym_a", ""),
                "sym_b":  cfg.get("sym_b", ""),
                "z_threshold": cfg.get("z_threshold", 2.0),
                "z_window": cfg.get("z_window", 50),
                "cat":    "forex",
                "pnl_backtest": cfg.get("pnl_net", 0),
            }

    return rules


def _infer_cat(sym: str) -> str:
    """Infer asset category from symbol name."""
    sym_up = sym.upper()
    if sym_up in ("US500", "US100", "US30", "DE40", "FR40", "UK100", "J225", "AU200"):
        return "indices"
    if sym_up in ("COPPER", "GOLD", "SILVER", "OIL_CRUDE", "OIL_BRENT", "NATURALGAS"):
        return "commodities"
    if sym_up in ("BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD", "AVAXUSD", "BNBUSD"):
        return "crypto"
    if sym_up in ("AAPL", "TSLA", "NVDA", "MSFT", "META", "GOOGL", "AMZN", "AMD"):
        return "stocks"
    return "forex"


# Build rules at import time
GOD_MODE_RULES = _merge_rules()


# ═══════════════════════════════════════════════════════════════════════════════
# ASSET_PROFILES OVERRIDE
# ═══════════════════════════════════════════════════════════════════════════════
def apply_god_mode():
    """
    Override ASSET_PROFILES with research-proven parameters.
    Called at boot by capital_client.py.
    
    For BK/TF/MR assets: override strat, tf, threshold-derived params.
    For M51/ML/PAIRS: keep the existing profile but tag with engine metadata.
    Also removes HARD_BAN assets from CAPITAL_INSTRUMENTS.
    """
    from brokers.capital_client import ASSET_PROFILES, CAPITAL_INSTRUMENTS

    overridden = 0
    banned = 0

    # Remove HARD_BAN assets from instrument list
    to_remove = [instr for instr in CAPITAL_INSTRUMENTS if instr in HARD_BAN]
    for instr in to_remove:
        CAPITAL_INSTRUMENTS.remove(instr)
        banned += 1

    # Override profiles with optimized params
    for sym, rule in GOD_MODE_RULES.items():
        if sym.startswith("PAIR_"):
            continue  # Pairs are virtual, not real instruments
        if sym not in ASSET_PROFILES:
            continue  # Not in broker instrument list

        profile = ASSET_PROFILES[sym]
        strat = rule.get("strat", profile.get("strat", "BK"))
        engine = rule.get("engine", strat)

        if strat in ("BK", "TF", "MR"):
            # Classic strategies: override strat, tf, and R:R params
            profile["strat"] = strat
            profile["tf"] = rule.get("tf", profile["tf"])
            if "rr" in rule:
                rr = rule["rr"]
                profile["tp1"] = rr
                profile["tp2"] = rr * 2.0
                profile["tp3"] = rr * 3.0
            if "time_stop" in rule:
                profile["max_hold"] = rule["time_stop"]
            if "threshold" in rule:
                profile["god_threshold"] = rule["threshold"]

        elif strat == "M51":
            # Statistical Extremes: tag the profile
            profile["strat"] = "MR"  # Routes via MR pathway (similar entry logic)
            profile["tf"] = rule.get("tf", "4h")
            profile["god_engine"] = "M51_STAT"
            profile["z_threshold"] = rule.get("z_threshold", 3.0)
            profile["z_window"] = rule.get("z_window", 100)

        elif strat == "ML":
            # RandomForest ML: tag the profile
            profile["strat"] = "TF"  # Routes via TF pathway
            profile["tf"] = rule.get("tf", "1d")
            profile["god_engine"] = "M52_ML"
            profile["n_est"] = rule.get("n_est", 50)
            profile["depth"] = rule.get("depth", 3)
            profile["horizon"] = rule.get("horizon", 5)

        profile["god_mode"] = True
        profile["god_source"] = rule.get("source", "unknown")
        profile["god_pnl"] = rule.get("pnl_backtest", 0)
        overridden += 1

    logger.info(
        f"⚡ GOD MODE ACTIVATED | {overridden} profiles overridden | "
        f"{banned} instruments HARD BANNED | "
        f"{len(CAPITAL_INSTRUMENTS)} active instruments"
    )

    return GOD_MODE_RULES
