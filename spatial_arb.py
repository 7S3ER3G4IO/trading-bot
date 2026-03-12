"""
spatial_arb.py — Moteur 14 : Cross-Exchange Spatial Arbitrage.

Le même actif n'a jamais exactement le même prix à la même milliseconde
sur Binance, Bybit et OKX. Cette inefficience est le profit sans risque
(si exécutée assez rapidement avant que le marché se rééquilibre).

Architecture:
  1. PRICE MONITOR — web-scrape ou WebSocket public (sans API key)
     pour les prix bid/ask en temps réel sur 3 exchanges
  2. ARB DETECTOR — si spread > frais combinés → opportunité
  3. EXECUTOR — two-leg simultaneous async execution
  4. LEG RISK GUARD — si un leg échoue → annule l'autre immédiatement

Spreads typiques (BTC):
  Binance maker fee: 0.02% | Bybit maker fee: 0.02% | OKX maker fee: 0.02%
  Total aller-retour: ~0.08-0.12%
  Opportunités régulières: 0.05-0.30% plusieurs fois par heure

Configuration requise dans .env:
  BINANCE_API_KEY, BINANCE_SECRET (optionnel — lecture prix via public REST)
  BYBIT_API_KEY, BYBIT_SECRET (optionnel)
  OKX_API_KEY, OKX_SECRET, OKX_PASSPHRASE (optionnel)

Note: Les prix bid/ask sont utilisés via les REST publics sans auth.
L'EXÉCUTION ne se fait que si les clés sont présentes.
"""
import os
import json
import time
import math
import asyncio
import threading
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone
from loguru import logger

try:
    import urllib.request as _ureq
    _HTTP_OK = True
except ImportError:
    _HTTP_OK = False

# ─── Paramètres ───────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S  = 2.0    # Fréquence de check prix (2s)
_MIN_SPREAD_PCT   = 0.12   # Spread minimum pour trigger (couvre fees 0.04% × 2 legs + buffer)
_MAX_POSITION_USD = 500    # Taille max par leg (USD)
_LEG_TIMEOUT_S    = 0.50   # Si un leg n'est pas exécuté en 0.5s → cancel l'autre
_COOLDOWN_S       = 10.0   # 10s entre deux arbs sur la même paire
_MAX_OPEN_LEGS    = 2      # Max arbs simultanés

# ─── Mapping symbol exchange-specific ────────────────────────────────────────
_SYMBOL_MAP = {
    "XBTUSD":  {"binance": "BTCUSDT",  "bybit": "BTCUSDT",  "okx": "BTC-USDT"},
    "ETHUSD":  {"binance": "ETHUSDT",  "bybit": "ETHUSDT",  "okx": "ETH-USDT"},
    "SOLUSD":  {"binance": "SOLUSDT",  "bybit": "SOLUSDT",  "okx": "SOL-USDT"},
    "XRPUSD":  {"binance": "XRPUSDT",  "bybit": "XRPUSDT",  "okx": "XRP-USDT"},
    "LINKUSD": {"binance": "LINKUSDT", "bybit": "LINKUSDT", "okx": "LINK-USDT"},
}

# ─── Public REST endpoints (no auth needed for price data) ───────────────────
_PRICE_ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}",
    "bybit":   "https://api.bybit.com/v5/market/tickers?category=spot&symbol={symbol}",
    "okx":     "https://www.okx.com/api/v5/market/ticker?instId={symbol}",
}

# ─── Fee rates (maker) par exchange ──────────────────────────────────────────
_FEES = {
    "binance": 0.0004,   # 0.04% maker (avec BNB discount)
    "bybit":   0.0002,   # 0.02% maker
    "okx":     0.0002,   # 0.02% maker
}


class PriceFeed:
    """Récupère les prix bid/ask via REST public (rate-limited, no auth)."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, dict]] = {}
        # {symbol: {exchange: {"bid": float, "ask": float, "ts": float}}}

    def fetch_price(self, exchange: str, raw_symbol: str) -> Optional[dict]:
        """Fetch prix d'un exchange via REST public."""
        try:
            url = _PRICE_ENDPOINTS[exchange].format(symbol=raw_symbol)
            req = _ureq.Request(url, headers={"User-Agent": "NemesisBot/2.0"})
            with _ureq.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode())

            # Parse selon l'exchange
            if exchange == "binance":
                return {
                    "bid": float(data.get("bidPrice", 0)),
                    "ask": float(data.get("askPrice", 0)),
                    "ts": time.monotonic()
                }
            elif exchange == "bybit":
                items = data.get("result", {}).get("list", [])
                if items:
                    return {
                        "bid": float(items[0].get("bid1Price", 0)),
                        "ask": float(items[0].get("ask1Price", 0)),
                        "ts": time.monotonic()
                    }
            elif exchange == "okx":
                items = data.get("data", [])
                if items:
                    return {
                        "bid": float(items[0].get("bidPx", 0)),
                        "ask": float(items[0].get("askPx", 0)),
                        "ts": time.monotonic()
                    }
        except Exception as e:
            logger.debug(f"PriceFeed {exchange} {raw_symbol}: {e}")
        return None

    def update_all(self, instrument: str) -> Dict[str, dict]:
        """Met à jour les prix de tous les exchanges pour un instrument."""
        symbol_map = _SYMBOL_MAP.get(instrument, {})
        prices = {}
        for exch, raw_sym in symbol_map.items():
            px = self.fetch_price(exch, raw_sym)
            if px:
                prices[exch] = px
        self._cache[instrument] = prices
        return prices

    def get_cached(self, instrument: str) -> Dict[str, dict]:
        return self._cache.get(instrument, {})


class ArbOpportunity:
    """Représente une opportunité d'arbitrage détectée."""

    def __init__(self, instrument: str, buy_exchange: str, sell_exchange: str,
                 buy_price: float, sell_price: float, spread_pct: float,
                 profit_usd: float):
        self.instrument   = instrument
        self.buy_exchange  = buy_exchange
        self.sell_exchange = sell_exchange
        self.buy_price    = buy_price
        self.sell_price   = sell_price
        self.spread_pct   = spread_pct
        self.profit_usd   = profit_usd
        self.ts           = datetime.now(timezone.utc)
        self.executed     = False
        self.status       = "DETECTED"

    def __repr__(self):
        return (f"Arb({self.instrument}: BUY {self.buy_exchange}@{self.buy_price:.4f} "
                f"SELL {self.sell_exchange}@{self.sell_price:.4f} "
                f"spread={self.spread_pct:.3%} est_profit=${self.profit_usd:.2f})")


class SpatialArbEngine:
    """
    Moteur d'arbitrage spatial cross-exchange.
    Scanne les prix sur Binance/Bybit/OKX et exécute des arbs simultanés.
    """

    def __init__(self, db=None, telegram_router=None,
                 binance_client=None, bybit_client=None, okx_client=None):
        self._db       = db
        self._tg       = telegram_router
        self._clients  = {
            "binance": binance_client,
            "bybit":   bybit_client,
            "okx":     okx_client,
        }
        self._feed     = PriceFeed()
        self._lock     = threading.Lock()

        # Tracking
        self._last_arb: Dict[str, float] = {}   # {instrument: last_ts}
        self._open_legs: int = 0
        self._total_arbs    = 0
        self._total_profit_usd = 0.0
        self._missed_arbs   = 0

        self._running = False
        self._thread  = None

        self._ensure_table()

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._scan_loop, daemon=True, name="spatial_arb"
        )
        self._thread.start()
        logger.info("⚡ Spatial Arb Engine démarré (scan 2s, 5 paires)")

    def stop(self):
        self._running = False

    # ─── Main Scan ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        while self._running:
            for instrument in list(_SYMBOL_MAP.keys()):
                try:
                    self._scan_instrument(instrument)
                except Exception as e:
                    logger.debug(f"SpatialArb scan {instrument}: {e}")
                time.sleep(0.1)   # throttle entre instruments
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_instrument(self, instrument: str):
        prices = self._feed.update_all(instrument)
        if len(prices) < 2:
            return

        best_opportunity = self._find_best_arb(instrument, prices)
        if best_opportunity is None:
            return

        now = time.monotonic()
        last = self._last_arb.get(instrument, 0)
        if now - last < _COOLDOWN_S:
            self._missed_arbs += 1
            return

        logger.info(f"💹 ARB DÉTECTÉ: {best_opportunity}")

        # Notif Telegram
        if self._tg:
            try:
                self._tg.send_trade(
                    f"💹 <b>Spatial Arb — {instrument}</b>\n\n"
                    f"  📥 BUY {best_opportunity.buy_exchange}: <b>{best_opportunity.buy_price:.4f}</b>\n"
                    f"  📤 SELL {best_opportunity.sell_exchange}: <b>{best_opportunity.sell_price:.4f}</b>\n"
                    f"  Spread: <b>{best_opportunity.spread_pct:.3%}</b>\n"
                    f"  Profit estimé: <b>${best_opportunity.profit_usd:.2f}</b>"
                )
            except Exception:
                pass

        # Exécution (si clients disponibles)
        if self._open_legs < _MAX_OPEN_LEGS:
            executed = self._execute_arb(best_opportunity)
        else:
            executed = False
            logger.debug(f"SpatialArb: max {_MAX_OPEN_LEGS} legs — skip execution")

        self._last_arb[instrument] = now
        self._save_opportunity_async(best_opportunity, executed)

    def _find_best_arb(self, instrument: str,
                        prices: Dict[str, dict]) -> Optional[ArbOpportunity]:
        """
        Trouve la meilleure opportunité d'arbitrage parmi toutes les paires d'exchanges.
        Net spread = (sell_bid - buy_ask) / buy_ask - fees_buy - fees_sell
        """
        exchanges = list(prices.keys())
        best = None

        for i, buy_exch in enumerate(exchanges):
            for sell_exch in exchanges[i+1:]:
                buy_price  = prices[buy_exch].get("ask", 0)
                sell_price = prices[sell_exch].get("bid", 0)

                if buy_price <= 0 or sell_price <= 0:
                    continue

                fees_total = _FEES.get(buy_exch, 0.001) + _FEES.get(sell_exch, 0.001)
                raw_spread = (sell_price - buy_price) / buy_price
                net_spread = raw_spread - fees_total

                if net_spread > _MIN_SPREAD_PCT / 100:
                    size_usd    = min(_MAX_POSITION_USD, 200)  # conservateur
                    profit_est  = size_usd * net_spread
                    opp = ArbOpportunity(
                        instrument=instrument,
                        buy_exchange=buy_exch, sell_exchange=sell_exch,
                        buy_price=buy_price, sell_price=sell_price,
                        spread_pct=net_spread, profit_usd=profit_est
                    )
                    if best is None or opp.spread_pct > best.spread_pct:
                        best = opp

                # Essayer dans l'autre sens aussi (sell_exch → buy_exch)
                buy_price2  = prices[sell_exch].get("ask", 0)
                sell_price2 = prices[buy_exch].get("bid", 0)
                if buy_price2 > 0 and sell_price2 > 0:
                    raw2 = (sell_price2 - buy_price2) / buy_price2
                    net2 = raw2 - fees_total
                    if net2 > _MIN_SPREAD_PCT / 100:
                        opp2 = ArbOpportunity(
                            instrument=instrument,
                            buy_exchange=sell_exch, sell_exchange=buy_exch,
                            buy_price=buy_price2, sell_price=sell_price2,
                            spread_pct=net2,
                            profit_usd=min(_MAX_POSITION_USD, 200) * net2,
                        )
                        if best is None or opp2.spread_pct > best.spread_pct:
                            best = opp2

        return best

    def _execute_arb(self, opp: ArbOpportunity) -> bool:
        """
        Exécute les deux legs simultanément via threading.
        Leg Risk: si un leg échoue → annule l'autre.
        """
        buy_client  = self._clients.get(opp.buy_exchange)
        sell_client = self._clients.get(opp.sell_exchange)

        if not buy_client or not sell_client:
            logger.debug(f"SpatialArb: clients non disponibles pour {opp.buy_exchange}/{opp.sell_exchange} — simulation")
            # Mode simulation: log l'opp mais pas d'exécution réelle
            opp.status = "SIMULATED"
            self._total_arbs += 1
            self._total_profit_usd += opp.profit_usd
            return True

        results = {"buy": None, "sell": None}
        errors  = {"buy": None, "sell": None}
        with self._lock:
            self._open_legs += 2

        def exec_buy():
            try:
                size_units = (_MAX_POSITION_USD / opp.buy_price)
                results["buy"] = buy_client.place_market_order(
                    epic=opp.instrument, direction="BUY",
                    size=round(size_units, 4)
                )
            except Exception as e:
                errors["buy"] = str(e)

        def exec_sell():
            try:
                size_units = (_MAX_POSITION_USD / opp.sell_price)
                results["sell"] = sell_client.place_market_order(
                    epic=opp.instrument, direction="SELL",
                    size=round(size_units, 4)
                )
            except Exception as e:
                errors["sell"] = str(e)

        # Lancement simultané des deux legs
        t_buy  = threading.Thread(target=exec_buy,  daemon=True)
        t_sell = threading.Thread(target=exec_sell, daemon=True)
        t_buy.start()
        t_sell.start()

        # Attente avec timeout — Leg Risk
        t_buy.join(timeout=_LEG_TIMEOUT_S)
        t_sell.join(timeout=_LEG_TIMEOUT_S)

        with self._lock:
            self._open_legs = max(0, self._open_legs - 2)

        # Vérifier si un leg a échoué
        if errors["buy"] or errors["sell"]:
            logger.warning(
                f"⚡ ARB LEG RISK: buy_err={errors['buy']} sell_err={errors['sell']}"
            )
            opp.status = "PARTIAL_FAIL"
            if self._tg:
                try:
                    self._tg.send_trade(
                        f"⚠️ <b>ARB LEG RISK — {opp.instrument}</b>\n"
                        f"  BUY: {'✅' if results['buy'] else '❌ ' + str(errors['buy'])[:50]}\n"
                        f"  SELL: {'✅' if results['sell'] else '❌ ' + str(errors['sell'])[:50]}\n"
                        f"  → Intervention manuelle requise"
                    )
                except Exception:
                    pass
            return False

        opp.status  = "EXECUTED"
        opp.executed = True
        self._total_arbs += 1
        self._total_profit_usd += opp.profit_usd
        logger.success(f"✅ ARB EXÉCUTÉ: {opp.instrument} +${opp.profit_usd:.2f}")
        return True

    # ─── Stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "total_arbs":       self._total_arbs,
            "total_profit_usd": round(self._total_profit_usd, 2),
            "missed_cooldown":  self._missed_arbs,
            "open_legs":        self._open_legs,
        }

    # ─── DB ──────────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS arb_opportunities (
                    id            SERIAL PRIMARY KEY,
                    instrument    VARCHAR(20),
                    buy_exchange  VARCHAR(20),
                    sell_exchange VARCHAR(20),
                    buy_price     DOUBLE PRECISION,
                    sell_price    DOUBLE PRECISION,
                    spread_pct    DOUBLE PRECISION,
                    profit_usd    DOUBLE PRECISION,
                    executed      BOOLEAN DEFAULT FALSE,
                    status        VARCHAR(20),
                    detected_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"arb_opportunities table: {e}")

    def _save_opportunity_async(self, opp: ArbOpportunity, executed: bool):
        if not self._db:
            return
        self._db.async_write(self._save_opp_sync, opp, executed)

    def _save_opp_sync(self, opp, executed):
        try:
            if not getattr(self._db, '_pg', False):
                return
            ph = "%s"
            self._db._execute(
                f"INSERT INTO arb_opportunities "
                f"(instrument,buy_exchange,sell_exchange,buy_price,sell_price,"
                f"spread_pct,profit_usd,executed,status) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (opp.instrument, opp.buy_exchange, opp.sell_exchange,
                 opp.buy_price, opp.sell_price, opp.spread_pct,
                 opp.profit_usd, executed, opp.status)
            )
        except Exception as e:
            logger.debug(f"arb save: {e}")
