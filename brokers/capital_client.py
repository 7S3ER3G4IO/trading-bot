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
# NEMESIS V6+V7 Hybride — 39 actifs | 3 stratégies | Dual Timeframe
# V7 4H: 29 actifs volatils (BK+TF)  |  V6 Daily: 10 forex MR low-vol
# ═══════════════════════════════════════════════════════════════════════════════
CAPITAL_INSTRUMENTS = [
    # ── V7 4H — Forex volatils (7) ──
    "GBPUSD",   "USDCHF",   "AUDNZD",   "AUDJPY",   "NZDJPY",   "EURCHF",   "CHFJPY",
    # ── V7 4H — Commodités (5) ──
    "GOLD",     "SILVER",   "OIL_WTI",  "OIL_BRENT","COPPER",
    # ── V7 4H — Indices (8) ──
    "US500",    "US100",    "US30",     "DE40",     "FR40",     "UK100",    "J225",     "AU200",
    # ── V7 4H — Crypto (3) ──
    "BNBUSD",   "XRPUSD",  "AVAXUSD",
    # ── V7 4H — Stocks (6) ──
    "AAPL",     "TSLA",     "NVDA",     "MSFT",     "META",     "GOOGL",
    # ── V6 Daily — Forex MR low-vol (10) ──
    "AUDUSD",   "NZDUSD",   "EURGBP",   "EURAUD",   "GBPAUD",
    "AUDCAD",   "GBPCAD",   "GBPCHF",   "CADCHF",   "NZDCAD",
]

INSTRUMENT_NAMES = {
    "GBPUSD":"GBP/USD","USDCHF":"USD/CHF","AUDNZD":"AUD/NZD","AUDJPY":"AUD/JPY",
    "NZDJPY":"NZD/JPY","EURCHF":"EUR/CHF","CHFJPY":"CHF/JPY",
    "GOLD":"Gold","SILVER":"Silver","OIL_WTI":"WTI Crude","OIL_BRENT":"Brent","COPPER":"Copper",
    "US500":"S&P 500","US100":"NASDAQ","US30":"Dow Jones","DE40":"DAX 40",
    "FR40":"CAC 40","UK100":"FTSE 100","J225":"Nikkei","AU200":"ASX 200",
    "BNBUSD":"BNB/USD","XRPUSD":"XRP/USD","AVAXUSD":"AVAX/USD",
    "AAPL":"Apple","TSLA":"Tesla","NVDA":"Nvidia","MSFT":"Microsoft","META":"Meta","GOOGL":"Google",
    "AUDUSD":"AUD/USD","NZDUSD":"NZD/USD","EURGBP":"EUR/GBP","EURAUD":"EUR/AUD",
    "GBPAUD":"GBP/AUD","AUDCAD":"AUD/CAD","GBPCAD":"GBP/CAD","GBPCHF":"GBP/CHF",
    "CADCHF":"CAD/CHF","NZDCAD":"NZD/CAD",
}

PIP_FACTOR = {
    "GBPUSD":0.0001,"USDCHF":0.0001,"AUDNZD":0.0001,"AUDJPY":0.01,
    "NZDJPY":0.01,"EURCHF":0.0001,"CHFJPY":0.01,
    "GOLD":0.01,"SILVER":0.001,"OIL_WTI":0.01,"OIL_BRENT":0.01,"COPPER":0.0001,
    "US500":0.1,"US100":0.1,"US30":1.0,"DE40":1.0,
    "FR40":1.0,"UK100":1.0,"J225":1.0,"AU200":1.0,
    "BNBUSD":0.01,"XRPUSD":0.0001,"AVAXUSD":0.01,
    "AAPL":0.01,"TSLA":0.01,"NVDA":0.01,"MSFT":0.01,"META":0.01,"GOOGL":0.01,
    "AUDUSD":0.0001,"NZDUSD":0.0001,"EURGBP":0.0001,"EURAUD":0.0001,
    "GBPAUD":0.0001,"AUDCAD":0.0001,"GBPCAD":0.0001,"GBPCHF":0.0001,
    "CADCHF":0.0001,"NZDCAD":0.0001,
}

MIN_SIZE = {
    "GOLD":0.01,"SILVER":1,"COPPER":1,"OIL_WTI":0.1,"OIL_BRENT":0.1,
    "US500":0.1,"US100":0.1,"US30":0.1,"DE40":0.1,"FR40":0.1,
    "UK100":0.1,"J225":1,"AU200":0.1,
    "AAPL":1,"TSLA":1,"NVDA":1,"MSFT":1,"META":1,"GOOGL":1,
    "BNBUSD":0.01,"XRPUSD":1,"AVAXUSD":1,
}

# ═══════════════════════════════════════════════════════════════════════════════
# ASSET_PROFILES — V6+V7 hybride optimisé
#   strat: "BK" (Breakout) | "MR" (Mean Reversion) | "TF" (Trend Following)
#   tf:    "4h" (V7) | "1d" (V6 daily)
#   tp1/tp2/tp3: en multiples du SL distance
#   sl_buffer (slb): multiplicateur ATR pour SL (MR/TF) ou % range (BK)
#   rsi_lo/rsi_hi: seuils RSI pour Mean Reversion uniquement
# ═══════════════════════════════════════════════════════════════════════════════
ASSET_PROFILES = {
    # ── V7 4H — Forex volatils ──
    "GBPUSD":  {"strat":"TF","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":1.0, "max_hold":30,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    "USDCHF":  {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":18},
    "AUDNZD":  {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "AUDJPY":  {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    "NZDJPY":  {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":18},
    "EURCHF":  {"strat":"BK","tf":"4h","tp1":1.0,"tp2":2.0,"tp3":3.0,"sl_buffer":0.12,"max_hold":30,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "CHFJPY":  {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    # ── V7 4H — Commodités ──
    "GOLD":    {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "SILVER":  {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "OIL_WTI": {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    "OIL_BRENT":{"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.08,"range_lb":18},
    "COPPER":  {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    # ── V7 4H — Indices ──
    "US500":   {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    "US100":   {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "US30":    {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "DE40":    {"strat":"TF","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":1.0, "max_hold":30,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "FR40":    {"strat":"MR","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":18,"adx_min":12,"bk_margin":0.05,"range_lb":6,"rsi_lo":30,"rsi_hi":70},
    "UK100":   {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    "J225":    {"strat":"TF","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":1.0, "max_hold":30,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "AU200":   {"strat":"MR","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":18,"adx_min":12,"bk_margin":0.05,"range_lb":6,"rsi_lo":25,"rsi_hi":75},
    # ── V7 4H — Crypto ──
    "BNBUSD":  {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.08,"range_lb":18},
    "XRPUSD":  {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "AVAXUSD": {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":18,"adx_min":12,"bk_margin":0.08,"range_lb":18},
    # ── V7 4H — Stocks ──
    "AAPL":    {"strat":"BK","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":0.10,"max_hold":18,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "TSLA":    {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "NVDA":    {"strat":"TF","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":1.0, "max_hold":30,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "MSFT":    {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.08,"range_lb":6},
    "META":    {"strat":"BK","tf":"4h","tp1":2.0,"tp2":3.0,"tp3":5.0,"sl_buffer":0.15,"max_hold":42,"adx_min":12,"bk_margin":0.05,"range_lb":6},
    "GOOGL":   {"strat":"TF","tf":"4h","tp1":1.5,"tp2":2.5,"tp3":4.0,"sl_buffer":1.0, "max_hold":30,"adx_min":12,"bk_margin":0.05,"range_lb":18},
    # ── V6 Daily — Forex MR low-vol ──
    "AUDUSD":  {"strat":"MR","tf":"1d","tp1":2.0,"tp2":3.0,"tp3":4.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":25,"rsi_hi":75},
    "NZDUSD":  {"strat":"MR","tf":"1d","tp1":1.5,"tp2":2.5,"tp3":3.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":30,"rsi_hi":70},
    "EURGBP":  {"strat":"BK","tf":"1d","tp1":1.0,"tp2":2.0,"tp3":3.0,"sl_buffer":0.10,"max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "EURAUD":  {"strat":"MR","tf":"1d","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":30,"rsi_hi":70},
    "GBPAUD":  {"strat":"TF","tf":"1d","tp1":2.0,"tp2":3.5,"tp3":5.5,"sl_buffer":2.0, "max_hold":15,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "AUDCAD":  {"strat":"MR","tf":"1d","tp1":2.0,"tp2":3.0,"tp3":4.0,"sl_buffer":0.6, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":25,"rsi_hi":75},
    "GBPCAD":  {"strat":"BK","tf":"1d","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.12,"max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5},
    "GBPCHF":  {"strat":"MR","tf":"1d","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":30,"rsi_hi":70},
    "CADCHF":  {"strat":"MR","tf":"1d","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":30,"rsi_hi":70},
    "NZDCAD":  {"strat":"MR","tf":"1d","tp1":1.5,"tp2":2.0,"tp3":3.5,"sl_buffer":0.5, "max_hold":5,"adx_min":10,"bk_margin":0.05,"range_lb":5,"rsi_lo":30,"rsi_hi":70},
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
                    tag = "" if url == primary else (
                          " (fallback opposé)" if url == opposite else " (open-api fallback)"
                    )
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
                    logger.warning(f"⚠️  Capital.com ordre rejeté : {data.get('reason', '')}")
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
                # Log le body de la réponse pour diagnostic
                try:
                    err_body = r.json()
                except Exception:
                    err_body = r.text[:300]
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
        "OIL_WTI":    1.00,   # WTI
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
