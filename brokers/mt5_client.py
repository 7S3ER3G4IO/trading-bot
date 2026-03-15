"""
brokers/mt5_client.py — Client IC Markets MT5 via MetaApi Cloud.

Miroir de l'interface CapitalClient pour intégration transparente dans Nemesis.

Variables d'environnement :
  METAAPI_TOKEN       → token API MetaApi (depuis app.metaapi.cloud/token)
  METAAPI_ACCOUNT_ID  → ID du compte MetaApi (généré à l'ajout du compte MT5)
  MT5_LOGIN           → numéro de compte MT5
  MT5_SERVER          → serveur MT5 (ex: ICMarketsEU-Demo)
"""
import os
import asyncio
import time
import threading
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List
from loguru import logger

# ─── Configuration ────────────────────────────────────────────────────────────
METAAPI_TOKEN      = os.getenv("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID", "")

# ─── Symbol mapping Capital.com → MT5 ────────────────────────────────────────
# IC Markets MT5 uses standard symbol names with some variations.
SYMBOL_MAP = {
    # Forex — same on both
    "EURUSD": "EURUSD", "USDJPY": "USDJPY", "GBPUSD": "GBPUSD",
    "GBPJPY": "GBPJPY", "EURJPY": "EURJPY", "USDCHF": "USDCHF",
    "AUDNZD": "AUDNZD", "AUDJPY": "AUDJPY", "NZDJPY": "NZDJPY",
    "EURCHF": "EURCHF", "CHFJPY": "CHFJPY", "AUDUSD": "AUDUSD",
    "NZDUSD": "NZDUSD", "EURGBP": "EURGBP", "EURAUD": "EURAUD",
    "GBPAUD": "GBPAUD", "AUDCAD": "AUDCAD", "GBPCAD": "GBPCAD",
    "GBPCHF": "GBPCHF", "CADCHF": "CADCHF",
    # Commodities
    "GOLD":       "XAUUSD",
    "SILVER":     "XAGUSD",
    "OIL_CRUDE":  "USOUSD",    # WTI Crude on IC Markets
    "OIL_BRENT":  "UKOUSD",    # Brent Crude on IC Markets
    "COPPER":     "XCUUSD",
    "NATURALGAS": "XNGUSD",
    # Indices — IC Markets uses .cash suffix
    "US500":  "US500.cash",
    "US100":  "USTEC.cash",
    "US30":   "US30.cash",
    "DE40":   "DE40.cash",
    "FR40":   "F40.cash",
    "UK100":  "UK100.cash",
    "J225":   "JP225.cash",
    "AU200":  "AUS200.cash",
    # Crypto — IC Markets format
    "BTCUSD":  "BTCUSD",
    "ETHUSD":  "ETHUSD",
    "BNBUSD":  "BNBUSD",
    "XRPUSD":  "XRPUSD",
    "SOLUSD":  "SOLUSD",
    "AVAXUSD": "AVAXUSD",
    # Stocks — IC Markets CFDs
    "AAPL":  "AAPL.US",
    "TSLA":  "TSLA.US",
    "NVDA":  "NVDA.US",
    "MSFT":  "MSFT.US",
    "META":  "META.US",
    "GOOGL": "GOOGL.US",
    "AMZN":  "AMZN.US",
    "AMD":   "AMD.US",
}

# Reverse map for converting MT5 symbols back to Capital.com epics
REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}

# ─── Timeframe mapping ───────────────────────────────────────────────────────
TF_MAP = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "30m": "30m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}


class MT5Client:
    """Client IC Markets MT5 via MetaApi Cloud — interface compatible CapitalClient."""

    def __init__(self):
        self._ok = False
        self._api = None
        self._account = None
        self._connection = None
        self._loop = None
        self._loop_thread = None
        # ── Reconnexion auto avec backoff exponentiel ─────────────────────
        self._reconnect_attempts  = 0
        self._reconnect_delays    = [3, 10, 30]   # secondes entre tentatives
        self._last_reconnect_ts   = 0.0            # time.monotonic()
        self._reconnect_lock      = threading.Lock()

        if not METAAPI_TOKEN or not METAAPI_ACCOUNT_ID:
            logger.debug("ℹ️  METAAPI_TOKEN / METAAPI_ACCOUNT_ID manquants — MT5 broker désactivé")
            return

        try:
            # Configurer proxy AVANT import MetaApi (SDK lit env au chargement)
            import os as _os
            _prx = _os.environ.get("HTTPS_PROXY", _os.environ.get("ALL_PROXY", ""))
            if _prx:
                _os.environ["HTTPS_PROXY"] = _prx
                _os.environ["HTTP_PROXY"]  = _prx
                _os.environ["ALL_PROXY"]   = _prx
                _os.environ.setdefault("NO_PROXY", "capital.com,localhost,127.0.0.1")
                _os.environ.setdefault("no_proxy", "capital.com,localhost,127.0.0.1")
            from metaapi_cloud_sdk import MetaApi
            self._MetaApi = MetaApi
        except ImportError:
            logger.warning("⚠️ metaapi-cloud-sdk non installé — MT5 broker désactivé")
            return

        # Start a dedicated event loop in a daemon thread for async operations
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="mt5-event-loop"
        )
        self._loop_thread.start()

        # Configurer proxy SOCKS5 UNIQUEMENT pour MetaApi (bypass ban IP Hetzner via WARP)
        # Capital.com doit se connecter directement (pas via proxy)
        _proxy = os.getenv("HTTPS_PROXY", os.getenv("ALL_PROXY", ""))
        if _proxy:
            os.environ["HTTPS_PROXY"] = _proxy
            os.environ["HTTP_PROXY"]  = _proxy
            os.environ["ALL_PROXY"]   = _proxy
            # Exclure Capital.com, PostgreSQL et services locaux du proxy
            _no_proxy = (
                "capital.com,backend-capital.com,open-api.capital.com,"
                "localhost,127.0.0.1,172.17.0.1,postgres,nemesis_postgres"
            )
            os.environ["NO_PROXY"]    = _no_proxy
            os.environ["no_proxy"]    = _no_proxy
            logger.info(f"↪️ MT5 MetaApi: proxy SOCKS5 configuré → {_proxy} (NO_PROXY: capital.com)")

        # Initialize the connection (timeout 300s = 4 régions × 60s + buffer)
        self._ok = self._run_async(self._async_init(), timeout=300)

    def _run_loop(self):
        """Run the asyncio event loop in a background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_async(self, coro, timeout: float = 30):
        """Run an async coroutine from sync context. Thread-safe."""
        if not self._loop or not self._loop.is_running():
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except Exception as e:
            _ename = type(e).__name__
            # NotFoundException = market closed / symbol unavailable — not a real error
            if "NotFound" in _ename or "NotFoundException" in str(e)[:40]:
                logger.debug(f"ℹ️  MT5 {_ename} (marché fermé / symbole indisponible): {e}")
            else:
                logger.error(f"❌ MT5 async error: {_ename}: {e}")
            return None

    async def _async_init(self) -> bool:
        """Connexion MetaApi IC Markets MT5.
        
        SDK v29: get_rpc_connection() supprim\u00e9, remplacer par get_streaming_connection().
        FIX geo-block: param\u00e8tre 'domain' pour bypasser le ban IP Hetzner.
        """
        import importlib.metadata
        try:
            sdk_ver = importlib.metadata.version("metaapi-cloud-sdk")
        except Exception:
            sdk_ver = "?"
        logger.info(f"\u23f3 MetaApi SDK v{sdk_ver} — initialisation connexion MT5...")

        # ── Tentative 1 : domain par d\u00e9faut (agiliumtrade.ai) ──────────────────
        for attempt, (domain, timeout) in enumerate([
            ("agiliumtrade.agiliumtrade.ai", 60),
            ("new-york.agiliumtrade.ai",     60),
            ("london.agiliumtrade.ai",        60),
            ("singapore.agiliumtrade.ai",     60),
        ], 1):
            try:
                logger.info(f"\U0001f50c MT5 tentative {attempt}/4 — domaine: {domain}")
                # SDK v29: le param\u00e8tre 'domain' permet de choisir l'endpoint
                # Proxy SOCKS5 gere le routage — pas besoin de domain=
                self._api = self._MetaApi(METAAPI_TOKEN)

                # R\u00e9cup\u00e9rer le compte
                self._account = await self._api.metatrader_account_api.get_account(
                    METAAPI_ACCOUNT_ID
                )
                if self._account.state not in ("DEPLOYED", "DEPLOYING"):
                    logger.info("🔄 MT5 MetaApi: d\u00e9ploiement du compte...")
                    await self._account.deploy()

                logger.info("⏳ MT5 MetaApi: connexion au terminal MT5...")
                # SDK v29 fix: wait_connected() KeyError si compte deja CONNECTED
                conn_status = getattr(self._account, "connection_status", None)
                if conn_status != "CONNECTED":
                    try:
                        await self._account.wait_connected(timeout_in_seconds=timeout)
                    except (KeyError, TypeError) as ke:
                        logger.debug(f"wait_connected() ignore: {ke}")
                else:
                    logger.info("MT5: compte deja CONNECTED")

                # SDK v29: get_streaming_connection() est SYNCHRONE (pas de await)
                try:
                    self._connection = self._account.get_streaming_connection()
                except AttributeError:
                    # SDK < v27 fallback
                    self._connection = self._account.get_rpc_connection()
                await self._connection.connect()
                try:
                    await self._connection.wait_synchronized(timeout_in_seconds=timeout)
                except (KeyError, TypeError) as ke:
                    logger.debug(f"wait_synchronized() ignore: {ke}")

                logger.info(
                    f"\U0001f3e6 MT5 IC Markets connect\u00e9 \u2705 (domain={domain}, "
                    f"account={METAAPI_ACCOUNT_ID[:8]}...)"
                )
                return True

            except Exception as err:
                ename = type(err).__name__
                if any(x in str(err).lower() or x in ename.lower()
                       for x in ["forbidden", "403", "unauthorized", "geo"]):
                    logger.warning(f"\u26a0\ufe0f  MT5 {domain}: geo-bloqu\u00e9 ({ename}) — essai domaine suivant...")
                else:
                    logger.warning(f"\u26a0\ufe0f  MT5 {domain}: {ename}: {str(err)[:120]}")
                continue

        logger.error(
            "\u274c MT5: toutes les r\u00e9gions MetaApi bloqu\u00e9es depuis ce VPS. "
            "Solution: contacter MetaApi support pour whitelister l'IP du VPS, "
            "ou configurer un proxy sortant (ex: Cloudflare Tunnel)."
        )
        return False


    # ─── Reconnexion auto ─────────────────────────────────────────────────

    def _is_connected(self) -> bool:
        """Retourne True si la connexion MT5 est vivante (terminal_state accessible)."""
        if not self._ok or self._connection is None:
            return False
        try:
            ts = getattr(self._connection, "terminal_state", None)
            return ts is not None and ts.connected
        except Exception:
            return False

    def _maybe_reconnect(self) -> bool:
        """Tente une reconnexion si la connexion est perdue. Backoff exponentiel.
        Retourne True si connectionn disponible (existante ou restaurée).
        """
        if self._is_connected():
            self._reconnect_attempts = 0
            return True

        with self._reconnect_lock:
            # Double-check après verrou
            if self._is_connected():
                return True

            now = time.monotonic()
            attempts = self._reconnect_attempts
            if attempts >= len(self._reconnect_delays):
                # Max tentatives atteint — attente longue
                if now - self._last_reconnect_ts < 120:
                    return False
                self._reconnect_attempts = 0  # Reset pour réessayer
                attempts = 0

            delay = self._reconnect_delays[attempts]
            if now - self._last_reconnect_ts < delay:
                return False  # Trop tôt pour réessayer

            self._last_reconnect_ts = now
            self._reconnect_attempts += 1

            logger.warning(
                f"⚠️ MT5: connexion perdue — tentative {self._reconnect_attempts}/{len(self._reconnect_delays)} "
                f"(backoff={delay}s)"
            )

            try:
                ok = self._run_async(self._async_reconnect(), timeout=90)
                if ok:
                    self._ok = True
                    self._reconnect_attempts = 0
                    logger.info("✅ MT5: reconnexion réussie")
                    return True
                else:
                    logger.warning(f"❌ MT5: reconnexion échouée (tentative {self._reconnect_attempts})")
                    return False
            except Exception as e:
                logger.error(f"❌ MT5 reconnect error: {e}")
                return False

    async def _async_reconnect(self) -> bool:
        """Relance la connexion streaming MT5."""
        try:
            if self._connection:
                try:
                    await self._connection.close()
                except Exception:
                    pass
            conn_status = getattr(self._account, "connection_status", None)
            if conn_status != "CONNECTED":
                await self._account.wait_connected(timeout_in_seconds=60)
            try:
                self._connection = self._account.get_streaming_connection()
            except AttributeError:
                self._connection = self._account.get_rpc_connection()
            await self._connection.connect()
            try:
                await self._connection.wait_synchronized(timeout_in_seconds=60)
            except (KeyError, TypeError):
                pass
            return True
        except Exception as e:
            logger.error(f"❌ MT5 _async_reconnect: {e}")
            return False

    @property
    def available(self) -> bool:
        return self._ok

    # ─── Solde ────────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Retourne l'equity du compte MT5. Tente auto-reconnexion si connexion perdue."""
        if not self.available:
            return 0.0
        # Tente reconnexion si terminal_state vide (connexion silencieusement perdue)
        try:
            ts = getattr(self._connection, "terminal_state", None)
            info = ts.account_information if ts else None
            if info:
                return float(info.get("equity", info.get("balance", 0)))
            # terminal_state accessible mais sans données → essai reconnexion
            if not self._is_connected():
                logger.warning("⚠️ MT5 get_balance: terminal_state vide — tentative reconnexion...")
                self._maybe_reconnect()
            return 0.0
        except Exception as e:
            logger.error(f"❌ MT5 get_balance: {e}")
            self._maybe_reconnect()
            return 0.0

    # ─── Prix temps réel ──────────────────────────────────────────────────────

    def get_current_price(self, epic: str) -> Optional[dict]:
        """Prix bid/ask via terminal_state (SDK v29)."""
        if not self.available:
            return None
        symbol = SYMBOL_MAP.get(epic, epic)
        try:
            ts = getattr(self._connection, "terminal_state", None)
            if not ts:
                return None
            # SDK v29: terminal_state.prices est un dict {symbol: {bid, ask, ...}}
            prices = getattr(ts, "prices", {})
            if isinstance(prices, dict):
                p = prices.get(symbol)
            elif hasattr(ts, "get_price"):
                p = ts.get_price(symbol)
            else:
                return None
            if not p:
                return None
            bid = float(p.get("bid", 0) or p.get("Bid", 0))
            ask = float(p.get("ask", 0) or p.get("Ask", 0))
            if bid <= 0 or ask <= 0:
                return None
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        except Exception as e:
            logger.debug(f"MT5 get_current_price {epic} ({symbol}): {e}")
            return None


    # ─── Données OHLCV ───────────────────────────────────────────────────────

    def fetch_ohlcv(self, epic: str, timeframe: str = "1h",
                    count: int = 300) -> Optional[pd.DataFrame]:
        """Télécharge les bougies OHLCV depuis MT5 via MetaApi."""
        if not self.available:
            return None
        symbol = SYMBOL_MAP.get(epic, epic)
        mt5_tf = TF_MAP.get(timeframe, "1h")

        try:
            # SDK v29: get_historical_candles sur l'account (pas la connection)
            now = datetime.now(timezone.utc)
            # estimer la fenêtre temporelle selon le timeframe
            tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                          "1h": 60, "4h": 240, "1d": 1440}.get(timeframe, 60)
            start = now - timedelta(minutes=tf_minutes * count * 2)
            candles = self._run_async(
                self._account.get_historical_candles(
                    symbol, mt5_tf, start, count
                ),
                timeout=60,
            )

            if not candles:
                logger.debug(f"MT5 fetch_ohlcv {epic}: pas de données (fallback Capital.com)")
                return None

            records = []
            for c in candles:
                ts = c.get("time", "")
                if isinstance(ts, str):
                    ts = pd.Timestamp(ts).tz_localize("UTC") if ts else None
                elif isinstance(ts, (int, float)):
                    ts = pd.Timestamp(ts, unit="s", tz="UTC")
                else:
                    ts = pd.Timestamp(ts)

                records.append({
                    "timestamp": ts,
                    "open":   float(c.get("open", 0)),
                    "high":   float(c.get("high", 0)),
                    "low":    float(c.get("low", 0)),
                    "close":  float(c.get("close", 0)),
                    "volume": float(c.get("tickVolume", c.get("volume", 0))),
                })

            if not records:
                return None

            df = pd.DataFrame(records)
            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df

        except Exception as e:
            logger.debug(f"MT5 fetch_ohlcv {epic} ({symbol}): {e} — fallback Capital.com")
            return None

    # ─── Ordres ──────────────────────────────────────────────────────────────

    # MIN_SIZE matching CapitalClient — IC Markets lot sizes
    MIN_SIZE = {
        "EURUSD": 0.01, "USDJPY": 0.01, "GBPUSD": 0.01, "GBPJPY": 0.01,
        "EURJPY": 0.01, "USDCHF": 0.01, "AUDUSD": 0.01, "NZDUSD": 0.01,
        "EURGBP": 0.01, "EURAUD": 0.01, "GBPAUD": 0.01, "AUDCAD": 0.01,
        "GBPCAD": 0.01, "GBPCHF": 0.01, "CADCHF": 0.01, "EURCHF": 0.01,
        "AUDNZD": 0.01, "AUDJPY": 0.01, "NZDJPY": 0.01, "CHFJPY": 0.01,
        "GOLD": 0.01, "XAUUSD": 0.01, "SILVER": 0.01, "XAGUSD": 0.01,
        "OIL_CRUDE": 0.01, "OIL_BRENT": 0.01,
        "US500": 0.01, "US100": 0.01, "US30": 0.01, "DE40": 0.01,
        "UK100": 0.01, "FR40": 0.01, "J225": 0.01, "AU200": 0.01,
        "BTCUSD": 0.01, "ETHUSD": 0.01,
    }

    def position_size(
        self, balance: float, risk_pct: float, entry: float, sl: float,
        epic: str, free_margin: float = 0.0,
    ) -> float:
        """Calcule la taille de position — miroir de CapitalClient.position_size()."""
        try:
            from config import (
                MAX_EFFECTIVE_LEVERAGE, ASSET_MARGIN_REQUIREMENTS, ASSET_CLASS_FALLBACK,
            )
        except ImportError:
            MAX_EFFECTIVE_LEVERAGE = 30
            ASSET_MARGIN_REQUIREMENTS = {"forex": 0.0333, "indices": 0.05, "crypto": 0.50, "commodities": 0.10, "stocks": 0.20}
            ASSET_CLASS_FALLBACK = {}

        if balance <= 0:
            return self.MIN_SIZE.get(epic.upper(), 0.01)

        sl_dist = abs(entry - sl)
        if sl_dist == 0:
            return 0.0

        min_sz = self.MIN_SIZE.get(epic.upper(), 0.01)

        # Étape 1: Raw sizing
        risk_amt = balance * risk_pct
        raw_size = risk_amt / sl_dist

        # Étape 2: Leverage cap
        nominal = raw_size * entry
        max_nominal = MAX_EFFECTIVE_LEVERAGE * balance
        capped_size = raw_size
        if nominal > max_nominal and entry > 0:
            capped_size = max_nominal / entry

        # Étape 3: Margin check
        try:
            from brokers.capital_client import ASSET_PROFILES
            profile = ASSET_PROFILES.get(epic, {})
            asset_class = profile.get("cat", ASSET_CLASS_FALLBACK.get(epic, "forex"))
        except ImportError:
            asset_class = "forex"

        margin_rate = ASSET_MARGIN_REQUIREMENTS.get(asset_class, 0.0333)
        margin_required = capped_size * entry * margin_rate
        effective_free = free_margin if free_margin > 0 else balance * 0.80
        if margin_required > effective_free and entry > 0:
            capped_size = min(capped_size, effective_free / (entry * margin_rate))

        final_size = max(min_sz, round(capped_size, 2))
        logger.debug(
            f"  📐 MT5 {epic}: size={final_size} | risk={risk_amt:.2f} / SL={sl_dist:.5f}"
        )
        return final_size

    def place_limit_order(
        self, epic: str, direction: str, size: float,
        sl_price: float, tp_price: float, timeout_s: int = 15,
    ) -> Optional[str]:
        """Place un ordre marché (MT5 n'a pas de limit-order natif via MetaApi RPC)."""
        return self.place_market_order(epic, direction, size, sl_price, tp_price)

    def _headers(self) -> dict:
        """Stub pour compatibilité TradeExecutor fast-path."""
        return {}

    def place_market_order(
        self,
        epic: str,
        direction: str,       # "BUY" ou "SELL"
        size: float,
        sl_price: float,
        tp_price: float,
    ) -> Optional[str]:
        """
        Place un ordre marché sur MT5 via MetaApi.
        Retourne le dealId (position ID) ou None si erreur.
        """
        if not self.available:
            return None

        symbol = SYMBOL_MAP.get(epic, epic)
        # Utilise le bon arrondi par instrument (GOLD=2, USDJPY=3, indices=1)
        try:
            from brokers.capital_client import PRICE_DECIMALS as _PD
            _dec = _PD.get(epic, 5)
        except ImportError:
            _dec = 5

        try:
            if direction.upper() == "BUY":
                result = self._run_async(
                    self._connection.create_market_buy_order(
                        symbol=symbol,
                        volume=round(size, 2),
                        stop_loss=round(sl_price, _dec),
                        take_profit=round(tp_price, _dec),
                        options={"comment": f"Nemesis {epic}"},
                    ),
                    timeout=15,
                )
            else:
                result = self._run_async(
                    self._connection.create_market_sell_order(
                        symbol=symbol,
                        volume=round(size, 2),
                        stop_loss=round(sl_price, _dec),
                        take_profit=round(tp_price, _dec),
                        options={"comment": f"Nemesis {epic}"},
                    ),
                    timeout=15,
                )

            if not result:
                logger.error(f"❌ MT5 place_order {epic}: résultat vide")
                return None

            # Check result
            string_code = result.get("stringCode", "")
            if string_code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
                order_id = result.get("orderId", result.get("positionId", ""))
                logger.info(
                    f"✅ MT5 {direction} {epic} ({symbol}) size={size} | "
                    f"orderId={order_id}"
                )
                return str(order_id)

            logger.error(
                f"❌ MT5 place_order {epic}: {string_code} — "
                f"{result.get('message', result)}"
            )
            return None

        except Exception as e:
            logger.error(f"❌ MT5 place_order {epic}: {e}")
            return None

    def close_partial(self, epic: str, direction: str, partial_size: float) -> bool:
        """
        Ferme partiellement une position MT5.
        Utilisé pour TP1 (40%) et TP2 (40%) du protocole Multi-TP.
        MetaApi v29 : close_position_partially(position_id, volume, options).
        """
        if not self.available:
            return False
        symbol = SYMBOL_MAP.get(epic, epic)
        try:
            # Trouver la position ouverte correspondant au symbole
            positions = getattr(self._connection, "terminal_state", None)
            positions = getattr(positions, "positions", []) if positions else []
            target_pos = None
            for pos in positions:
                if pos.get("symbol", "") == symbol:
                    target_pos = pos
                    break
            if not target_pos:
                logger.warning(f"⚠️ MT5 close_partial {epic}: aucune position ouverte pour {symbol}")
                return False

            position_id = str(target_pos.get("id", ""))
            if not position_id:
                return False

            vol = round(partial_size, 2)
            result = self._run_async(
                self._connection.close_position_partially(
                    position_id,
                    vol,
                    {"comment": f"Nemesis partial {epic}"},
                ),
                timeout=15,
            )
            if result:
                string_code = result.get("stringCode", "")
                if string_code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
                    logger.info(f"✅ MT5 partial close {epic} ({symbol}) vol={vol} | code={string_code}")
                    return True
                logger.warning(f"⚠️ MT5 partial close {epic}: {string_code} — {result.get('message', result)}")
            return False
        except Exception as e:
            logger.error(f"❌ MT5 close_partial {epic}: {e}")
            return False

    def update_position(self, deal_id: str, stop_level: float = None, limit_level: float = None, epic: str = "") -> bool:
        """Modifie le SL/TP d'une position ouverte (trailing stop)."""
        if not self.available:
            return False
        try:
            try:
                from brokers.capital_client import PRICE_DECIMALS as _PD
                _dec = _PD.get(epic, 5) if epic else 5
            except ImportError:
                _dec = 5
            options = {}
            if stop_level is not None:
                options["stopLoss"] = round(stop_level, _dec)
            if limit_level is not None:
                options["takeProfit"] = round(limit_level, _dec)
            if not options:
                return True

            result = self._run_async(
                self._connection.modify_position(deal_id, **options),
                timeout=10,
            )
            if result:
                string_code = result.get("stringCode", "")
                if string_code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
                    return True
                logger.debug(f"MT5 update_position {deal_id}: {string_code}")
            return False
        except Exception as e:
            logger.debug(f"MT5 update_position {deal_id}: {e}")
            return False

    def close_position(self, deal_id: str) -> bool:
        """Ferme une position ouverte par son ID."""
        if not self.available:
            return False
        try:
            result = self._run_async(
                self._connection.close_position(deal_id),
                timeout=15,
            )
            if result:
                string_code = result.get("stringCode", "")
                if string_code in ("TRADE_RETCODE_DONE", "TRADE_RETCODE_PLACED"):
                    logger.info(f"✅ MT5 position {deal_id} fermée")
                    return True
                logger.error(f"❌ MT5 close {deal_id}: {string_code}")
            return False
        except Exception as e:
            logger.error(f"❌ MT5 close_position {deal_id}: {e}")
            return False

    def get_open_positions(self) -> List[dict]:
        """Retourne la liste des positions ouvertes sur MT5."""
        if not self.available:
            return []
        try:
            positions = (self._connection.terminal_state.positions if hasattr(self._connection, "terminal_state") and self._connection.terminal_state else [])
            if not positions:
                return []

            result = []
            for pos in positions:
                mt5_symbol = pos.get("symbol", "")
                epic = REVERSE_SYMBOL_MAP.get(mt5_symbol, mt5_symbol)
                result.append({
                    "position": {
                        "dealId":    str(pos.get("id", "")),
                        "direction": pos.get("type", "POSITION_TYPE_BUY").replace(
                            "POSITION_TYPE_", ""
                        ),
                        "level":     pos.get("openPrice", 0),
                        "size":      pos.get("volume", 0),
                        "stopLevel": pos.get("stopLoss", 0),
                        "limitLevel": pos.get("takeProfit", 0),
                    },
                    "market": {
                        "epic": epic,
                    },
                })
            return result
        except Exception as e:
            logger.error(f"❌ MT5 get_positions: {e}")
            return []

    # ─── Recherche de marchés (debug) ────────────────────────────────────────

    def search_markets(self, term: str, limit: int = 5) -> list:
        """Recherche un symbole sur MT5."""
        if not self.available:
            return []
        try:
            symbols = self._run_async(
                self._connection.get_symbols()
            )
            if not symbols:
                return []
            term_lower = term.lower()
            matches = [
                {"epic": s.get("symbol", ""), "name": s.get("description", "")}
                for s in symbols
                if term_lower in s.get("symbol", "").lower()
                   or term_lower in s.get("description", "").lower()
            ]
            return matches[:limit]
        except Exception as e:
            logger.debug(f"MT5 search_markets({term}): {e}")
            return []

    def shutdown(self):
        """Clean shutdown of the MetaApi connection."""
        if self._connection:
            try:
                self._run_async(self._connection.close())
            except Exception:
                pass
        if self._api:
            try:
                self._run_async(self._api.close())
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("🔌 MT5 MetaApi déconnecté")
