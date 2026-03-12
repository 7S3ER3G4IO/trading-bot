"""
market_maker.py — Moteur 15 : High-Frequency Market Making (Avellaneda-Stoikov).

Modèle mathématique de tenue de marché optimal:
  Avellaneda & Stoikov, "High-frequency trading in a limit order book" (2008).

Problème: maximiser l'utilité espérée des profits tout en gérant le risque d'inventaire.

Solution: placer des ordres bid/ask autour d'un "prix de réserve" qui tient compte
de la position d'inventaire actuelle.

Prix de réserve:
  r(s, t) = s - q × γ × σ² × (T - t)

  où:
    s   = prix mid actuel
    q   = inventaire actuel (positif = long, négatif = short)
    γ   = coefficient d'aversion au risque (0.1 = modéré)
    σ²  = variance du prix (ATR²)
    T-t = temps restant dans la session

Spread optimal:
  δ* = γ × σ² × (T - t) + (2/γ) × ln(1 + γ/κ)

  où κ = intensité d'arrivée des ordres (paramètre de la distribution de Poisson)

Skew d'inventaire:
  - Si inventaire > max_inventory: baisse le bid (décourage nouveaux achats),
    monte l'ask (encourage les ventes)
  - Si inventaire < -max_inventory: monte le bid, baisse l'ask

Usage:
    mm = MarketMaker(capital_client, db)
    mm.start()  # daemon thread, quotes refresh toutes les 5s

    quotes = mm.get_quotes("EURUSD")
    # → {"bid": 1.0850, "ask": 1.0860, "mid": 1.0855, "spread": 0.0010}
"""
import math
import time
import threading
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone
from loguru import logger

# ─── Paramètres Avellaneda-Stoikov ────────────────────────────────────────────
_GAMMA        = 0.10   # Aversion au risque (0=neutre, 1=très risk-averse)
_KAPPA        = 1.50   # Intensité d'arrivée des ordres (ordres/seconde normalisé)
_T_HORIZON    = 3600   # Horizon de temps (1 heure en secondes)
_SIGMA_DEFAULT = 0.001 # Volatilité par défaut si ATR non disponible (0.1%)
_MIN_SPREAD   = 0.0002  # Spread minimum absolu (2 bps)
_MAX_SPREAD   = 0.0050  # Spread maximum (50 bps)
_QUOTE_SIZE   = 0.10   # Taille de chaque ordre de MM (lots)
_MAX_INVENTORY = 2.0   # Inventaire max avant skew maximum
_REFRESH_S    = 5.0    # Refresh des quotes toutes les 5s

# ─── Instruments autorisés au Market Making ──────────────────────────────────
_MM_INSTRUMENTS = ["EURUSD", "GBPUSD", "USDJPY", "GOLD"]   # Spread naturellement large


class InventoryManager:
    """Suit l'inventaire du Market Maker par instrument."""

    def __init__(self):
        self._inventory: Dict[str, float] = {}   # {instrument: position_size}
        self._avg_cost:  Dict[str, float] = {}   # {instrument: avg_entry_price}
        self._lock = threading.Lock()

    def update(self, instrument: str, size: float, price: float, direction: str):
        with self._lock:
            sign = 1 if direction == "BUY" else -1
            old_inv  = self._inventory.get(instrument, 0.0)
            new_inv  = old_inv + sign * size
            old_cost = self._avg_cost.get(instrument, price)

            # Weighted average cost
            if abs(new_inv) > 0:
                total_val = old_cost * abs(old_inv) + price * size
                self._avg_cost[instrument] = total_val / (abs(old_inv) + size)

            self._inventory[instrument] = round(new_inv, 4)

    def get(self, instrument: str) -> float:
        with self._lock:
            return self._inventory.get(instrument, 0.0)

    def get_pnl(self, instrument: str, current_price: float) -> float:
        with self._lock:
            inv  = self._inventory.get(instrument, 0.0)
            cost = self._avg_cost.get(instrument, current_price)
            return round(inv * (current_price - cost), 4)

    def all(self) -> dict:
        with self._lock:
            return dict(self._inventory)


class AvellanedaStoikov:
    """
    Calcul du prix de réserve et du spread optimal selon Avellaneda-Stoikov.
    """

    @staticmethod
    def reservation_price(mid_price: float, inventory: float,
                           sigma: float, gamma: float = _GAMMA,
                           t_remaining: float = _T_HORIZON) -> float:
        """
        r = s - q × γ × σ² × (T - t)
        Ajuste le prix de référence selon l'inventaire pour inciter à l'équilibrage.
        """
        return mid_price - inventory * gamma * (sigma ** 2) * t_remaining

    @staticmethod
    def optimal_spread(sigma: float, gamma: float = _GAMMA,
                        kappa: float = _KAPPA,
                        t_remaining: float = _T_HORIZON) -> float:
        """
        δ* = γ × σ² × (T - t) + (2/γ) × ln(1 + γ/κ)
        Plus la volatilité et le risk sont élevés, plus le spread est large.
        """
        risk_term  = gamma * (sigma ** 2) * t_remaining
        avoidance  = (2 / gamma) * math.log(1 + gamma / kappa) if gamma > 0 else 0
        spread = risk_term + avoidance
        return max(_MIN_SPREAD, min(_MAX_SPREAD, spread))

    @staticmethod
    def skew_quotes(bid: float, ask: float, inventory: float,
                     max_inventory: float = _MAX_INVENTORY) -> Tuple[float, float]:
        """
        Applique le skew d'inventaire pour inciter à l'équilibrage.
        Si inventory > 0 (long), on veut vendre → monte l'ask, baisse le bid.
        """
        skew_factor = max(-1.0, min(1.0, inventory / max_inventory))
        spread = ask - bid
        mid    = (ask + bid) / 2

        # Décale le milieu vers la direction opposée à l'inventaire
        shifted_mid = mid - skew_factor * spread * 0.5

        new_bid = shifted_mid - spread / 2
        new_ask = shifted_mid + spread / 2

        return round(new_bid, 6), round(new_ask, 6)


class MarketMaker:
    """
    Market Maker haute fréquence: Avellaneda-Stoikov + gestion inventaire.
    Gère simulatneément plusieurs instruments.
    Enregistre ses quotes dans Supabase pour analyse.
    """

    def __init__(self, capital_client=None, db=None, telegram_router=None,
                 ohlcv_cache=None):
        self._capital  = capital_client
        self._db       = db
        self._tg       = telegram_router
        self._cache    = ohlcv_cache

        self.inventory = InventoryManager()
        self._quotes:  Dict[str, dict] = {}   # {instrument: {bid, ask, mid, spread}}
        self._lock     = threading.Lock()

        # Stats
        self._quotes_placed   = 0
        self._quotes_filled   = 0
        self._total_spread_pnl = 0.0
        self._pnl_inventory   = 0.0

        self._running = False
        self._thread  = None

        self._ensure_table()

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._quote_loop, daemon=True, name="market_maker"
        )
        self._thread.start()
        logger.info(f"📊 Market Maker démarré (Avellaneda-Stoikov) | {len(_MM_INSTRUMENTS)} instruments")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_quotes(self, instrument: str) -> Optional[dict]:
        """Retourne les quotes actuelles pour un instrument."""
        with self._lock:
            return dict(self._quotes.get(instrument, {}))

    def on_fill(self, instrument: str, side: str, price: float, size: float):
        """Appelé quand un de nos orders de MM est exécuté."""
        self.inventory.update(instrument, size, price, side)
        self._quotes_filled += 1
        self._total_spread_pnl += self._estimate_spread_capture(instrument, price, size, side)

    def stats(self) -> dict:
        return {
            "quotes_placed": self._quotes_placed,
            "quotes_filled": self._quotes_filled,
            "spread_pnl":    round(self._total_spread_pnl, 4),
            "inventory":     self.inventory.all(),
        }

    def format_report(self) -> str:
        inv = self.inventory.all()
        inv_str = "\n".join(
            f"  {inst}: {q:+.3f}" for inst, q in inv.items() if abs(q) > 0.001
        ) or "  Inventaire neutre"
        return (
            f"📊 <b>Market Maker (Avellaneda-Stoikov)</b>\n\n"
            f"  Quotes placées: {self._quotes_placed}\n"
            f"  Fills: {self._quotes_filled}\n"
            f"  Spread PnL estimé: {self._total_spread_pnl:+.4f}\n\n"
            f"  <b>Inventaire:</b>\n{inv_str}"
        )

    # ─── Quote Loop ──────────────────────────────────────────────────────────

    def _quote_loop(self):
        session_start = time.monotonic()
        while self._running:
            t_elapsed   = time.monotonic() - session_start
            t_remaining = max(60, _T_HORIZON - t_elapsed)

            for instrument in _MM_INSTRUMENTS:
                try:
                    self._refresh_quotes(instrument, t_remaining)
                except Exception as e:
                    logger.debug(f"MM quote {instrument}: {e}")
                time.sleep(0.1)

            time.sleep(_REFRESH_S)

    def _refresh_quotes(self, instrument: str, t_remaining: float):
        """Calcule et (si possible) place de nouveaux quotes bid/ask."""
        mid = self._get_mid_price(instrument)
        if mid is None or mid <= 0:
            return

        sigma = self._get_sigma(instrument)
        inv   = self.inventory.get(instrument)

        # Avellaneda-Stoikov
        r     = AvellanedaStoikov.reservation_price(mid, inv, sigma, t_remaining=t_remaining)
        delta = AvellanedaStoikov.optimal_spread(sigma, t_remaining=t_remaining)

        raw_bid = r - delta / 2
        raw_ask = r + delta / 2

        # Inventory skew
        bid, ask = AvellanedaStoikov.skew_quotes(raw_bid, raw_ask, inv)

        quote = {
            "mid":   round(mid, 6),
            "bid":   round(bid, 6),
            "ask":   round(ask, 6),
            "spread": round(ask - bid, 6),
            "spread_pct": round((ask - bid) / mid, 6),
            "reservation_price": round(r, 6),
            "inventory": round(inv, 4),
            "sigma": round(sigma, 6),
            "t_remaining": round(t_remaining),
            "ts": datetime.now(timezone.utc).isoformat(),
        }

        with self._lock:
            self._quotes[instrument] = quote

        self._quotes_placed += 1
        logger.debug(
            f"📊 MM {instrument}: bid={bid:.5f} ask={ask:.5f} "
            f"spread={quote['spread_pct']:.4%} inv={inv:+.2f}"
        )

        # Place les ordres si client disponible et inventaire sous contrôle
        if self._capital and abs(inv) < _MAX_INVENTORY * 2:
            self._place_quotes(instrument, bid, ask)

        self._save_quote_async(instrument, quote)

    def _place_quotes(self, instrument: str, bid: float, ask: float):
        """Place les deux orders limit (bid et ask) simultanément."""
        # Note: Capital.com CFD ne supporte pas les pending limit orders de la même façon
        # En production réelle, utiliser un exchange spot avec l'API limit order
        # Cette fonction est prête pour l'intégration avec Binance/Bybit
        pass   # Stub — override dans sous-classes exchange-specific

    def _estimate_spread_capture(self, instrument: str, price: float,
                                  size: float, side: str) -> float:
        """Estime le profit capturé sur le spread pour ce fill."""
        quote = self._quotes.get(instrument, {})
        if not quote:
            return 0.0
        spread = quote.get("spread", 0.0)
        return size * spread * price / 2

    # ─── Price/Vol Data ──────────────────────────────────────────────────────

    def _get_mid_price(self, instrument: str) -> Optional[float]:
        if not self._capital:
            return None
        try:
            px = self._capital.get_current_price(instrument)
            if px:
                return px.get("mid", None)
        except Exception:
            return None

    def _get_sigma(self, instrument: str) -> float:
        """Σ basé sur l'ATR normalisé."""
        try:
            if self._cache:
                df = self._cache.get(instrument)
                if df is not None and "atr" in df.columns and len(df) > 1:
                    mid = df["close"].iloc[-1]
                    if mid > 0:
                        return float(df["atr"].iloc[-1]) / mid
        except Exception:
            pass
        return _SIGMA_DEFAULT

    # ─── DB ──────────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS market_maker_quotes (
                    id          SERIAL PRIMARY KEY,
                    instrument  VARCHAR(20),
                    mid         DOUBLE PRECISION,
                    bid         DOUBLE PRECISION,
                    ask         DOUBLE PRECISION,
                    spread_pct  DOUBLE PRECISION,
                    reservation DOUBLE PRECISION,
                    inventory   DOUBLE PRECISION,
                    sigma       DOUBLE PRECISION,
                    quoted_at   TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"market_maker_quotes table: {e}")

    def _save_quote_async(self, instrument: str, quote: dict):
        if not self._db:
            return
        self._db.async_write(self._save_quote_sync, instrument, quote)

    def _save_quote_sync(self, instrument, q):
        try:
            if not getattr(self._db, '_pg', False):
                return
            ph = "%s"
            self._db._execute(
                f"INSERT INTO market_maker_quotes "
                f"(instrument,mid,bid,ask,spread_pct,reservation,inventory,sigma) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (instrument, q["mid"], q["bid"], q["ask"],
                 q["spread_pct"], q["reservation_price"],
                 q["inventory"], q["sigma"])
            )
        except Exception as e:
            logger.debug(f"MM quote save: {e}")
