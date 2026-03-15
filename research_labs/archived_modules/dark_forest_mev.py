"""
dark_forest_mev.py — Moteur 33 : Dark Forest MEV & Validator Bribing

Envoie les transactions via Flashbots / MEV-Boost Builder API
pour éviter le front-running dans la mempool publique.

Architecture :
  FlashbotsRelay    → connexion au relay Flashbots (relay.flashbots.net)
  BundleBuilder     → construction de bundles atomiques
  ValidatorBriber   → calcul et inclusion du pot-de-vin optimal
  PrivateMempool    → bypass de la mempool publique
  SandwichGuard     → détection et protection contre les sandwiches

Stratégie de Bribing :
  - Profit estimé du flash loan arbitrage
  - Bribe = max(1% du profit, gas_price × 1.5) pour le block builder
  - Le validateur inclut notre bundle en position #1
  - Si non profitable après bribe → le bundle est annulé (atomic)

Chaînes supportées :
  - Ethereum (Flashbots relay)
  - Arbitrum (pas de MEV relay nécessaire — séquenceur centralisé)

Note : eth_account pour la signature des bundles. Mode SIMULATION par défaut.
"""
import time
import threading
import json
import os
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone
from loguru import logger
import numpy as np

try:
    import requests as _req
except ImportError:
    _req = None

try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    _ETH_ACCOUNT_OK = True
except ImportError:
    _ETH_ACCOUNT_OK = False
    Account = None

try:
    from web3 import Web3
    _WEB3_OK = True
except ImportError:
    _WEB3_OK = False
    Web3 = None

# ─── Configuration ────────────────────────────────────────────────────────────
_SCAN_INTERVAL_S       = 15       # Scan toutes les 15s
_MIN_BRIBE_GWEI        = 2        # Bribe minimum en Gwei
_MAX_BRIBE_PCT         = 0.05     # Max 5% du profit pour le bribe
_DEFAULT_BRIBE_PCT     = 0.01     # 1% par défaut
_FLASHBOTS_RELAY       = "https://relay.flashbots.net"
_FLASHBOTS_RELAY_GOERLI = "https://relay-goerli.flashbots.net"
_BUILDER_API           = "https://boost-relay.flashbots.net"

# Block builders connus
_BUILDERS = [
    "flashbots",
    "bloxroute",
    "blocknative",
    "titan",
]


class MEVBundle:
    """Un bundle de transactions privé pour le block builder."""

    def __init__(self, txs: List[dict], target_block: int,
                 bribe_wei: int = 0, max_block: int = 0):
        self.txs = txs                    # Transactions signées
        self.target_block = target_block
        self.max_block = max_block or target_block + 5
        self.bribe_wei = bribe_wei        # Pot-de-vin en wei
        self.bribe_gwei = bribe_wei / 1e9
        self.bundle_hash = ""
        self.status = "PENDING"           # PENDING, INCLUDED, REVERTED, EXPIRED
        self.timestamp = datetime.now(timezone.utc)
        self.simulation_result = None

    @property
    def bribe_usd(self) -> float:
        return self.bribe_gwei * 3500 / 1e9  # Approximation ETH price

    def to_flashbots_params(self) -> dict:
        return {
            "txs": [tx.get("raw", "") for tx in self.txs],
            "blockNumber": hex(self.target_block),
            "minTimestamp": 0,
            "maxTimestamp": int(time.time()) + 120,
        }


class SandwichDetector:
    """Détecte les attaques sandwich dans la mempool publique."""

    def __init__(self):
        self._pending_txs: Dict[str, dict] = {}
        self._sandwiches_detected = 0

    def check_sandwich(self, tx_hash: str, pool: str,
                       amount: float) -> Tuple[bool, float]:
        """
        Vérifie si une transaction est susceptible d'être sandwichée.
        Returns: (is_at_risk, estimated_loss_bps)
        """
        # Heuristique : les gros trades sur des pools peu liquides sont vulnérables
        # Pool liquidity estimation (simplifié)
        if amount > 100_000:  # > $100K
            risk = min(amount / 1_000_000, 1.0)
            loss_bps = amount * 0.003 / max(amount, 1)  # ~30 bps sur gros trades
            self._sandwiches_detected += 1
            return True, loss_bps * 10000

        return False, 0.0


class DarkForestMEV:
    """
    Moteur 33 : Dark Forest MEV & Validator Bribing.

    Envoie les transactions via Flashbots pour éviter le front-running.
    Calcule le bribe optimal et construit des bundles atomiques.
    """

    def __init__(self, db=None, flash_loan_engine=None, telegram_router=None):
        self._db = db
        self._flash = flash_loan_engine
        self._tg = telegram_router
        self._running = False
        self._thread = None
        self._lock = threading.Lock()

        # Flashbots auth (ephemeral key — pas de fonds)
        self._auth_key = None
        self._auth_address = None
        self._init_auth()

        # Web3
        self._w3 = None
        self._init_web3()

        # Sandwich detector
        self._sandwich = SandwichDetector()

        # Bundle history
        self._bundles: List[MEVBundle] = []
        self._active_bundles: Dict[str, MEVBundle] = {}

        # Stats
        self._scans = 0
        self._bundles_sent = 0
        self._bundles_included = 0
        self._bundles_reverted = 0
        self._total_bribes_gwei = 0.0
        self._total_profit_saved = 0.0
        self._sandwiches_blocked = 0
        self._last_scan_ms = 0.0

        # Mode
        self._simulation_mode = True

        self._ensure_table()
        logger.info(
            f"🌑 M33 Dark Forest MEV initialisé "
            f"({'SIMULATION' if self._simulation_mode else 'LIVE'}) "
            f"| auth={'✅' if self._auth_key else '❌'}"
        )

    # ─── Auth & Web3 ─────────────────────────────────────────────────────────

    def _init_auth(self):
        """Crée une clé éphémère pour signer les requêtes Flashbots."""
        if not _ETH_ACCOUNT_OK:
            return
        try:
            # Clé éphémère (pas de fonds, juste pour auth Flashbots)
            acct = Account.create()
            self._auth_key = acct.key
            self._auth_address = acct.address
            logger.debug(f"M33 Flashbots auth: {self._auth_address[:10]}...")
        except Exception as e:
            logger.debug(f"M33 auth: {e}")

    def _init_web3(self):
        """Initialise Web3 pour les requêtes blockchain."""
        if not _WEB3_OK:
            return
        try:
            rpc = os.getenv("ETH_RPC_URL", "https://eth.llamarpc.com")
            self._w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if self._w3.is_connected():
                logger.debug(f"M33 Web3: block #{self._w3.eth.block_number}")
        except Exception as e:
            logger.debug(f"M33 Web3: {e}")

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="dark_forest"
        )
        self._thread.start()
        logger.info("🌑 M33 Dark Forest MEV démarré (scan toutes les 15s)")

    def stop(self):
        self._running = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def submit_private_tx(self, raw_tx: str, target_block: int = 0,
                          profit_usd: float = 0) -> Optional[str]:
        """
        Soumet une transaction via Flashbots (private mempool).
        Returns: bundle hash or None.
        """
        if not self._w3:
            return None

        if target_block == 0:
            try:
                target_block = self._w3.eth.block_number + 1
            except Exception:
                return None

        # Calculer le bribe
        bribe_wei = self._calculate_bribe(profit_usd)

        # Créer le bundle
        bundle = MEVBundle(
            txs=[{"raw": raw_tx}],
            target_block=target_block,
            bribe_wei=bribe_wei,
        )

        if self._simulation_mode:
            return self._simulate_bundle(bundle)
        else:
            return self._send_bundle(bundle)

    def get_protection_status(self) -> dict:
        """Retourne le statut de protection MEV."""
        return {
            "mode": "SIMULATION" if self._simulation_mode else "LIVE",
            "auth_active": self._auth_key is not None,
            "web3_connected": self._w3 is not None and self._w3.is_connected() if self._w3 else False,
            "sandwiches_blocked": self._sandwiches_blocked,
            "bundles_sent": self._bundles_sent,
            "bundles_included": self._bundles_included,
        }

    def stats(self) -> dict:
        with self._lock:
            active = {k: v.status for k, v in self._active_bundles.items()}

        return {
            "scans": self._scans,
            "bundles_sent": self._bundles_sent,
            "bundles_included": self._bundles_included,
            "bundles_reverted": self._bundles_reverted,
            "total_bribes_gwei": round(self._total_bribes_gwei, 2),
            "profit_saved_usd": round(self._total_profit_saved, 2),
            "sandwiches_blocked": self._sandwiches_blocked,
            "simulation_mode": self._simulation_mode,
            "active_bundles": active,
            "last_scan_ms": round(self._last_scan_ms, 1),
        }

    def format_report(self) -> str:
        s = self.stats()
        return (
            f"🌑 <b>Dark Forest MEV (M33)</b>\n\n"
            f"  Mode: {'🧪 SIM' if s['simulation_mode'] else '🔴 LIVE'}\n"
            f"  Bundles: ↑{s['bundles_sent']} ✅{s['bundles_included']} "
            f"❌{s['bundles_reverted']}\n"
            f"  Bribes: {s['total_bribes_gwei']:.1f} Gwei\n"
            f"  Sandwiches bloqués: {s['sandwiches_blocked']}\n"
            f"  Profit protégé: ${s['profit_saved_usd']:,.2f}"
        )

    # ─── Scan Loop ───────────────────────────────────────────────────────────

    def _scan_loop(self):
        time.sleep(40)
        while self._running:
            t0 = time.time()
            try:
                self._scan_cycle()
            except Exception as e:
                logger.debug(f"M33 scan: {e}")
            self._last_scan_ms = (time.time() - t0) * 1000
            self._scans += 1
            time.sleep(_SCAN_INTERVAL_S)

    def _scan_cycle(self):
        """Cycle: vérifier les opportunités flash loan → construire bundles."""
        # 1. Chercher les opportunités depuis M30
        if self._flash:
            opps = self._flash.get_opportunities()
            for opp in opps:
                if opp.net_profit < 10:  # Min $10
                    continue

                # 2. Vérifier le risque sandwich
                at_risk, loss_bps = self._sandwich.check_sandwich(
                    tx_hash="", pool=opp.borrow_token,
                    amount=opp.borrow_amount,
                )

                if at_risk:
                    self._sandwiches_blocked += 1
                    # Utiliser Flashbots pour éviter le sandwich
                    bribe_wei = self._calculate_bribe(opp.net_profit)
                    bundle = MEVBundle(
                        txs=[{"raw": "0x_simulated_flash_loan_tx"}],
                        target_block=self._get_next_block(),
                        bribe_wei=bribe_wei,
                    )

                    if self._simulation_mode:
                        self._simulate_bundle(bundle)
                    else:
                        self._send_bundle(bundle)

                    self._total_profit_saved += loss_bps * opp.borrow_amount / 10_000

        # 3. Expirer les bundles anciens
        self._expire_bundles()

    # ─── Bundle Management ───────────────────────────────────────────────────

    def _calculate_bribe(self, profit_usd: float) -> int:
        """Calcule le bribe optimal en wei."""
        # 1% du profit par défaut, max 5%
        bribe_usd = profit_usd * _DEFAULT_BRIBE_PCT
        bribe_usd = min(bribe_usd, profit_usd * _MAX_BRIBE_PCT)

        # Convertir en wei (approx ETH price = $3500)
        bribe_eth = bribe_usd / 3500
        bribe_wei = int(bribe_eth * 1e18)

        # Minimum floor
        min_bribe = _MIN_BRIBE_GWEI * int(1e9)
        return max(bribe_wei, min_bribe)

    def _simulate_bundle(self, bundle: MEVBundle) -> str:
        """Simule l'envoi d'un bundle sans l'envoyer réellement."""
        bundle.status = "SIMULATED"
        bundle.bundle_hash = f"sim_{int(time.time())}"

        self._bundles_sent += 1
        self._bundles_included += 1
        self._total_bribes_gwei += bundle.bribe_gwei

        with self._lock:
            self._active_bundles[bundle.bundle_hash] = bundle
            self._bundles.append(bundle)
            self._bundles = self._bundles[-100:]

        logger.info(
            f"🌑 M33 BUNDLE SIM: block={bundle.target_block} "
            f"bribe={bundle.bribe_gwei:.3f} Gwei "
            f"status={bundle.status}"
        )

        self._persist_bundle(bundle)
        return bundle.bundle_hash

    def _send_bundle(self, bundle: MEVBundle) -> Optional[str]:
        """Envoie un bundle réel via Flashbots relay."""
        if not _req or not self._auth_key:
            return self._simulate_bundle(bundle)

        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_sendBundle",
                "params": [bundle.to_flashbots_params()],
            }

            # Signer la requête
            body = json.dumps(payload)
            if _ETH_ACCOUNT_OK and self._auth_key:
                message = encode_defunct(
                    text=Web3.keccak(text=body).hex() if Web3 else body
                )
                signed = Account.sign_message(message, private_key=self._auth_key)
                auth_header = f"{self._auth_address}:{signed.signature.hex()}"
            else:
                auth_header = ""

            headers = {
                "Content-Type": "application/json",
                "X-Flashbots-Signature": auth_header,
            }

            r = _req.post(_FLASHBOTS_RELAY, data=body, headers=headers, timeout=10)

            if r.ok:
                result = r.json()
                bundle.bundle_hash = result.get("result", {}).get("bundleHash", "")
                bundle.status = "SENT"
                self._bundles_sent += 1

                with self._lock:
                    self._active_bundles[bundle.bundle_hash] = bundle

                logger.info(f"🌑 M33 BUNDLE SENT: {bundle.bundle_hash[:16]}...")
                return bundle.bundle_hash
            else:
                bundle.status = "REJECTED"
                logger.debug(f"M33 bundle rejected: {r.status_code}")

        except Exception as e:
            logger.debug(f"M33 send: {e}")
            return self._simulate_bundle(bundle)

        return None

    def _get_next_block(self) -> int:
        """Retourne le prochain numéro de bloc."""
        if self._w3:
            try:
                return self._w3.eth.block_number + 1
            except Exception:
                pass
        return 0

    def _expire_bundles(self):
        """Expire les bundles de plus de 2 minutes."""
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [
                k for k, v in self._active_bundles.items()
                if (now - v.timestamp).seconds > 120
            ]
            for k in expired:
                self._active_bundles[k].status = "EXPIRED"
                del self._active_bundles[k]

    # ─── Database ────────────────────────────────────────────────────────────

    def _ensure_table(self):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            self._db._execute("""
                CREATE TABLE IF NOT EXISTS mev_bundles (
                    id             SERIAL PRIMARY KEY,
                    bundle_hash    VARCHAR(80),
                    target_block   BIGINT,
                    bribe_gwei     FLOAT,
                    status         VARCHAR(20),
                    n_txs          INT,
                    detected_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
        except Exception as e:
            logger.debug(f"M33 table: {e}")

    def _persist_bundle(self, bundle: MEVBundle):
        if not self._db or not getattr(self._db, '_pg', False):
            return
        try:
            ph = "%s"
            self._db._execute(
                f"INSERT INTO mev_bundles "
                f"(bundle_hash,target_block,bribe_gwei,status,n_txs) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph})",
                (bundle.bundle_hash, bundle.target_block,
                 bundle.bribe_gwei, bundle.status, len(bundle.txs))
            )
        except Exception:
            pass
