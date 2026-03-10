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

# Instruments Capital.com (epics)
CAPITAL_INSTRUMENTS = [
    "GOLD",      # Or (XAU/USD)    — trend fort, ATR élevé
    "EURUSD",    # EUR/USD         — Forex liquide
    "GBPUSD",    # GBP/USD         — breakout puissant
    "USDJPY",    # USD/JPY         — tendances longues
    "US500",     # S&P 500         — trending NY open
    "US100",     # NASDAQ 100      — technologie, volatil
    "DE40",      # DAX 40          — London open
    "OIL_BRENT", # Brent Oil       — gros moves
]

# Noms lisibles
INSTRUMENT_NAMES = {
    "GOLD":      "Or (XAU/USD)",
    "EURUSD":    "EUR/USD",
    "GBPUSD":    "GBP/USD",
    "USDJPY":    "USD/JPY",
    "US500":     "S&P 500",
    "US100":     "NASDAQ 100",
    "DE40":      "DAX 40",
    "OIL_BRENT": "Brent Oil",
}

# Valeur d'un pip par instrument (pour affichage)
PIP_FACTOR = {
    "GOLD":      0.01,
    "EURUSD":    0.0001, "GBPUSD": 0.0001, "USDJPY": 0.01,
    "US500":     1.0,    "US100":  1.0,    "DE40":   1.0,
    "OIL_BRENT": 0.01,
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
        Auth Capital.com. Essaie dans l'ordre :
        1. URL DEMO/LIVE selon CAPITAL_DEMO
        2. URL fallback opposée (LIVE si DEMO, DEMO si LIVE)
        3. URL officielle open-api.capital.com (toujours résoluble via Cloudflare)
        """
        urls_to_try: list = [BASE_URL]
        fallback = LIVE_URL if CAPITAL_DEMO else DEMO_URL
        if fallback not in urls_to_try:
            urls_to_try.append(fallback)
        if OPEN_URL not in urls_to_try:
            urls_to_try.append(OPEN_URL)  # Toujours résoluble (Cloudflare CDN)

        for url in urls_to_try:
            try:
                r = self._session.post(
                    f"{url}/session",
                    headers={"X-CAP-API-KEY": CAPITAL_API_KEY},
                    json={"identifier": CAPITAL_EMAIL, "password": CAPITAL_PASSWORD,
                          "encryptedPassword": False},
                    timeout=15,
                )
                r.raise_for_status()
                self._cst   = r.headers.get("CST")
                self._token = r.headers.get("X-SECURITY-TOKEN")
                self._auth_ts  = time.time()
                self._base_url = url
                env = "DEMO" if CAPITAL_DEMO else "LIVE"
                tag = " (open-api fallback)" if url == OPEN_URL else (
                      " (fallback opposé)" if url != BASE_URL else ""
                )
                logger.info(f"🏦 Capital.com connecté ({env}){tag} ✅ — {url}")
                return bool(self._cst and self._token)
            except Exception as e:
                logger.warning(f"⚠️  Capital.com auth échoué sur {url}: {type(e).__name__}: {e}")
                continue

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
        "DE40":       1.0,    # DAX 40
    }  # Forex : 0.01 (par défaut)

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
