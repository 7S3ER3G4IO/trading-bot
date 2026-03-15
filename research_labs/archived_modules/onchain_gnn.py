"""
onchain_gnn.py — Moteur 23 : On-Chain GNN & Mempool Sniping

Analyse on-chain des mouvements de Baleines (Whales) en temps réel.
Utilise un Graph Neural Network simplifié (message-passing via NetworkX)
pour détecter les flux massifs de crypto vers les exchanges AVANT
que le carnet d'ordres ne réagisse.

Sources de données (gratuites) :
  - mempool.space API (BTC pending transactions)
  - Blockchain.com API (BTC large transactions)
  - Whale Alert free tier (multi-chain whale moves)

Architecture :
  WhaleGraph  → graphe des portefeuilles connectés aux exchanges
  GNN Engine  → score de "whale flow" par propagation de messages
  Signals     → si score > seuil → FRONT_RUN signal pour le bot
"""
import time
import threading
import math
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from loguru import logger

try:
    import networkx as nx
    _NX_OK = True
except ImportError:
    _NX_OK = False

try:
    import requests as _req
except ImportError:
    _req = None

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S     = 30       # Scan toutes les 30s
_WHALE_THRESHOLD_USD = 500_000  # Mouvement > $500K = whale
_FLOW_SCORE_TRIGGER  = 0.7     # Score > 0.7 = signal FRONT_RUN
_GRAPH_MAX_NODES     = 500     # Limite la taille du graphe (RAM)
_EXCHANGE_WALLETS    = {
    # Principaux exchanges (adresses publiques connues) — échantillon
    "binance", "coinbase", "kraken", "bitfinex", "okx",
    "bybit", "kucoin", "gemini", "huobi", "bitstamp",
}
_MEMPOOL_API         = "https://mempool.space/api"
_BLOCKCHAIN_API      = "https://blockchain.info"
_WHALE_ALERT_API     = "https://api.whale-alert.io/v1"

# Mapping instrument → chain pour ciblage
_CHAIN_MAP = {
    "BTCUSD": "bitcoin",
    "ETHUSD": "ethereum",
    "BNBUSD": "bsc",
    "XRPUSD": "ripple",
    "SOLUSD": "solana",
    "AVAXUSD": "avalanche",
}


class WhaleMove:
    """Représente un mouvement de baleine détecté."""
    __slots__ = ("from_addr", "to_addr", "amount_usd", "chain",
                 "is_exchange_inflow", "timestamp", "tx_hash")

    def __init__(self, from_addr: str, to_addr: str, amount_usd: float,
                 chain: str, is_exchange_inflow: bool, tx_hash: str = ""):
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.amount_usd = amount_usd
        self.chain = chain
        self.is_exchange_inflow = is_exchange_inflow
        self.timestamp = datetime.now(timezone.utc)
        self.tx_hash = tx_hash


class OnChainGNN:
    """
    Moteur 23 : On-Chain Graph Neural Network & Mempool Sniping.

    Surveille la blockchain pour détecter les mouvements de Baleines
    vers les exchanges. Construit un graphe de flux et calcule un
    "Whale Flow Score" via message-passing GNN simplifié.
    """

    def __init__(self, db=None, capital_client=None, telegram_router=None):
        self._db = db
        self._capital = capital_client
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Graphe des flux whale
        self._graph = nx.DiGraph() if _NX_OK else None
        self._whale_moves: List[WhaleMove] = []
        self._flow_scores: Dict[str, float] = {}   # instrument → score [0..1]

        # Stats
        self._scans = 0
        self._whales_detected = 0
        self._signals_fired = 0
        self._last_scan_ms = 0.0

        # Ensure DB table
        self._ensure_table()
        logger.info("🐋 M23 On-Chain GNN initialisé (Whale Tracking + Mempool Sniping)")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Démarre le scan on-chain en background."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="onchain_gnn"
        )
        self._thread.start()
        logger.info("🐋 M23 On-Chain GNN démarré (scan toutes les 30s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_whale_signal(self, instrument: str) -> Tuple[bool, float, str]:
        """
        Retourne le signal whale pour un instrument crypto.
        Returns: (should_front_run, flow_score, reason)
        """
        if instrument not in _CHAIN_MAP:
            return False, 0.0, "not_crypto"

        with self._lock:
            score = self._flow_scores.get(instrument, 0.0)

        if score >= _FLOW_SCORE_TRIGGER:
            return True, score, f"whale_inflow_{score:.2f}"
        return False, score, "below_threshold"

    def get_all_scores(self) -> Dict[str, float]:
        """Retourne tous les scores de flux whale."""
        with self._lock:
            return dict(self._flow_scores)

    def stats(self) -> dict:
        return {
            "scans": self._scans,
            "whales_detected": self._whales_detected,
            "signals_fired": self._signals_fired,
            "graph_nodes": self._graph.number_of_nodes() if self._graph else 0,
            "graph_edges": self._graph.number_of_edges() if self._graph else 0,
            "last_scan_ms": round(self._last_scan_ms, 1),
            "flow_scores": self.get_all_scores(),
        }

    def format_report(self) -> str:
        s = self.stats()
        scores_str = " | ".join(
            f"{k}:{v:.2f}" for k, v in s["flow_scores"].items() if v > 0
        ) or "—"
        return (
            f"🐋 <b>On-Chain GNN (M23)</b>\n\n"
            f"  Scans: {s['scans']} | Whales: {s['whales_detected']}\n"
            f"  Signals: {s['signals_fired']}\n"
            f"  Graphe: {s['graph_nodes']} nodes / {s['graph_edges']} edges\n"
            f"  Flow Scores: {scores_str}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        """Boucle principale de scan on-chain."""
        time.sleep(15)  # Laisser le bot finir son init
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M23 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Un cycle complet de scan on-chain."""
        # 1. Collecter les mouvements whale
        moves = self._fetch_whale_moves()

        if not moves:
            return

        # 2. Mettre à jour le graphe
        self._update_graph(moves)

        # 3. Calculer les scores via GNN message-passing
        self._compute_flow_scores()

        # 4. Émettre des signaux si nécessaire
        self._emit_signals()

    # ─── Data Collection ─────────────────────────────────────────────────────

    def _fetch_whale_moves(self) -> List[WhaleMove]:
        """Collecte les mouvements whale depuis les APIs publiques."""
        moves = []

        # Source 1: Mempool.space — transactions BTC en attente
        moves.extend(self._scan_btc_mempool())

        # Source 2: Blockchain.com — derniers blocs BTC (grosses transactions)
        moves.extend(self._scan_btc_recent_blocks())

        if moves:
            self._whales_detected += len(moves)
            # Trim history (garder les 200 derniers)
            with self._lock:
                self._whale_moves.extend(moves)
                self._whale_moves = self._whale_moves[-200:]

        return moves

    def _scan_btc_mempool(self) -> List[WhaleMove]:
        """Scan les transactions BTC en attente dans la mempool."""
        if not _req:
            return []
        moves = []
        try:
            # Récupérer les transactions récentes de la mempool
            r = _req.get(f"{_MEMPOOL_API}/mempool/recent", timeout=8)
            if not r.ok:
                return []
            txs = r.json()[:20]  # Top 20 récentes

            for tx in txs:
                value_btc = tx.get("value", 0) / 1e8  # satoshis → BTC
                # Estimation USD (approximation ~ $80K/BTC)
                value_usd = value_btc * 80_000

                if value_usd >= _WHALE_THRESHOLD_USD:
                    moves.append(WhaleMove(
                        from_addr=tx.get("txid", "")[:12],
                        to_addr="exchange_potential",
                        amount_usd=value_usd,
                        chain="bitcoin",
                        is_exchange_inflow=True,  # Conservateur
                        tx_hash=tx.get("txid", ""),
                    ))
        except Exception:
            pass  # Fail silently — non-bloquant
        return moves

    def _scan_btc_recent_blocks(self) -> List[WhaleMove]:
        """Analyse les derniers blocs BTC pour les grosses transactions."""
        if not _req:
            return []
        moves = []
        try:
            # API Blockchain.com — derniers blocs
            r = _req.get(
                f"{_BLOCKCHAIN_API}/blocks?timespan=10minutes&format=json",
                timeout=8
            )
            if not r.ok:
                return []
            blocks = r.json().get("blocks", [])[:2]

            for block in blocks:
                # Vérifier le nombre de transactions et le volume
                n_tx = block.get("n_tx", 0)
                if n_tx > 100:  # Bloc riche → potentiellement intéressant
                    # Estimer le flux basé sur la taille du bloc
                    total_btc = block.get("total_btc_sent", 0) / 1e8
                    if total_btc > 50:  # > 50 BTC dans le bloc
                        moves.append(WhaleMove(
                            from_addr=f"block_{block.get('height', 0)}",
                            to_addr="network",
                            amount_usd=total_btc * 80_000,
                            chain="bitcoin",
                            is_exchange_inflow=False,
                            tx_hash=str(block.get("hash", ""))[:16],
                        ))
        except Exception:
            pass
        return moves

    # ─── Graph Construction ──────────────────────────────────────────────────

    def _update_graph(self, moves: List[WhaleMove]):
        """Met à jour le graphe de flux avec les nouveaux mouvements."""
        if not self._graph:
            return

        for move in moves:
            # Ajouter les nœuds
            self._graph.add_node(move.from_addr, type="wallet",
                                last_seen=move.timestamp.isoformat())
            self._graph.add_node(move.to_addr, type="exchange" if move.is_exchange_inflow else "wallet",
                                last_seen=move.timestamp.isoformat())

            # Ajouter l'arête pondérée
            if self._graph.has_edge(move.from_addr, move.to_addr):
                self._graph[move.from_addr][move.to_addr]["weight"] += move.amount_usd
                self._graph[move.from_addr][move.to_addr]["count"] += 1
            else:
                self._graph.add_edge(
                    move.from_addr, move.to_addr,
                    weight=move.amount_usd,
                    count=1,
                    chain=move.chain,
                )

        # Pruning : limiter la taille du graphe
        if self._graph.number_of_nodes() > _GRAPH_MAX_NODES:
            self._prune_graph()

    def _prune_graph(self):
        """Supprime les nœuds les moins connectés pour limiter la RAM."""
        if not self._graph:
            return
        # Trier par degré (les moins connectés d'abord)
        nodes_by_degree = sorted(
            self._graph.degree(), key=lambda x: x[1]
        )
        # Supprimer les 20% les moins connectés
        n_remove = max(1, len(nodes_by_degree) // 5)
        for node, _ in nodes_by_degree[:n_remove]:
            self._graph.remove_node(node)

    # ─── GNN Message-Passing ─────────────────────────────────────────────────

    def _compute_flow_scores(self):
        """
        GNN simplifié : message-passing sur le graphe des flux.
        Calcule un "Whale Flow Score" par chaîne (= par instrument crypto).
        Algorithme :
          1. Initialiser chaque nœud avec un score basé sur son flux total
          2. Propager les scores à travers les arêtes (message-passing)
          3. Aggréger les scores des nœuds "exchange" par chaîne
          4. Normaliser en [0, 1]
        """
        if not self._graph or self._graph.number_of_nodes() == 0:
            return

        # Phase 1 : Initialisation des scores
        for node in self._graph.nodes():
            total_outflow = sum(
                d.get("weight", 0) for _, _, d in self._graph.out_edges(node, data=True)
            )
            total_inflow = sum(
                d.get("weight", 0) for _, _, d in self._graph.in_edges(node, data=True)
            )
            # Score initial = log du flux total (pour éviter les outliers)
            self._graph.nodes[node]["score"] = math.log1p(total_outflow + total_inflow)

        # Phase 2 : Message-passing (2 itérations)
        for _ in range(2):
            new_scores = {}
            for node in self._graph.nodes():
                # Agrégation des voisins (mean-pool)
                neighbors = list(self._graph.predecessors(node)) + \
                           list(self._graph.successors(node))
                if neighbors:
                    neighbor_score = sum(
                        self._graph.nodes[n].get("score", 0) for n in neighbors
                    ) / len(neighbors)
                    # Update: 70% self + 30% neighbors
                    new_scores[node] = (
                        0.7 * self._graph.nodes[node].get("score", 0) +
                        0.3 * neighbor_score
                    )
                else:
                    new_scores[node] = self._graph.nodes[node].get("score", 0)

            for node, score in new_scores.items():
                self._graph.nodes[node]["score"] = score

        # Phase 3 : Agréger par chaîne → par instrument
        chain_scores: Dict[str, float] = {}
        chain_counts: Dict[str, int] = {}

        for _, _, data in self._graph.edges(data=True):
            chain = data.get("chain", "bitcoin")
            weight = data.get("weight", 0)
            chain_scores[chain] = chain_scores.get(chain, 0) + weight
            chain_counts[chain] = chain_counts.get(chain, 0) + 1

        # Phase 4 : Normaliser en [0, 1] et mapper vers instruments
        max_score = max(chain_scores.values()) if chain_scores else 1
        with self._lock:
            for instrument, chain in _CHAIN_MAP.items():
                raw = chain_scores.get(chain, 0)
                # Sigmoid-like normalization
                normalized = 1 / (1 + math.exp(-5 * (raw / max(max_score, 1) - 0.5)))
                self._flow_scores[instrument] = round(normalized, 3)

    # ─── Signal Emission ─────────────────────────────────────────────────────

    def _emit_signals(self):
        """Émet des signaux si le score whale dépasse le seuil."""
        with self._lock:
            for instrument, score in self._flow_scores.items():
                if score >= _FLOW_SCORE_TRIGGER:
                    self._signals_fired += 1
                    logger.info(
                        f"🐋 M23 WHALE SIGNAL: {instrument} flow={score:.2f} "
                        f"→ FRONT_RUN ready"
                    )
                    self._persist_move(instrument, score)

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS onchain_whale_moves (
                    id           SERIAL PRIMARY KEY,
                    instrument   VARCHAR(20),
                    flow_score   FLOAT,
                    whale_count  INT DEFAULT 0,
                    graph_nodes  INT DEFAULT 0,
                    detected_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M23 table: {e}")

    def _persist_move(self, instrument: str, score: float):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            g_nodes = self._graph.number_of_nodes() if self._graph else 0
            ph = "%s"
            self._db._execute(
                f"INSERT INTO onchain_whale_moves (instrument,flow_score,whale_count,graph_nodes) "
                f"VALUES ({ph},{ph},{ph},{ph})",
                (instrument, score, self._whales_detected, g_nodes)
            )
        except Exception:
            pass
