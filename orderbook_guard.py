"""
orderbook_guard.py — Moteur 2 : Order Book Imbalance Filter.

Capital.com ne fournit pas de L2 orderbook.
On utilise le bid/ask spread + momentum de prix court-terme comme proxy:

Bid/Ask Imbalance Proxy:
    - Si BUY signal : ask_price est le vrai coût d'entrée
      Si Ask >> Bid de trop (spread large) ET que les dernières bougies
      montrent un momentum vendeur → "sell wall détecté" → skip
    - Si SELL signal : inverse

Méthode multi-signal (plus robuste qu'un seul indicateur) :
    1. Spread relatif   (spread/mid > threshold → skip)
    2. Momentum court   (prix mid close des 5 dernières mins vs 15 dernières mins)
    3. Volume imbalance (up_bars vs down_bars sur les N dernières bougies)

Non-bloquant : exécuté dans un ThreadPoolExecutor, timeout 0.5s.
Si timeout → on laisse passer (fail-open).
"""
import concurrent.futures
import time
from loguru import logger

# ─── Paramètres ───────────────────────────────────────────────────────────────
_SPREAD_THRESHOLD  = 0.0020   # Spread > 0.20% du mid → déjà filtré plus tôt, on est plus tolérant
_MOMENTUM_BARS     = 5        # Bougies pour calculer le momentum court
_MOMENTUM_WINDOW   = 15       # Bougies pour le momentum long (baseline)
_VOLUME_BARS       = 10       # Bougies pour le volume imbalance
_IMBALANCE_THRESH  = 0.70     # Si 70% des bougies vont contre notre signal → block
_GUARD_TIMEOUT     = 0.5      # Max 0.5s pour ne pas bloquer le signal pipeline
_CONFIDENCE_WINDOW = 3        # Besoin de >= 2/3 signaux convergents pour bloquer

_GUARD_EXEC = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="ob_guard"
)


class OrderBookGuard:
    """
    Filtre d'imbalance juste avant l'entrée en position.

    Usage:
        guard = OrderBookGuard(capital_client)
        allowed, reason = guard.check(instrument, direction, df, entry)
        if not allowed:
            logger.info(f"⛔ Entrée bloquée: {reason}")
            return
    """

    def __init__(self, capital_client=None):
        self._capital = capital_client
        self._block_count = 0
        self._pass_count = 0

    def check(self, instrument: str, direction: str, df, entry: float) -> tuple:
        """
        Checks imbalance signals asynchronously (timeout 0.5s).
        Returns (allowed: bool, reason: str).
        Fail-open: if timeout or error → (True, "timeout/error").
        """
        future = _GUARD_EXEC.submit(
            self._check_sync, instrument, direction, df, entry
        )
        try:
            allowed, reason = future.result(timeout=_GUARD_TIMEOUT)
            if not allowed:
                self._block_count += 1
                logger.info(
                    f"🛡️ OrderBook Guard: {instrument} {direction} BLOQUÉ — {reason} "
                    f"(total bloqués: {self._block_count})"
                )
            else:
                self._pass_count += 1
            return allowed, reason
        except concurrent.futures.TimeoutError:
            logger.debug(f"OrderBook Guard {instrument}: timeout — fail-open")
            return True, "timeout"
        except Exception as e:
            logger.debug(f"OrderBook Guard {instrument}: error ({e}) — fail-open")
            return True, f"error:{e}"

    # ─── Internals ───────────────────────────────────────────────────────────

    def _check_sync(self, instrument: str, direction: str, df, entry: float) -> tuple:
        """Exécuté dans le thread pool — calcule l'imbalance."""
        signals_against = 0  # Nombre de signaux contre notre direction

        # ── Signal 1: Spread relatif ─────────────────────────────────────────
        spread_score, spread_reason = self._check_spread(instrument, direction, entry)
        if spread_score > 0:
            signals_against += spread_score

        # ── Signal 2: Momentum court terme ───────────────────────────────────
        mom_score, mom_reason = self._check_momentum(df, direction)
        if mom_score > 0:
            signals_against += mom_score

        # ── Signal 3: Volume imbalance ────────────────────────────────────────
        vol_score, vol_reason = self._check_volume_imbalance(df, direction)
        if vol_score > 0:
            signals_against += vol_score

        # Décision: besoin de ≥ 2 signaux convergents pour bloquer
        if signals_against >= _CONFIDENCE_WINDOW - 1:
            reasons = filter(None, [spread_reason, mom_reason, vol_reason])
            return False, " | ".join(reasons)

        return True, "ok"

    def _check_spread(self, instrument: str, direction: str,
                       entry: float) -> tuple:
        """Fetch prix en temps réel et évalue le spread."""
        try:
            if not self._capital:
                return 0, None
            px = self._capital.get_current_price(instrument)
            if not px:
                return 0, None

            bid = px.get("bid", entry)
            ask = px.get("ask", entry)
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 0

            # Si spread très large → entrée défavorable
            if spread_pct > _SPREAD_THRESHOLD * 2:
                return 1, f"spread={spread_pct:.3%}"

            # Directional check: si BUY, l'ask est très distance du mid
            # → marché en mode vendeur (takers payent plus)
            if direction == "BUY" and ask > mid * (1 + _SPREAD_THRESHOLD * 1.5):
                return 1, f"ask_premium={((ask/mid)-1):.3%}"
            if direction == "SELL" and bid < mid * (1 - _SPREAD_THRESHOLD * 1.5):
                return 1, f"bid_discount={((mid/bid)-1):.3%}"

        except Exception:
            pass
        return 0, None

    def _check_momentum(self, df, direction: str) -> tuple:
        """
        Momentum court: si les 5 dernières bougies vont contre notre signal.
        Court MA vs Long MA du close.
        """
        try:
            if df is None or "close" not in df.columns or len(df) < _MOMENTUM_WINDOW:
                return 0, None

            closes = df["close"].iloc[-_MOMENTUM_WINDOW:].values
            ma_short = closes[-_MOMENTUM_BARS:].mean()
            ma_long  = closes.mean()

            # Momentum baissier alors qu'on veut acheter
            if direction == "BUY" and ma_short < ma_long * 0.9985:
                return 1, f"momentum⬇ ma_short={ma_short:.5f}<ma_long={ma_long:.5f}"

            # Momentum haussier alors qu'on veut vendre
            if direction == "SELL" and ma_short > ma_long * 1.0015:
                return 1, f"momentum⬆ ma_short={ma_short:.5f}>ma_long={ma_long:.5f}"

        except Exception:
            pass
        return 0, None

    def _check_volume_imbalance(self, df, direction: str) -> tuple:
        """
        Volume imbalance: ratio bougies haussières vs baissières sur N bar.
        """
        try:
            if df is None or "close" not in df.columns or "open" not in df.columns:
                return 0, None
            if len(df) < _VOLUME_BARS:
                return 0, None

            recent = df.iloc[-_VOLUME_BARS:]
            up_bars   = (recent["close"] > recent["open"]).sum()
            down_bars = (recent["close"] < recent["open"]).sum()
            total     = up_bars + down_bars
            if total == 0:
                return 0, None

            down_ratio = down_bars / total
            up_ratio   = up_bars   / total

            # Vouloir BUY mais 70%+ des bars sont baissières
            if direction == "BUY" and down_ratio >= _IMBALANCE_THRESH:
                return 1, f"vol_imbalance: {down_ratio:.0%} bearish bars"

            # Vouloir SELL mais 70%+ des bars sont haussières
            if direction == "SELL" and up_ratio >= _IMBALANCE_THRESH:
                return 1, f"vol_imbalance: {up_ratio:.0%} bullish bars"

        except Exception:
            pass
        return 0, None

    def stats(self) -> dict:
        total = self._block_count + self._pass_count
        block_rate = self._block_count / total if total > 0 else 0
        return {
            "blocked": self._block_count,
            "passed": self._pass_count,
            "block_rate": f"{block_rate:.1%}",
        }
