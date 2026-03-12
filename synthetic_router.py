"""
synthetic_router.py — Moteur 28 : Synthetic Pair Routing & Triangular Arbitrage

Calcule en temps réel si passer par une paire intermédiaire est moins cher
que l'accès direct. Si le coût synthétique (2 ordres) est inférieur au
spread direct, le bot exécute la route synthétique pour créer sa propre liquidité.

Architecture :
  PairGraph        → graphe des paires tradables et leurs spreads
  RouteCalculator  → Dijkstra modifié pour trouver la route optimale
  TriangularArb    → détecte les opportunités de triangular arbitrage
  SyntheticRouter  → exécute les ordres simultanés sur la route optimale

Exemple :
  Route DOGE/EUR :
    Direct : DOGEEUR spread 1.2%
    Synthétique : EUR→USDT (0.05%) + USDT→DOGE (0.15%) = 0.20% total
    → La route synthétique est 6× moins chère → on l'utilise.
"""
import time
import threading
import math
from typing import Dict, Optional, List, Tuple, Set
from datetime import datetime, timezone, timedelta
from loguru import logger
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S        = 30       # Scan toutes les 30s
_MIN_PROFIT_BPS         = 5        # Profit minimum en bps pour exécution
_MAX_ROUTE_LEGS         = 3        # Max 3 legs par route
_SPREAD_COST_MULT       = 1.5      # Multiplicateur de spread pour sécurité
_EXECUTION_SLIPPAGE_BPS = 3        # Slippage estimé par leg (bps)

# Paires tradables sur Capital.com avec leurs groupes de devise
_PAIR_GRAPH = {
    # Forex majors — tous tradables directement
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"), "USDCHF": ("USD", "CHF"),
    "AUDUSD": ("AUD", "USD"), "NZDUSD": ("NZD", "USD"),
    "USDCAD": ("USD", "CAD"),
    # Forex crosses
    "EURJPY": ("EUR", "JPY"), "GBPJPY": ("GBP", "JPY"),
    "EURGBP": ("EUR", "GBP"), "AUDJPY": ("AUD", "JPY"),
    "AUDCAD": ("AUD", "CAD"), "AUDNZD": ("AUD", "NZD"),
    "NZDJPY": ("NZD", "JPY"), "CHFJPY": ("CHF", "JPY"),
    "CADCHF": ("CAD", "CHF"), "CADJPY": ("CAD", "JPY"),
    "NZDCAD": ("NZD", "CAD"),
    "GBPCHF": ("GBP", "CHF"), "GBPCAD": ("GBP", "CAD"),
    # Crypto (base/USD)
    "BTCUSD": ("BTC", "USD"), "ETHUSD": ("ETH", "USD"),
}

# Spreads typiques en bps (basis points)
_TYPICAL_SPREADS = {
    "EURUSD": 1.5, "GBPUSD": 2.0, "USDJPY": 1.5, "USDCHF": 2.5,
    "AUDUSD": 2.0, "NZDUSD": 3.0, "USDCAD": 2.5,
    "EURJPY": 3.0, "GBPJPY": 5.0, "EURGBP": 2.5, "AUDJPY": 4.0,
    "AUDCAD": 4.0, "AUDNZD": 5.0, "NZDJPY": 5.0, "CHFJPY": 4.0,
    "CADCHF": 5.0, "CADJPY": 4.0, "NZDCAD": 5.0,
    "GBPCHF": 5.0, "GBPCAD": 5.0,
    "BTCUSD": 30, "ETHUSD": 25,
}


class Route:
    """Représente une route de trading (directe ou synthétique)."""

    def __init__(self, legs: List[Tuple[str, str]], total_cost_bps: float,
                 is_synthetic: bool = False):
        self.legs = legs               # [(pair, direction="BUY"/"SELL"), ...]
        self.total_cost_bps = total_cost_bps
        self.is_synthetic = is_synthetic
        self.n_legs = len(legs)

    def __repr__(self):
        path = " → ".join(f"{p}({d})" for p, d in self.legs)
        return f"Route({path} | cost={self.total_cost_bps:.1f}bps)"


class TriangularOpportunity:
    """Opportunité d'arbitrage triangulaire détectée."""

    def __init__(self, legs: List[Tuple[str, str, float]],
                 profit_bps: float, currencies: List[str]):
        self.legs = legs              # [(pair, direction, rate), ...]
        self.profit_bps = profit_bps
        self.currencies = currencies
        self.timestamp = datetime.now(timezone.utc)

    def __repr__(self):
        path = " → ".join(f"{p}" for p, _, _ in self.legs)
        return f"TriArb({path} | +{self.profit_bps:.1f}bps)"


class SyntheticRouter:
    """
    Moteur 28 : Synthetic Pair Routing & Triangular Arbitrage.

    Calcule les routes optimales entre devises et détecte les
    opportunités d'arbitrage triangulaire en temps réel.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Graphe de devises (adjacence)
        self._currency_graph: Dict[str, Dict[str, Tuple[str, float]]] = {}
        self._build_currency_graph()

        # Prix actuels
        self._prices: Dict[str, dict] = {}  # pair → {bid, ask, mid, spread_bps}

        # Routes optimales cachées
        self._optimal_routes: Dict[Tuple[str, str], Route] = {}

        # Opportunités triangulaires
        self._tri_opps: List[TriangularOpportunity] = []
        self._active_tri: Dict[str, TriangularOpportunity] = {}

        # Stats
        self._scans = 0
        self._routes_found = 0
        self._tri_opps_total = 0
        self._last_scan_ms = 0.0

        self._ensure_table()
        logger.info(
            f"🔺 M28 Synthetic Router initialisé "
            f"({len(_PAIR_GRAPH)} paires, {len(self._currency_graph)} devises)"
        )

    # ─── Graph Construction ──────────────────────────────────────────────────

    def _build_currency_graph(self):
        """Construit le graphe des devises à partir des paires tradables."""
        self._currency_graph = {}
        for pair, (base, quote) in _PAIR_GRAPH.items():
            spread = _TYPICAL_SPREADS.get(pair, 10)

            # base → quote (BUY pair)
            if base not in self._currency_graph:
                self._currency_graph[base] = {}
            self._currency_graph[base][quote] = (pair, spread)

            # quote → base (SELL pair)
            if quote not in self._currency_graph:
                self._currency_graph[quote] = {}
            self._currency_graph[quote][base] = (pair, spread)

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="synth_router"
        )
        self._thread.start()
        logger.info("🔺 M28 Synthetic Router démarré (scan toutes les 30s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_optimal_route(self, from_ccy: str, to_ccy: str) -> Optional[Route]:
        """
        Retourne la route optimale entre deux devises.
        Peut être directe ou synthétique.
        """
        with self._lock:
            return self._optimal_routes.get((from_ccy, to_ccy))

    def get_synthetic_cost(self, pair: str) -> Tuple[float, float, bool]:
        """
        Compare le coût direct vs synthétique pour une paire.
        Returns: (direct_cost_bps, synthetic_cost_bps, use_synthetic)
        """
        if pair not in _PAIR_GRAPH:
            return 0, 0, False

        base, quote = _PAIR_GRAPH[pair]
        direct_cost = _TYPICAL_SPREADS.get(pair, 10)

        # Chercher la route synthétique
        with self._lock:
            route = self._optimal_routes.get((base, quote))

        if route and route.is_synthetic:
            return direct_cost, route.total_cost_bps, route.total_cost_bps < direct_cost
        return direct_cost, direct_cost, False

    def get_triangular_opportunities(self) -> List[TriangularOpportunity]:
        """Retourne les opportunités d'arbitrage triangulaire actives."""
        with self._lock:
            return list(self._active_tri.values())

    def stats(self) -> dict:
        with self._lock:
            n_routes = len(self._optimal_routes)
            n_synthetic = sum(1 for r in self._optimal_routes.values() if r.is_synthetic)
            tri_active = {k: round(v.profit_bps, 1)
                          for k, v in self._active_tri.items()}
        return {
            "scans": self._scans,
            "routes_total": n_routes,
            "routes_synthetic": n_synthetic,
            "tri_opps_total": self._tri_opps_total,
            "tri_active": tri_active,
            "currencies": len(self._currency_graph),
            "pairs": len(_PAIR_GRAPH),
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        tri_str = " | ".join(
            f"{k}:+{v}bps" for k, v in s["tri_active"].items()
        ) or "—"
        return (
            f"🔺 <b>Synthetic Router (M28)</b>\n\n"
            f"  Routes: {s['routes_total']} ({s['routes_synthetic']} synthétiques)\n"
            f"  Devises: {s['currencies']} | Paires: {s['pairs']}\n"
            f"  Tri-Arb: {s['tri_opps_total']} opportunités\n"
            f"  Actives: {tri_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(30)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M28 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: refresh prix → routes optimales → triangular arb."""
        # 1. Refresh des prix
        self._refresh_prices()

        # 2. Calculer les routes optimales
        self._compute_optimal_routes()

        # 3. Scanner les opportunités triangulaires
        self._scan_triangular()

    # ─── Price Refresh ───────────────────────────────────────────────────────

    def _refresh_prices(self):
        """Rafraîchit les prix de toutes les paires."""
        if not self._capital:
            return
        for pair in _PAIR_GRAPH:
            try:
                px = self._capital.get_current_price(pair)
                if px:
                    bid = px.get("bid", 0)
                    ask = px.get("ask", 0)
                    mid = px.get("mid", 0)
                    spread_bps = ((ask - bid) / max(mid, 1e-10)) * 10_000

                    self._prices[pair] = {
                        "bid": bid, "ask": ask, "mid": mid,
                        "spread_bps": round(spread_bps, 1),
                    }
            except Exception:
                pass

    # ─── Route Optimization ──────────────────────────────────────────────────

    def _compute_optimal_routes(self):
        """Calcule les routes optimales entre toutes les paires de devises."""
        currencies = list(self._currency_graph.keys())

        for from_ccy in currencies:
            for to_ccy in currencies:
                if from_ccy == to_ccy:
                    continue

                # Route directe
                direct_route = self._find_direct_route(from_ccy, to_ccy)

                # Route synthétique (BFS/Dijkstra)
                synth_route = self._find_synthetic_route(from_ccy, to_ccy)

                # Prendre la moins chère
                best = direct_route
                if synth_route:
                    if not best or synth_route.total_cost_bps < best.total_cost_bps:
                        best = synth_route
                        self._routes_found += 1

                if best:
                    with self._lock:
                        self._optimal_routes[(from_ccy, to_ccy)] = best

    def _find_direct_route(self, from_ccy: str, to_ccy: str) -> Optional[Route]:
        """Trouve la route directe entre deux devises."""
        adj = self._currency_graph.get(from_ccy, {})
        if to_ccy in adj:
            pair, spread = adj[to_ccy]
            # Vérifier le spread réel si disponible
            real_spread = self._prices.get(pair, {}).get("spread_bps", spread)
            cost = real_spread * _SPREAD_COST_MULT + _EXECUTION_SLIPPAGE_BPS

            # Direction
            base, quote = _PAIR_GRAPH.get(pair, ("", ""))
            direction = "BUY" if from_ccy == quote else "SELL"

            return Route(
                legs=[(pair, direction)],
                total_cost_bps=round(cost, 1),
                is_synthetic=False,
            )
        return None

    def _find_synthetic_route(self, from_ccy: str, to_ccy: str) -> Optional[Route]:
        """
        Dijkstra modifié pour trouver la route synthétique la moins chère.
        Max 3 legs.
        """
        # BFS avec coût
        queue = [(from_ccy, [], 0.0, {from_ccy})]  # (current, legs, cost, visited)
        best = None

        while queue:
            current, legs, cost, visited = queue.pop(0)

            if current == to_ccy and legs:
                route = Route(legs=legs, total_cost_bps=round(cost, 1), is_synthetic=True)
                if not best or route.total_cost_bps < best.total_cost_bps:
                    best = route
                continue

            if len(legs) >= _MAX_ROUTE_LEGS:
                continue

            for next_ccy, (pair, spread) in self._currency_graph.get(current, {}).items():
                if next_ccy in visited:
                    continue

                real_spread = self._prices.get(pair, {}).get("spread_bps", spread)
                leg_cost = real_spread * _SPREAD_COST_MULT + _EXECUTION_SLIPPAGE_BPS

                base, quote = _PAIR_GRAPH.get(pair, ("", ""))
                direction = "BUY" if current == quote else "SELL"

                queue.append((
                    next_ccy,
                    legs + [(pair, direction)],
                    cost + leg_cost,
                    visited | {next_ccy},
                ))

        return best

    # ─── Triangular Arbitrage ────────────────────────────────────────────────

    def _scan_triangular(self):
        """Scan toutes les combinaisons triangulaires pour l'arbitrage."""
        if not self._prices:
            return

        currencies = list(self._currency_graph.keys())

        for a in currencies:
            for b in self._currency_graph.get(a, {}):
                for c in self._currency_graph.get(b, {}):
                    if c == a or c == b:
                        continue
                    # Vérifier si c → a existe
                    if a not in self._currency_graph.get(c, {}):
                        continue

                    # Calculer le profit du triangle a→b→c→a
                    profit = self._calc_triangle_profit(a, b, c)
                    if profit and profit > _MIN_PROFIT_BPS:
                        tri_key = f"{a}-{b}-{c}"
                        opp = TriangularOpportunity(
                            legs=self._get_triangle_legs(a, b, c),
                            profit_bps=profit,
                            currencies=[a, b, c],
                        )
                        with self._lock:
                            self._active_tri[tri_key] = opp
                        self._tri_opps_total += 1

                        logger.info(
                            f"🔺 M28 TRI-ARB: {tri_key} +{profit:.1f}bps"
                        )
                        self._persist_opportunity(opp, tri_key)

    def _calc_triangle_profit(self, a: str, b: str, c: str) -> Optional[float]:
        """Calcule le profit d'un triangle a→b→c→a."""
        try:
            # Leg 1: a → b
            pair_ab, spread_ab = self._currency_graph[a][b]
            rate_ab = self._get_effective_rate(pair_ab, a, b)
            if not rate_ab:
                return None

            # Leg 2: b → c
            pair_bc, spread_bc = self._currency_graph[b][c]
            rate_bc = self._get_effective_rate(pair_bc, b, c)
            if not rate_bc:
                return None

            # Leg 3: c → a
            pair_ca, spread_ca = self._currency_graph[c][a]
            rate_ca = self._get_effective_rate(pair_ca, c, a)
            if not rate_ca:
                return None

            # Profit = (rate_ab * rate_bc * rate_ca - 1) * 10000 bps
            product = rate_ab * rate_bc * rate_ca
            profit_bps = (product - 1) * 10_000

            # Soustraire les coûts (spreads + slippage)
            total_cost = sum([
                self._prices.get(pair_ab, {}).get("spread_bps", spread_ab),
                self._prices.get(pair_bc, {}).get("spread_bps", spread_bc),
                self._prices.get(pair_ca, {}).get("spread_bps", spread_ca),
                3 * _EXECUTION_SLIPPAGE_BPS,  # 3 legs
            ])

            net_profit = profit_bps - total_cost
            return round(net_profit, 1) if net_profit > 0 else None

        except Exception:
            return None

    def _get_effective_rate(self, pair: str, from_ccy: str, to_ccy: str) -> Optional[float]:
        """Retourne le taux effectif pour une conversion devise."""
        px = self._prices.get(pair)
        if not px or px["mid"] <= 0:
            return None

        base, quote = _PAIR_GRAPH.get(pair, ("", ""))

        if from_ccy == base:
            # Selling base for quote: use bid
            return px["bid"]
        else:
            # Buying base with quote: use 1/ask
            return 1 / px["ask"] if px["ask"] > 0 else None

    def _get_triangle_legs(self, a: str, b: str, c: str) -> List[Tuple[str, str, float]]:
        """Retourne les legs détaillés d'un triangle."""
        legs = []
        for from_c, to_c in [(a, b), (b, c), (c, a)]:
            pair, _ = self._currency_graph[from_c][to_c]
            rate = self._get_effective_rate(pair, from_c, to_c) or 0
            base, quote = _PAIR_GRAPH.get(pair, ("", ""))
            direction = "SELL" if from_c == base else "BUY"
            legs.append((pair, direction, rate))
        return legs

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS synthetic_routes (
                    id          SERIAL PRIMARY KEY,
                    route_key   VARCHAR(30),
                    profit_bps  FLOAT,
                    legs        TEXT,
                    currencies  TEXT,
                    detected_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M28 table: {e}")

    def _persist_opportunity(self, opp: TriangularOpportunity, key: str):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            import json
            ph = "%s"
            legs_str = json.dumps([(p, d, r) for p, d, r in opp.legs])
            self._db._execute(
                f"INSERT INTO synthetic_routes (route_key,profit_bps,legs,currencies) "
                f"VALUES ({ph},{ph},{ph},{ph})",
                (key, opp.profit_bps, legs_str, ",".join(opp.currencies))
            )
        except Exception:
            pass
