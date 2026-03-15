"""
brokers/capital_client.py — Client Capital.com REST API.

Instruments supportés (CFDs) :
  EURUSD     → EUR/USD
  GBPUSD     → GBP/USD
  USDJPY     → USD/JPY
  US500      → S&P 500
  US100      → NASDAQ 100
  DE40       → DAX 40
  GOLD       → Or (XAU/USD)
  OIL_BRENT  → Brent Crude

Authentification :
  1. POST /session avec X-CAP-API-KEY + email + password
  2. Réponse contient CST + X-SECURITY-TOKEN
  3. Ces 2 tokens sont utilisés pour toutes les requêtes suivantes

Variables d'environnement :
  CAPITAL_API_KEY  → clé API générée dans les paramètres du compte
  CAPITAL_EMAIL    → email du compte Capital.com
  CAPITAL_PASSWORD → mot de passe du compte Capital.com
  CAPITAL_DEMO     → "true" pour le compte démo (défaut), "false" pour live
"""
import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from loguru import logger

# ─── Configuration ────────────────────────────────────────────────────────────
CAPITAL_API_KEY  = os.getenv("CAPITAL_API_KEY",  "")
CAPITAL_EMAIL    = os.getenv("CAPITAL_EMAIL",    "")
CAPITAL_PASSWORD = os.getenv("CAPITAL_PASSWORD", "")
CAPITAL_DEMO     = os.getenv("CAPITAL_DEMO", "true").lower() == "true"

# URLs officielles Capital.com (vérifiées DNS — domaine backend-capital.com avec tiret)
DEMO_URL = "https://demo-api-capital.backend-capital.com/api/v1"  # résout : 52.210.147.137 ✅
LIVE_URL = "https://api-capital.backend-capital.com/api/v1"       # résout : 45.60.76.121 ✅
# URL documentation (fallback Cloudflare CDN si les deux échouent)
OPEN_URL  = "https://open-api.capital.com/api/v1"
BASE_URL  = DEMO_URL if CAPITAL_DEMO else LIVE_URL

# Résolution timeframe → format Capital.com
TF_MAP = {
    "1m":  "MINUTE",
    "5m":  "MINUTE_5",
    "15m": "MINUTE_15",
    "30m": "MINUTE_30",
    "1h":  "HOUR",
    "4h":  "HOUR_4",
    "1d":  "DAY",
}

# ═══════════════════════════════════════════════════════════════════════════════
# NEMESIS V8 — 50 actifs | 3 stratégies | 1H haute fréquence + Daily MR
# 1H: 41 actifs volatils (BK+TF+MR)  |  Daily: 9 forex MR low-vol
# Objectif: 30+ trades/jour
# ═══════════════════════════════════════════════════════════════════════════════
CAPITAL_INSTRUMENTS = [
    # ── PROP FIRM ELITE 10 — validé 5/5 seeds, risque 0.35%, BK 1H ──
    # Sélection depuis prop_firm_backtest.py — challenge PASSED sur tous les seeds
    "GOLD",     # Commodité — wr=50%, rr=1.0, top performer BK
    "SILVER",   # Commodité — wr=69%, rr=1.0, meilleur win_rate
    "J225",     # Indice    — wr=60%, rr=1.0
    "EURJPY",   # Forex     — wr=62%, rr=2.0
    "DE40",     # Indice    — wr=62%, rr=1.0
    "UK100",    # Indice    — wr=67%, rr=1.5
    "AU200",    # Indice    — wr=67%, rr=1.0
    "BTCUSD",   # Crypto    — wr=54%, rr=1.0
    "GBPJPY",   # Forex     — wr=54%, rr=2.0
    "TSLA",     # Action    — wr=57%, rr=1.0
]

INSTRUMENT_NAMES = {
    "EURUSD":"EUR/USD","USDJPY":"USD/JPY","GBPJPY":"GBP/JPY","EURJPY":"EUR/JPY",
    "GBPUSD":"GBP/USD","USDCHF":"USD/CHF","AUDNZD":"AUD/NZD","AUDJPY":"AUD/JPY",
    "NZDJPY":"NZD/JPY","EURCHF":"EUR/CHF","CHFJPY":"CHF/JPY",
    "GOLD":"Gold","SILVER":"Silver","OIL_CRUDE":"WTI Crude","OIL_BRENT":"Brent",
    "COPPER":"Copper","NATURALGAS":"Nat Gas",
    "US500":"S&P 500","US100":"NASDAQ","US30":"Dow Jones","DE40":"DAX 40",
    "FR40":"CAC 40","UK100":"FTSE 100","J225":"Nikkei","AU200":"ASX 200",
    "BTCUSD":"BTC/USD","ETHUSD":"ETH/USD",
    "BNBUSD":"BNB/USD","XRPUSD":"XRP/USD","SOLUSD":"SOL/USD","AVAXUSD":"AVAX/USD",
    "AAPL":"Apple","TSLA":"Tesla","NVDA":"Nvidia","MSFT":"Microsoft",
    "META":"Meta","GOOGL":"Google","AMZN":"Amazon","AMD":"AMD",
    "AUDUSD":"AUD/USD","NZDUSD":"NZD/USD","EURGBP":"EUR/GBP","EURAUD":"EUR/AUD",
    "GBPAUD":"GBP/AUD","AUDCAD":"AUD/CAD","GBPCAD":"GBP/CAD","GBPCHF":"GBP/CHF",
    "CADCHF":"CAD/CHF",
}

PIP_FACTOR = {
    "EURUSD":0.0001,"USDJPY":0.01,"GBPJPY":0.01,"EURJPY":0.01,
    "GBPUSD":0.0001,"USDCHF":0.0001,"AUDNZD":0.0001,"AUDJPY":0.01,
    "NZDJPY":0.01,"EURCHF":0.0001,"CHFJPY":0.01,
    "GOLD":0.01,"SILVER":0.001,"OIL_CRUDE":0.01,"OIL_BRENT":0.01,
    "COPPER":0.0001,"NATURALGAS":0.001,
    "US500":0.1,"US100":0.1,"US30":1.0,"DE40":1.0,
    "FR40":1.0,"UK100":1.0,"J225":1.0,"AU200":1.0,
    "BTCUSD":0.01,"ETHUSD":0.01,
    "BNBUSD":0.01,"XRPUSD":0.0001,"SOLUSD":0.01,"AVAXUSD":0.01,
    "AAPL":0.01,"TSLA":0.01,"NVDA":0.01,"MSFT":0.01,"META":0.01,"GOOGL":0.01,
    "AMZN":0.01,"AMD":0.01,
    "AUDUSD":0.0001,"NZDUSD":0.0001,"EURGBP":0.0001,"EURAUD":0.0001,
    "GBPAUD":0.0001,"AUDCAD":0.0001,"GBPCAD":0.0001,"GBPCHF":0.0001,
    "CADCHF":0.0001,
}

MIN_SIZE = {
    "GOLD":0.01,"SILVER":1,"COPPER":1,"OIL_CRUDE":0.1,"OIL_BRENT":0.1,"NATURALGAS":0.1,
    "US500":0.1,"US100":0.1,"US30":0.1,"DE40":0.1,"FR40":0.1,
    "UK100":0.1,"J225":1,"AU200":0.1,
    "BTCUSD":0.001,"ETHUSD":0.01,
    "AAPL":1,"TSLA":1,"NVDA":1,"MSFT":1,"META":1,"GOOGL":1,"AMZN":1,"AMD":1,
    "BNBUSD":0.01,"XRPUSD":1,"SOLUSD":0.1,"AVAXUSD":1,
}

# ─── R-1: Decimal precision per instrument (auto-derived from PIP_FACTOR) ────
# Used to round SL/TP to the correct number of decimals for Capital.com API.
import math as _math
PRICE_DECIMALS = {k: max(0, int(round(-_math.log10(v)))) for k, v in PIP_FACTOR.items()}

# ═══════════════════════════════════════════════════════════════════════════════
# ASSET_PROFILES — V8 Haute Fréquence
#   strat: "BK" (Breakout) | "MR" (Mean Reversion) | "TF" (Trend Following)
#   tf:    "1h" (V8) | "1d" (Daily MR)
#   range_lb: bougies lookback pour BK (4 = 4h en 1H)
#   bk_margin: % du range pour valider breakout (0.03 = 3% — sensible pour 1H)
#   tp1/tp2/tp3: en multiples ATR (1.5x / 3.0x / 5.0x pour tous les instruments)
#   sl_buffer: multiplicateur ATR pour SL (MR/TF) ou % du range pour BK
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Asset Class Classification ───────────────────────────────────────────────
# Maps cat → asset_class (CRYPTO = 24/7 markets | TRADFI = weekday-only markets)
ASSET_CLASS_MAP = {
    "crypto":      "CRYPTO",
    "forex":       "TRADFI",
    "forex_mr":    "TRADFI",
    "indices":     "TRADFI",
    "commodities": "TRADFI",
    "stocks":      "TRADFI",
}

# Per-class risk parameters
RISK_BY_CLASS = {
    "CRYPTO": {"time_stop_h": 48, "rr_min": 1.5},
    "TRADFI": {"time_stop_h": 24, "rr_min": 1.2},
}

# Friday Kill-Switch: TRADFI positions closed at Friday 20:50 UTC
FRIDAY_KILLSWITCH_HOUR = 20
FRIDAY_KILLSWITCH_MINUTE = 50

def get_asset_class(instrument: str) -> str:
    """Returns 'CRYPTO' or 'TRADFI' for a given instrument."""
    profile = ASSET_PROFILES.get(instrument, {})
    cat = profile.get("cat", "forex")
    return ASSET_CLASS_MAP.get(cat, "TRADFI")

def get_risk_params(instrument: str) -> dict:
    """Returns {time_stop_h, rr_min} for a given instrument."""
    cls = get_asset_class(instrument)
    return RISK_BY_CLASS.get(cls, RISK_BY_CLASS["TRADFI"])

ASSET_PROFILES = {
    # ═══ V1 ULTIMATE: All 10 Elite → BK (Breakout) × 1H × TP 2.5R ═══
    # tp1 = 2.5 (single TP, Multi-TP managed by bot_monitor)
    # sl_buffer = 0.10 (10% of range for BK SL placement)
    "EURJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "AUDNZD":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "ETHUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "GOLD":    {"strat":"BK","tf":"1h","cat":"commodities","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "GBPUSD":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "GBPJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "SILVER":  {"strat":"BK","tf":"1h","cat":"commodities","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "EURUSD":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "AUDUSD":  {"strat":"BK","tf":"1h","cat":"forex","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
    "DE40":    {"strat":"BK","tf":"1h","cat":"indices","tp1":2.5,"tp2":2.5,"tp3":2.5,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":6},
}

# ═══════════════════════════════════════════════════════════════════════════════
# S-3: Micro-Timeframe Profiles (5m MR + 15m BK)
# These are scanned ADDITIONALLY to the main 1H TF on the most liquid instruments.
# Key: instrument name with _5M or _15M suffix (for tracking/dedup only)
# The actual API instrument name is the same (e.g., EURUSD).
# ═══════════════════════════════════════════════════════════════════════════════
MICRO_TF_PROFILES = {
    # 5m Mean Reversion — ultra-liquid, tight spreads
    "EURUSD_5M":  {"epic":"EURUSD","strat":"MR","tf":"5m","cat":"forex_mr","tp1":1.0,"tp2":1.5,"tp3":2.0,"sl_buffer":0.8,"max_hold":60,"adx_min":8,"rsi_lo":25,"rsi_hi":75,"range_lb":12,"bk_margin":0.02,"max_per_hour":3},
    "GOLD_5M":    {"epic":"GOLD",  "strat":"MR","tf":"5m","cat":"commodities","tp1":1.0,"tp2":1.5,"tp3":2.0,"sl_buffer":0.8,"max_hold":60,"adx_min":8,"rsi_lo":25,"rsi_hi":75,"range_lb":12,"bk_margin":0.02,"max_per_hour":3},
    "BTCUSD_5M":  {"epic":"BTCUSD","strat":"MR","tf":"5m","cat":"crypto","tp1":1.0,"tp2":1.5,"tp3":2.0,"sl_buffer":0.8,"max_hold":60,"adx_min":8,"rsi_lo":25,"rsi_hi":75,"range_lb":12,"bk_margin":0.02,"max_per_hour":3},
    "US500_5M":   {"epic":"US500", "strat":"MR","tf":"5m","cat":"indices","tp1":1.0,"tp2":1.5,"tp3":2.0,"sl_buffer":0.8,"max_hold":60,"adx_min":8,"rsi_lo":25,"rsi_hi":75,"range_lb":12,"bk_margin":0.02,"max_per_hour":3},
    # 15m Breakout — indices with tighter ranges
    "US500_15M":  {"epic":"US500", "strat":"BK","tf":"15m","cat":"indices","tp1":1.2,"tp2":2.0,"tp3":3.0,"sl_buffer":0.10,"max_hold":120,"adx_min":10,"bk_margin":0.02,"range_lb":8,"max_per_hour":2},
    "US100_15M":  {"epic":"US100", "strat":"BK","tf":"15m","cat":"indices","tp1":1.2,"tp2":2.0,"tp3":3.0,"sl_buffer":0.10,"max_hold":120,"adx_min":10,"bk_margin":0.02,"range_lb":8,"max_per_hour":2},
    "DE40_15M":   {"epic":"DE40",  "strat":"BK","tf":"15m","cat":"indices","tp1":1.2,"tp2":2.0,"tp3":3.0,"sl_buffer":0.10,"max_hold":120,"adx_min":10,"bk_margin":0.02,"range_lb":8,"max_per_hour":2},
    "GBPUSD_5M":  {"epic":"GBPUSD","strat":"MR","tf":"5m","cat":"forex_mr","tp1":1.0,"tp2":1.5,"tp3":2.0,"sl_buffer":0.8,"max_hold":60,"adx_min":8,"rsi_lo":25,"rsi_hi":75,"range_lb":12,"bk_margin":0.02,"max_per_hour":3},
}

# ═══════════════════════════════════════════════════════════════════════════════
# GOD MODE — Override ASSET_PROFILES with research-proven strategies
# ═══════════════════════════════════════════════════════════════════════════════
try:
    from god_mode import apply_god_mode, HARD_BAN
    apply_god_mode()
    # Remove HARD_BAN micro-TF entries too
    _micro_ban = [k for k in MICRO_TF_PROFILES if MICRO_TF_PROFILES[k].get("epic") in HARD_BAN]
    for k in _micro_ban:
        del MICRO_TF_PROFILES[k]
except Exception as _gm_e:
    import traceback
    print(f"⚠️ GOD MODE init skipped: {_gm_e}")
    traceback.print_exc()


class CapitalClient:
    """Client Capital.com — authentification par session + ordres CFD."""

    SESSION_TTL = 9 * 60  # Renouvelle 1 min avant l'expiration réelle (10 min Capital.com)

    def __init__(self):
        self._cst      = None
        self._token    = None
        self._auth_ts  = 0
        self._session  = requests.Session()
        self._base_url = BASE_URL  # mis à jour dans _authenticate si fallback utilisé

        if not CAPITAL_API_KEY or not CAPITAL_EMAIL or not CAPITAL_PASSWORD:
            logger.debug("ℹ️  CAPITAL_API_KEY / EMAIL / PASSWORD manquants — broker désactivé")
            self._ok = False
            return

        self._ok = self._authenticate()

    def _authenticate(self) -> bool:
        """
        Auth Capital.com avec back-off exponentiel.
        Pour DEMO : réessaie l'URL DEMO 3× avant de tenter le fallback.
        Le fallback LIVE pour un compte DEMO retourne 0€ → éviter autant que possible.
        """
        env   = "DEMO" if CAPITAL_DEMO else "LIVE"
        # DEMO ISOLATION : en mode DEMO, on ne tente JAMAIS l'URL LIVE
        # Le fallback LIVE retourne 0€ de balance et pourrait en théorie
        # ouvrir des positions sur de l'argent réel.
        primary  = DEMO_URL if CAPITAL_DEMO else LIVE_URL

        if CAPITAL_DEMO:
            # DEMO only : 3 tentatives DEMO avec back-off, puis open-api (CDN neutre)
            urls_with_tries = [
                (primary,  3),   # 3 essais DEMO avec back-off 3s/6s/9s
                (OPEN_URL, 1),   # open-api CDN Cloudflare — compte DEMO y est accessible
            ]
        else:
            # LIVE only : 3 tentatives LIVE + fallback open-api
            urls_with_tries = [
                (primary,  3),
                (OPEN_URL, 1),
            ]

        for url, max_tries in urls_with_tries:
            for attempt in range(1, max_tries + 1):
                try:
                    r = self._session.post(
                        f"{url}/session",
                        headers={"X-CAP-API-KEY": CAPITAL_API_KEY},
                        json={"identifier": CAPITAL_EMAIL, "password": CAPITAL_PASSWORD,
                              "encryptedPassword": False},
                        timeout=15,
                    )
                    r.raise_for_status()
                    self._cst      = r.headers.get("CST")
                    self._token    = r.headers.get("X-SECURITY-TOKEN")
                    self._auth_ts  = time.time()
                    self._base_url = url
                    tag = "" if url == primary else " (open-api fallback)"
                    logger.info(f"🏦 Capital.com connecté ({env}){tag} ✅ — {url}")
                    return bool(self._cst and self._token)
                except Exception as e:
                    if "429" in str(e):
                        backoff = attempt * 3  # 3s, 6s, 9s
                        logger.warning(
                            f"⚠️  Capital.com 429 sur {url} (tentative {attempt}/{max_tries})"
                            f" — nouveau essai dans {backoff}s"
                        )
                        time.sleep(backoff)
                    else:
                        logger.warning(f"⚠️  Capital.com auth échoué sur {url}: {type(e).__name__}: {e}")
                        break  # Erreur non-429 → passer à l'URL suivante directement

        logger.error("❌ Capital.com : toutes les URLs ont échoué (DEMO + LIVE + open-api)")
        return False


    def _headers(self) -> dict:
        """Retourne les headers d'authentification, renouvelle si nécessaire."""
        if time.time() - self._auth_ts > self.SESSION_TTL:
            ok = self._authenticate()
            if not ok:
                logger.error("❌ Capital.com : renouvellement session échoué — tokens potentiellement expirés")
        return {
            "X-CAP-API-KEY":    CAPITAL_API_KEY,
            "CST":              self._cst or "",
            "X-SECURITY-TOKEN": self._token or "",
            "Content-Type":     "application/json",
        }

    @property
    def available(self) -> bool:
        return self._ok

    # ─── Recherche de marchés (debug) ────────────────────────────────────────

    def search_markets(self, term: str, limit: int = 5) -> list:
        """Recherche un marché par nom/epic sur Capital.com API."""
        if not self.available:
            return []
        try:
            r = self._session.get(
                f"{self._base_url}/markets",
                headers=self._headers(),
                params={"searchTerm": term, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            return [
                {"epic": m.get("epic", ""), "name": m.get("instrumentName", "")}
                for m in r.json().get("markets", [])
            ]
        except Exception as e:
            logger.debug(f"search_markets({term}): {e}")
            return []

    def validate_epics(self):
        """Vérifie au démarrage que chaque epic de CAPITAL_INSTRUMENTS est valide."""
        if not self.available:
            return
        bad = []
        for epic in CAPITAL_INSTRUMENTS:
            try:
                r = self._session.get(
                    f"{self._base_url}/prices/{epic}",
                    headers=self._headers(),
                    params={"resolution": "HOUR", "max": 1, "pageSize": 1},
                    timeout=10,
                )
                if r.status_code == 404:
                    bad.append(epic)
                    # Chercher le bon nom
                    search_term = epic.replace("_", " ")
                    results = self.search_markets(search_term, limit=3)
                    suggestions = ", ".join([f"{r['epic']} ({r['name']})" for r in results])
                    logger.warning(
                        f"⚠️  Epic {epic} INVALIDE (404) — suggestions: {suggestions or 'aucune'}"
                    )
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"validate_epics {epic}: {e}")
        if bad:
            logger.error(f"❌ {len(bad)} epics invalides: {bad}")
        else:
            logger.info("✅ Tous les 39 epics validés sur Capital.com")

    # ─── Données de marché ────────────────────────────────────────────────────

    # ─── Rate limiter state (class-level, shared across instances) ───────────
    _rl_lock = __import__("threading").Lock()
    _rl_last_call = 0.0
    _rl_min_interval = 0.12  # 8 req/s max (Capital.com limit: ~10 req/s)

    def _rate_limit(self):
        """Global rate limiter: enforces minimum interval between API calls."""
        with CapitalClient._rl_lock:
            now = time.time()
            wait = CapitalClient._rl_min_interval - (now - CapitalClient._rl_last_call)
            if wait > 0:
                time.sleep(wait)
            CapitalClient._rl_last_call = time.time()

    def fetch_ohlcv(self, epic: str, timeframe: str = "5m", count: int = 300) -> Optional[pd.DataFrame]:
        """Télécharge les bougies OHLCV depuis Capital.com. Rate-limited + 429 retry."""
        if not self.available:
            return None

        gran = TF_MAP.get(timeframe, "MINUTE_5")

        for attempt in range(3):
            try:
                self._rate_limit()  # enforce global rate limit
                r = self._session.get(
                    f"{self._base_url}/prices/{epic}",
                    headers=self._headers(),
                    params={"resolution": gran, "max": count, "pageSize": count},
                    timeout=15,
                )

                # Handle 429 explicitly
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 5 * (2 ** attempt)))
                    logger.warning(f"⚠️ Capital 429 {epic} — wait {retry_after}s (attempt {attempt+1}/3)")
                    time.sleep(retry_after)
                    continue

                r.raise_for_status()
                prices = r.json().get("prices", [])
                if not prices:
                    return None

                records = []
                for p in prices:
                    mid_open  = (p["openPrice"]["bid"]  + p["openPrice"]["ask"])  / 2
                    mid_high  = (p["highPrice"]["bid"]  + p["highPrice"]["ask"])  / 2
                    mid_low   = (p["lowPrice"]["bid"]   + p["lowPrice"]["ask"])   / 2
                    mid_close = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2
                    records.append({
                        "timestamp": pd.Timestamp(p["snapshotTimeUTC"]).tz_localize("UTC"),
                        "open":   mid_open,
                        "high":   mid_high,
                        "low":    mid_low,
                        "close":  mid_close,
                        "volume": float(p.get("lastTradedVolume", 0)),
                    })

                df = pd.DataFrame(records)
                df.set_index("timestamp", inplace=True)
                df.sort_index(inplace=True)
                return df

            except Exception as e:
                if "429" in str(e):
                    wait_s = 5 * (2 ** attempt)  # 5s, 10s, 20s
                    logger.warning(f"⚠️ Capital 429 {epic} — wait {wait_s}s (attempt {attempt+1}/3)")
                    time.sleep(wait_s)
                    continue
                logger.error(f"❌ Capital.com fetch_ohlcv {epic}: {e}")
                return None

        logger.error(f"❌ Capital.com fetch_ohlcv {epic}: max retries exceeded (429)")
        return None


    def get_balance(self) -> float:
        """Retourne la valeur totale du compte (equity, pas margin disponible)."""
        if not self.available:
            return 0.0
        try:
            r = self._session.get(f"{self._base_url}/accounts", headers=self._headers(), timeout=10)
            r.raise_for_status()
            accounts = r.json().get("accounts", [])
            for acc in accounts:
                if acc.get("preferred"):
                    # Use 'balance' (total equity) NOT 'available' (equity - margin)
                    bal = acc["balance"]
                    return float(bal.get("balance", bal.get("available", 0)))
            if accounts:
                bal = accounts[0]["balance"]
                return float(bal.get("balance", bal.get("available", 0)))
            return 0.0
        except Exception as e:
            logger.error(f"❌ Capital.com get_balance: {e}")
            return 0.0

    def get_current_price(self, epic: str) -> Optional[dict]:
        """Retourne le bid/ask actuel d'un instrument. Retry on 429."""
        if not self.available:
            return None
        for attempt in range(3):
            try:
                self._rate_limit()
                r = self._session.get(
                    f"{self._base_url}/markets/{epic}",
                    headers=self._headers(),
                    timeout=10,
                )
                if r.status_code == 429:
                    wait = 3 * (attempt + 1)  # 3s, 6s, 9s
                    logger.debug(f"⏳ get_price {epic} 429 — retry in {wait}s ({attempt+1}/3)")
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                snap = data.get("snapshot", {})
                return {
                    "bid": float(snap.get("bid", 0)),
                    "ask": float(snap.get("offer", 0)),
                    "mid": (float(snap.get("bid", 0)) + float(snap.get("offer", 0))) / 2,
                }
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                logger.error(f"❌ Capital.com get_price {epic}: {e}")
                return None
        logger.warning(f"⚠️ get_price {epic}: 429 persistant après 3 retries")
        return None


    # ─── E-3: Request with Exponential Backoff ─────────────────────────────

    def _request_safe(self, method: str, url: str, retries: int = 4,
                      rl_priority: int = 2, **kwargs):
        """
        E-3: Requête HTTP avec exponential backoff + Retry-After header.
        Backoff: 0.5s → 1s → 2s → 4s (cap).
        Rate-Limit Guardian: acquire() avant chaque tentative selon priorité.
        """
        from rate_limiter import get_rate_limiter, Priority as RLP
        _rl = get_rate_limiter()
        _pri = RLP(rl_priority) if isinstance(rl_priority, int) else rl_priority

        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("headers", self._headers())
        for attempt in range(retries):
            try:
                _rl.acquire(_pri)   # Rate-Limit Guardian
                r = getattr(self._session, method)(url, **kwargs)
                if r.status_code == 429:
                    # Notifier le Rate-Limit Guardian
                    retry_after = r.headers.get("Retry-After")
                    wait = min(float(retry_after), 30) if retry_after else min(0.5 * (2 ** attempt), 10)
                    _rl.on_429(retry_after=int(wait))
                    logger.warning(f"⏳ 429 Rate-limited — wait {wait:.1f}s (attempt {attempt+1})")
                    time.sleep(wait)
                    continue
                return r
            except Exception as e:
                wait = min(0.5 * (2 ** attempt), 10)
                logger.debug(f"Request error attempt {attempt+1}/{retries}: {e} — wait {wait:.1f}s")
                time.sleep(wait)
        return None


    # ─── Ordres ──────────────────────────────────────────────────────────────

    def confirm_deal(self, deal_ref: str, retries: int = 4) -> Optional[str]:
        """
        E-3: Échange dealReference → dealId avec exponential backoff.
        """
        if not self.available or not deal_ref:
            return None
        for attempt in range(retries):
            try:
                r = self._request_safe(
                    "get", f"{self._base_url}/confirms/{deal_ref}", retries=1
                )
                if r is None:
                    time.sleep(min(0.5 * (2 ** attempt), 4))
                    continue
                r.raise_for_status()
                data     = r.json()
                deal_id  = data.get("dealId")
                status   = data.get("dealStatus", "")
                if status == "ACCEPTED" and deal_id:
                    logger.info(f"  ✅ Confirmé dealRef={deal_ref} → dealId={deal_id}")
                    return deal_id
                if status == "REJECTED":
                    reject_reason = data.get('rejectReason', data.get('reason', 'unknown'))
                    logger.warning(f"⚠️  Capital.com ordre rejeté : {reject_reason} | {data}")
                    return None
                logger.debug(f"  ⏳ Confirm attempt {attempt+1}/{retries} status={status!r}")
                time.sleep(min(0.5 * (2 ** attempt), 4))
            except Exception as e:
                logger.error(f"❌ confirm_deal attempt {attempt+1}: {e}")
                time.sleep(min(0.5 * (2 ** attempt), 4))
        logger.error(f"❌ confirm_deal épuisé après {retries} essais — dealRef={deal_ref}")
        return None

    def place_market_order(
        self,
        epic: str,
        direction: str,  # "BUY" ou "SELL"
        size: float,
        sl_price: float,
        tp_price: float,
    ) -> Optional[str]:
        """
        Passe un ordre marché Capital.com avec SL et TP.
        Retourne le dealId (ID permanent) ou None si erreur.
        Appelle confirm_deal() automatiquement pour échanger le dealReference.
        """
        if not self.available:
            return None

        # ⛔ GARDE-FOU DEMO ABSOLU : refuse d'exécuter si connecté sur l'URL LIVE en mode DEMO.
        # Dernier filet de sécurité — aucun argent réel ne peut être touché.
        if CAPITAL_DEMO and self._base_url == LIVE_URL:
            logger.error(
                "⛔ BLOCAGE SÉCURITÉ : tentative d'ordre sur URL LIVE en mode DEMO ! "
                f"({self._base_url}) — ordre annulé. Vérifier CAPITAL_DEMO=true."
            )
            return None

        try:
            data = {
                "epic":           epic,
                "direction":      direction,
                "size":           str(round(size, 2)),
                "orderType":      "MARKET",
                "stopLevel":      round(sl_price, 5),
                "profitLevel":    round(tp_price, 5),
                "guaranteedStop": False,
                "forceOpen":      True,
            }
            r = self._session.post(
                f"{self._base_url}/positions",
                headers=self._headers(),
                json=data,
                timeout=15,
            )
            if r.status_code >= 400:
                try:
                    err_body = r.json()
                except Exception:
                    err_body = r.text[:300]

                # Auto-retry with guaranteedStop=True if broker requires it
                err_code = err_body.get("errorCode", "") if isinstance(err_body, dict) else str(err_body)
                if "guaranteed-stop-loss" in str(err_code).lower():
                    logger.info(f"🔄 {epic}: guaranteed stop requis — retry avec guaranteedStop=True")
                    data["guaranteedStop"] = True
                    r = self._session.post(
                        f"{self._base_url}/positions",
                        headers=self._headers(),
                        json=data,
                        timeout=15,
                    )
                    if r.status_code >= 400:
                        try:
                            err_body2 = r.json()
                        except Exception:
                            err_body2 = r.text[:300]
                        err_code2 = err_body2.get("errorCode", "") if isinstance(err_body2, dict) else str(err_body2)

                        # If SL rejected for min value, parse minimum and retry with wider SL
                        import re
                        sl_match = re.search(r'stoploss\.minvalue:\s*([\d.]+)', str(err_code2))
                        if sl_match:
                            min_sl = float(sl_match.group(1))
                            logger.info(f"🔄 {epic}: SL minimum broker = {min_sl} — ajustement")
                            data["stopLevel"] = round(min_sl, 5)
                            r = self._session.post(
                                f"{self._base_url}/positions",
                                headers=self._headers(),
                                json=data,
                                timeout=15,
                            )
                            if r.status_code >= 400:
                                try:
                                    err_body3 = r.json()
                                except Exception:
                                    err_body3 = r.text[:300]
                                logger.error(
                                    f"❌ Capital.com place_order {epic} (SL adj): HTTP {r.status_code} | "
                                    f"Body: {err_body3}"
                                )
                                return None
                        else:
                            logger.error(
                                f"❌ Capital.com place_order {epic} (retry): HTTP {r.status_code} | "
                                f"Body: {err_body2} | Payload: {data}"
                            )
                            return None
                else:
                    logger.error(
                        f"❌ Capital.com place_order {epic}: HTTP {r.status_code} | "
                        f"Body: {err_body} | Payload: {data}"
                    )
                    return None
            r.raise_for_status()
            resp     = r.json()
            deal_ref = resp.get("dealReference")
            if not deal_ref:
                logger.error(f"❌ Capital.com place_order : pas de dealReference dans {resp}")
                return None

            # Échange dealReference → dealId (ID permanent requis pour PUT/DELETE)
            deal_id = self.confirm_deal(deal_ref)
            if deal_id:
                logger.info(f"✅ Capital.com {direction} {epic} size={size} | dealId={deal_id}")
            return deal_id

        except Exception as e:
            logger.error(f"❌ Capital.com place_order {epic}: {e}")
            return None

    # ─── E-1: Limit Order with MARKET Fallback ────────────────────────────

    def place_limit_order(
        self,
        epic: str,
        direction: str,
        size: float,
        sl_price: float,
        tp_price: float,
        limit_price: float = 0,
        timeout_s: float = 30,
    ) -> Optional[str]:
        """
        E-1: Place un LIMIT order agressif (mid ± 1 pip).
        Si pas fill en timeout_s secondes → fallback MARKET.
        
        Returns dealId ou None.
        """
        if not self.available:
            return None

        if CAPITAL_DEMO and self._base_url == LIVE_URL:
            logger.error("⛔ BLOCAGE SÉCURITÉ : limite sur URL LIVE en mode DEMO !")
            return None

        # Calcul du prix limit si non fourni
        if limit_price <= 0:
            px = self.get_current_price(epic)
            if not px:
                logger.warning(f"⚠️ E-1 {epic}: prix indisponible → fallback MARKET")
                return self.place_market_order(epic, direction, size, sl_price, tp_price)
            pip = PIP_FACTOR.get(epic, 0.0001)
            if direction == "BUY":
                limit_price = px["mid"] + pip  # Slightly above mid for fast fill
            else:
                limit_price = px["mid"] - pip

        dec = PRICE_DECIMALS.get(epic, 5)
        limit_price = round(limit_price, dec)

        try:
            data = {
                "epic":           epic,
                "direction":      direction,
                "size":           str(round(size, 2)),
                "orderType":      "LIMIT",
                "level":          limit_price,
                "stopLevel":      round(sl_price, dec),
                "profitLevel":    round(tp_price, dec),
                "guaranteedStop": False,
                "forceOpen":      True,
                "timeInForce":    "GOOD_TILL_CANCELLED",
            }
            logger.info(f"📋 E-1 LIMIT {direction} {epic} @ {limit_price} size={size}")

            r = self._request_safe("post", f"{self._base_url}/workingorders", json=data)
            if r is None or r.status_code >= 400:
                logger.warning(f"⚠️ E-1 LIMIT failed ({r.status_code if r else 'None'}) → fallback MARKET")
                return self.place_market_order(epic, direction, size, sl_price, tp_price)

            resp = r.json()
            deal_ref = resp.get("dealReference")
            if not deal_ref:
                logger.warning(f"⚠️ E-1 {epic}: no dealRef → fallback MARKET")
                return self.place_market_order(epic, direction, size, sl_price, tp_price)

            # Wait for fill with timeout
            start = time.time()
            while time.time() - start < timeout_s:
                deal_id = self.confirm_deal(deal_ref, retries=1)
                if deal_id:
                    logger.info(f"✅ E-1 LIMIT FILLED {epic} dealId={deal_id}")
                    return deal_id
                time.sleep(2)

            # Timeout → cancel + MARKET fallback
            logger.info(f"⏰ E-1 LIMIT timeout {timeout_s}s — cancel + MARKET fallback")
            try:
                self._request_safe("delete", f"{self._base_url}/workingorders/{deal_ref}")
            except Exception:
                pass
            return self.place_market_order(epic, direction, size, sl_price, tp_price)

        except Exception as e:
            logger.error(f"❌ E-1 place_limit_order {epic}: {e} → MARKET fallback")
            return self.place_market_order(epic, direction, size, sl_price, tp_price)

    def get_open_positions(self) -> List[dict]:
        """Retourne les positions ouvertes."""
        if not self.available:
            return []
        try:
            r = self._session.get(f"{self._base_url}/positions", headers=self._headers(), timeout=10)
            r.raise_for_status()
            return r.json().get("positions", [])
        except Exception as e:
            logger.error(f"❌ Capital.com get_positions: {e}")
            return []

    def close_position(self, deal_id: str) -> bool:
        """Ferme une position par son dealId."""
        if not self.available:
            return False
        try:
            r = self._session.delete(
                f"{self._base_url}/positions/{deal_id}",
                headers=self._headers(),
                timeout=15,
            )
            r.raise_for_status()
            logger.info(f"✅ Capital.com position {deal_id} fermée")
            return True
        except Exception as e:
            logger.error(f"❌ Capital.com close_position {deal_id}: {e}")
            return False

    def close_partial(self, epic: str, direction: str, partial_size: float) -> bool:
        """
        Ferme partiellement une position CFD en passant un ordre opposé.
        Capital.com matche automatiquement contre la position existante.

        Args:
            epic:         Symbole (ex: "GOLD")
            direction:    Direction de la position OUVERTE ("BUY" ou "SELL")
            partial_size: Taille à fermer (ex: 40% de la position totale)

        Returns:
            True si l'ordre opposé a été placé avec succès
        """
        if not self.available or partial_size <= 0:
            return False
        # Ordre opposé = close partiel
        close_direction = "SELL" if direction == "BUY" else "BUY"
        # Taille minimum
        min_sz = self.MIN_SIZE.get(epic, 0.01)
        if partial_size < min_sz:
            logger.warning(
                f"close_partial {epic}: taille {partial_size:.4f} < min {min_sz} — skip"
            )
            return False
        try:
            body = {
                "epic":          epic,
                "direction":     close_direction,
                "size":          str(round(partial_size, 2)),
                "type":          "MARKET",
                "guaranteedStop": False,
                "trailingStop":  False,
            }
            r = self._session.post(
                f"{self._base_url}/orders",
                headers=self._headers(),
                json=body,
                timeout=15,
            )
            r.raise_for_status()
            logger.info(
                f"✅ Capital.com partial close {epic} {close_direction} {partial_size:.4f} lots"
            )
            return True
        except Exception as e:
            logger.error(f"❌ Capital.com close_partial {epic}: {e}")
            return False


    def modify_position_stop(self, deal_id: str, new_stop: float) -> bool:
        """
        Déplace le Stop-Loss d'une position existante (pour le Break-Even).
        Capital.com PUT /positions/{dealId} requiert :
          - stopLevel : nouveau niveau de SL
          - guaranteedStop : false (doit être explicité)
          - trailingStop   : false (sinon le SL devient trailing)
        """
        if not self.available or not deal_id:
            return False
        try:
            r = self._session.put(
                f"{self._base_url}/positions/{deal_id}",
                headers=self._headers(),
                json={
                    "stopLevel":      round(new_stop, 5),
                    "guaranteedStop": False,
                    "trailingStop":   False,
                },
                timeout=15,
            )
            r.raise_for_status()
            logger.info(f"✅ Capital.com BE activé — {deal_id} → SL={new_stop:.5f}")
            return True
        except Exception as e:
            logger.error(f"❌ Capital.com modify_stop {deal_id}: {e}")
            return False

    # ─── Taille minimale par instrument (Capital.com) ─────────────────────────
    MIN_SIZE = {
        # Métaux précieux
        "GOLD":       0.10,   # XAUUSD — min 0.1 unit
        "SILVER":     1.00,   # XAGUSD
        # Pétrole
        "OIL_BRENT":  1.00,   # Brent Crude — min 1 unit
        "OIL_CRUDE":    1.00,   # WTI
        "OIL":        0.10,   # fallback legacy
        "NATURALGAS": 0.10,
        # Indices (minimum 1 contrat)
        "US500":      1.0,    # S&P 500
        "US100":      1.0,    # NASDAQ 100
        "US30":       1.0,    # Dow Jones 30
        "DE40":       1.0,    # DAX 40
        "FR40":       1.0,    # CAC 40
    }  # Forex (AUDJPY, GBPCHF, EURGBP, etc.) : 0.01 (par défaut)

    # ─── Calcul taille de position ────────────────────────────────────────────

    def position_size(
        self,
        balance: float,
        risk_pct: float,
        entry: float,
        sl: float,
        epic: str,
        free_margin: float = 0.0,
    ) -> float:
        """
        Calcule la taille de position en unités Capital.com.

        Pipeline de sécurité (3 étapes) :
          1. Raw size = risk_pct × balance / sl_distance
          2. Leverage cap: nominal ≤ MAX_EFFECTIVE_LEVERAGE × balance
          3. Margin check: marge requise ≤ free_margin (solde libre)

        Parameters
        ----------
        balance     : Solde total du compte
        risk_pct    : Fraction du capital à risquer (ex: 0.005 = 0.5%)
        entry       : Prix d'entrée
        sl          : Prix du Stop Loss
        epic        : Nom de l'instrument (ex: "BTCUSD")
        free_margin : Solde libre disponible (0 = skip margin check)
        """
        from config import (
            MAX_EFFECTIVE_LEVERAGE,
            ASSET_MARGIN_REQUIREMENTS,
            ASSET_CLASS_FALLBACK,
        )

        if balance <= 0:
            logger.warning(f"⚠️ position_size: balance={balance} invalide — taille minimale")
            return self.MIN_SIZE.get(epic.upper(), 0.01)

        sl_dist = abs(entry - sl)
        if sl_dist == 0:
            return 0.0

        min_sz = self.MIN_SIZE.get(epic.upper(), 0.01)

        # ─── Étape 1: Raw sizing (risque classique) ───────────────────────
        risk_amt = balance * risk_pct
        raw_size = risk_amt / sl_dist

        # ─── Étape 2: Leverage Cap ────────────────────────────────────────
        # Nominal = size × entry. Si nominal > MAX_LEVERAGE × capital → plafonner
        nominal = raw_size * entry
        max_nominal = MAX_EFFECTIVE_LEVERAGE * balance
        capped_size = raw_size

        if nominal > max_nominal and entry > 0:
            capped_size = max_nominal / entry
            logger.warning(
                f"⚠️ LEVERAGE CAP {epic}: raw={raw_size:.4f} (nominal={nominal:,.0f}€) "
                f"> {MAX_EFFECTIVE_LEVERAGE}× capital → capped={capped_size:.4f} "
                f"(nominal={capped_size * entry:,.0f}€)"
            )

        # ─── Étape 3: Broker Margin Check ─────────────────────────────────
        # Détermine la classe d'actif pour calculer la marge requise
        asset_class = "forex"  # default
        try:
            profile = ASSET_PROFILES.get(epic, {})
            asset_class = profile.get("cat", ASSET_CLASS_FALLBACK.get(epic, "forex"))
        except Exception:
            asset_class = ASSET_CLASS_FALLBACK.get(epic, "forex")

        margin_rate = ASSET_MARGIN_REQUIREMENTS.get(asset_class, 0.0333)
        margin_required = capped_size * entry * margin_rate

        # Si free_margin fourni et insuffisant → réduire
        effective_free = free_margin if free_margin > 0 else balance * 0.80  # 80% fallback
        if margin_required > effective_free and entry > 0:
            max_size_by_margin = effective_free / (entry * margin_rate)
            old_size = capped_size
            capped_size = min(capped_size, max_size_by_margin)
            logger.warning(
                f"⚠️ MARGIN CAP {epic}: margin_required={margin_required:,.0f}€ "
                f"> free={effective_free:,.0f}€ ({asset_class} {margin_rate:.1%}) "
                f"→ size {old_size:.4f} → {capped_size:.4f}"
            )

        # ─── Final clamp ──────────────────────────────────────────────────
        final_size = max(min_sz, round(capped_size, 2))

        # Log complet pour audit
        final_nominal = final_size * entry
        final_leverage = final_nominal / balance if balance > 0 else 0
        final_margin = final_nominal * margin_rate
        logger.debug(
            f"  📐 {epic}: size={final_size} | nominal={final_nominal:,.0f}€ "
            f"| leverage={final_leverage:.1f}× | margin={final_margin:,.0f}€ "
            f"({asset_class} {margin_rate:.1%}) | risk={risk_amt:.2f}€ / SL={sl_dist:.5f}"
        )
        return final_size
