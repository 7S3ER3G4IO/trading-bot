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
    # ── 1H — Forex majeurs (11) ──
    "EURUSD",   "USDJPY",   "GBPUSD",   "GBPJPY",   "EURJPY",
    "USDCHF",   "AUDNZD",   "AUDJPY",   "NZDJPY",   "EURCHF",   "CHFJPY",
    # ── 1H — Commodités (6) ──
    "GOLD",     "SILVER",   "OIL_CRUDE",  "OIL_BRENT","COPPER",   "NATURALGAS",
    # ── 1H — Indices (8) ──
    "US500",    "US100",    "US30",     "DE40",     "FR40",     "UK100",    "J225",     "AU200",
    # ── 1H — Crypto (6) ──
    "BTCUSD",   "ETHUSD",   "BNBUSD",   "XRPUSD",   "SOLUSD",   "AVAXUSD",
    # ── 1H — Stocks (8) ──
    "AAPL",     "TSLA",     "NVDA",     "MSFT",     "META",     "GOOGL",    "AMZN",     "AMD",
    # ── Daily — Forex MR low-vol (9) ──
    "AUDUSD",   "NZDUSD",   "EURGBP",   "EURAUD",   "GBPAUD",
    "AUDCAD",   "GBPCAD",   "GBPCHF",   "CADCHF",
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

# ═══════════════════════════════════════════════════════════════════════════════
# ASSET_PROFILES — V8 Haute Fréquence
#   strat: "BK" (Breakout) | "MR" (Mean Reversion) | "TF" (Trend Following)
#   tf:    "1h" (V8) | "1d" (Daily MR)
#   range_lb: bougies lookback pour BK (4 = 4h en 1H)
#   bk_margin: % du range pour valider breakout (0.03 = 3% — sensible pour 1H)
#   tp1/tp2/tp3: en multiples ATR
# ═══════════════════════════════════════════════════════════════════════════════
ASSET_PROFILES = {
    # ── 1H — Forex majeurs (haute liquidité, spreads bas) ──
    "EURUSD":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "USDJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "GBPUSD":  {"strat":"TF","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "GBPJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "EURJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "USDCHF":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "AUDNZD":  {"strat":"MR","tf":"1h","cat":"forex_mr","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0,"max_hold":12,"adx_min":10,"bk_margin":0.03,"range_lb":4,"rsi_lo":35,"rsi_hi":65},
    "AUDJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "NZDJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "EURCHF":  {"strat":"MR","tf":"1h","cat":"forex_mr","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0,"max_hold":12,"adx_min":10,"bk_margin":0.03,"range_lb":4,"rsi_lo":35,"rsi_hi":65},
    "CHFJPY":  {"strat":"BK","tf":"1h","cat":"forex","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    # ── 1H — Commodités (haute volatilité) ──
    "GOLD":       {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "SILVER":     {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "OIL_CRUDE":  {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "OIL_BRENT":  {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "COPPER":     {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "NATURALGAS": {"strat":"BK","tf":"1h","cat":"commodities","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    # ── 1H — Indices ──
    "US500":   {"strat":"BK","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "US100":   {"strat":"BK","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "US30":    {"strat":"BK","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "DE40":    {"strat":"TF","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "FR40":    {"strat":"MR","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":12,"adx_min":10,"bk_margin":0.03,"range_lb":4,"rsi_lo":35,"rsi_hi":65},
    "UK100":   {"strat":"BK","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "J225":    {"strat":"TF","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":18,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "AU200":   {"strat":"MR","tf":"1h","cat":"indices","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":12,"adx_min":10,"bk_margin":0.03,"range_lb":4,"rsi_lo":35,"rsi_hi":65},
    # ── 1H — Crypto (haute vol, 24/7) ──
    "BTCUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "ETHUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "BNBUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "XRPUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.10,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "SOLUSD":  {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "AVAXUSD": {"strat":"BK","tf":"1h","cat":"crypto","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    # ── 1H — Stocks US (NY session) ──
    "AAPL":    {"strat":"BK","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.10,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    "TSLA":    {"strat":"BK","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    "NVDA":    {"strat":"TF","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "MSFT":    {"strat":"BK","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    "META":    {"strat":"BK","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    "GOOGL":   {"strat":"TF","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "AMZN":    {"strat":"TF","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":1.0, "max_hold":12,"adx_min":12,"bk_margin":0.03,"range_lb":4},
    "AMD":     {"strat":"BK","tf":"1h","cat":"stocks","tp1":1.5,"tp2":3.0,"tp3":5.0,"sl_buffer":0.12,"max_hold":8, "adx_min":12,"bk_margin":0.03,"range_lb":4},
    # ── Daily — Forex MR low-vol (fiables, 1 signal/jour) ──
    "AUDUSD":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":2.0,"tp2":3.0,"tp3":4.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
    "NZDUSD":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":1.5,"tp2":2.5,"tp3":3.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
    "EURGBP":  {"strat":"BK","tf":"1d","cat":"forex","tp1":1.0,"tp2":2.0,"tp3":3.0,"sl_buffer":0.10,"max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "EURAUD":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":1.0, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
    "GBPAUD":  {"strat":"TF","tf":"1d","cat":"forex","tp1":2.0,"tp2":3.5,"tp3":5.5,"sl_buffer":2.0, "max_hold":15,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "AUDCAD":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":2.0,"tp2":3.0,"tp3":4.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
    "GBPCAD":  {"strat":"BK","tf":"1d","cat":"forex","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "GBPCHF":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":1.0, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
    "CADCHF":  {"strat":"MR","tf":"1d","cat":"forex_mr","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":1.0, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":35,"rsi_hi":65},
}







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

    def fetch_ohlcv(self, epic: str, timeframe: str = "5m", count: int = 300) -> Optional[pd.DataFrame]:
        """Télécharge les bougies OHLCV depuis Capital.com."""
        if not self.available:
            return None
        try:
            gran = TF_MAP.get(timeframe, "MINUTE_5")
            r = self._session.get(
                f"{self._base_url}/prices/{epic}",
                headers=self._headers(),
                params={"resolution": gran, "max": count, "pageSize": count},
                timeout=15,
            )
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
            logger.error(f"❌ Capital.com fetch_ohlcv {epic}: {e}")
            return None

    def get_balance(self) -> float:
        """Retourne le solde disponible du compte."""
        if not self.available:
            return 0.0
        try:
            r = self._session.get(f"{self._base_url}/accounts", headers=self._headers(), timeout=10)
            r.raise_for_status()
            accounts = r.json().get("accounts", [])
            for acc in accounts:
                if acc.get("preferred"):
                    return float(acc["balance"]["available"])
            if accounts:
                return float(accounts[0]["balance"]["available"])
            return 0.0
        except Exception as e:
            logger.error(f"❌ Capital.com get_balance: {e}")
            return 0.0

    def get_current_price(self, epic: str) -> Optional[dict]:
        """Retourne le bid/ask actuel d'un instrument."""
        if not self.available:
            return None
        try:
            r = self._session.get(
                f"{self._base_url}/markets/{epic}",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            snap = data.get("snapshot", {})
            return {
                "bid": float(snap.get("bid", 0)),
                "ask": float(snap.get("offer", 0)),
                "mid": (float(snap.get("bid", 0)) + float(snap.get("offer", 0))) / 2,
            }
        except Exception as e:
            logger.error(f"❌ Capital.com get_price {epic}: {e}")
            return None

    # ─── Ordres ──────────────────────────────────────────────────────────────

    def confirm_deal(self, deal_ref: str, retries: int = 3) -> Optional[str]:
        """
        Échange un dealReference temporaire contre le dealId permanent.
        Retry avec délai car Capital.com peut prendre quelques centaines de ms
        à confirmer un ordre même après avoir retourné le dealReference.
        """
        if not self.available or not deal_ref:
            return None
        for attempt in range(retries):
            try:
                r = self._session.get(
                    f"{self._base_url}/confirms/{deal_ref}",
                    headers=self._headers(),
                    timeout=10,
                )
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
                # Status = PENDING / vide → retry
                logger.debug(f"  ⏳ Confirm attempt {attempt+1}/{retries} status={status!r}")
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"❌ Capital.com confirm_deal attempt {attempt+1}: {e}")
                time.sleep(0.5)
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
    ) -> float:
        """
        Calcule la taille de position en unités Capital.com.
        Risque = risk_pct × balance / distance_SL
        """
        if balance <= 0:
            logger.warning(f"⚠️ position_size: balance={balance} invalide — taille minimale utilisée")
            min_sz = self.MIN_SIZE.get(epic.upper(), 0.01)
            return min_sz
        sl_dist = abs(entry - sl)
        if sl_dist == 0:
            return 0.0
        risk_amt = balance * risk_pct
        size     = risk_amt / sl_dist
        min_sz   = self.MIN_SIZE.get(epic.upper(), 0.01)
        size     = max(min_sz, round(size, 2))
        logger.debug(f"  Capital.com size {epic}: {size} (risque={risk_amt:.2f} / SL_dist={sl_dist:.5f} / min={min_sz})")
        return size
