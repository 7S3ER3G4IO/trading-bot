"""
protection_model.py — Freqtrade Protection Model (#8)

Blacklist automatique d'un actif suite à une losing streak
ou une volatilité anormale.

Règles (inspirées du Protection Model de Freqtrade) :
  - 3 SL consécutifs → pause 24h pour cet actif
  - Perte totale > 5% en 1h → pause 4h
  - Gain > 0 reset le compteur SL

L'actif blacklisté continue d'être surveillé mais
aucun nouveau trade n'est ouvert dessus.
"""
import time, json, os
from datetime import datetime, timezone, timedelta
from loguru import logger

PROTECTION_FILE    = "protection_state.json"
SL_STREAK_LIMIT    = 3     # SL consécutifs avant blacklist
STREAK_COOLDOWN_H  = 24    # Heures de pause après losing streak
LOSS_RATE_LIMIT_PCT= 5.0   # % de perte en 1h → pause 4h
RAPID_LOSS_COOLDOWN= 4     # Heures de pause après perte rapide


class ProtectionModel:
    """
    Gère la blacklist automatique des actifs sur-riskés.
    Thread-safe (lecture/écriture sur dict synchronisé par GIL Python).
    """

    def __init__(self):
        self._state: dict = {}  # {symbol: {"sl_streak": int, "blocked_until": float, "reason": str}}
        self._load()

    def _load(self):
        if os.path.exists(PROTECTION_FILE):
            try:
                with open(PROTECTION_FILE) as f:
                    self._state = json.load(f)
                # Nettoie les expirations passées
                now = time.time()
                for sym, s in list(self._state.items()):
                    if s.get("blocked_until", 0) < now:
                        s["blocked_until"] = 0
                logger.info(f"🛡  Protection Model chargé : {len(self._state)} symboles trackés")
            except Exception:
                self._state = {}

    def _save(self):
        try:
            with open(PROTECTION_FILE, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception:
            pass

    def _ensure(self, symbol: str):
        if symbol not in self._state:
            self._state[symbol] = {"sl_streak": 0, "blocked_until": 0, "reason": ""}

    def on_trade_closed(self, symbol: str, pnl: float):
        """
        Appeler après chaque clôture de trade.
        pnl > 0 → win (reset streak)
        pnl < 0 → SL (incrémente streak)
        """
        self._ensure(symbol)
        s = self._state[symbol]

        if pnl >= 0:
            # Win → reset streak
            old = s["sl_streak"]
            s["sl_streak"] = 0
            if old > 0:
                logger.info(f"✅ Protection {symbol} : streak reset (trade gagnant)")
        else:
            # Loss → incrémente streak
            s["sl_streak"] = s.get("sl_streak", 0) + 1
            logger.warning(f"🛡  Protection {symbol} : SL streak = {s['sl_streak']}/{SL_STREAK_LIMIT}")

            if s["sl_streak"] >= SL_STREAK_LIMIT:
                until = time.time() + STREAK_COOLDOWN_H * 3600
                s["blocked_until"] = until
                s["reason"]        = f"{SL_STREAK_LIMIT} SL consécutifs"
                until_str = datetime.fromtimestamp(until, tz=timezone.utc).strftime("%H:%M UTC")
                logger.warning(
                    f"🚫 Protection {symbol} BLACKLISTÉ jusqu'à {until_str} "
                    f"({SL_STREAK_LIMIT} SL consécutifs)"
                )

        self._save()

    def on_rapid_loss(self, symbol: str, loss_pct: float):
        """
        Blacklist temporaire si perte rapide > LOSS_RATE_LIMIT_PCT en 1h.
        """
        self._ensure(symbol)
        s = self._state[symbol]

        if loss_pct >= LOSS_RATE_LIMIT_PCT:
            until = time.time() + RAPID_LOSS_COOLDOWN * 3600
            s["blocked_until"] = until
            s["reason"]        = f"Perte rapide {loss_pct:.1f}% en 1h"
            logger.warning(f"🚫 Protection {symbol} : perte rapide {loss_pct:.1f}% — pause {RAPID_LOSS_COOLDOWN}h")
            self._save()

    def is_blocked(self, symbol: str) -> bool:
        """Retourne True si l'actif est actuellement blacklisté."""
        self._ensure(symbol)
        s   = self._state[symbol]
        now = time.time()
        if s["blocked_until"] > now:
            remaining = timedelta(seconds=s["blocked_until"] - now)
            hours     = int(remaining.total_seconds() // 3600)
            minutes   = int((remaining.total_seconds() % 3600) // 60)
            logger.warning(
                f"🚫 {symbol} blacklisté (raison: {s['reason']}) "
                f"— encore {hours}h{minutes:02d}m"
            )
            return True
        return False

    def get_blacklist(self) -> dict:
        """Retourne tous les actifs actuellement bloqués."""
        now = time.time()
        return {
            sym: s for sym, s in self._state.items()
            if s.get("blocked_until", 0) > now
        }

    def reset(self, symbol: str):
        """Reset manuel d'un actif (via /unblock Telegram)."""
        if symbol in self._state:
            self._state[symbol] = {"sl_streak": 0, "blocked_until": 0, "reason": ""}
            self._save()
            logger.info(f"✅ Protection {symbol} manuellement débloqué")

    def format_status(self) -> str:
        bl = self.get_blacklist()
        if not bl:
            return "✅ Aucun actif blacklisté"
        lines = "🚫 <b>Actifs blacklistés</b>\n\n"
        now = time.time()
        for sym, s in bl.items():
            remaining = int((s["blocked_until"] - now) / 3600)
            lines += f"  🔴 {sym} — {s['reason']} ({remaining}h restantes)\n"
        return lines


if __name__ == "__main__":
    pm = ProtectionModel()
    print(f"\n🛡  Protection Model — AlphaTrader\n")
    pm.on_trade_closed("ETH/USDT", -10)
    pm.on_trade_closed("ETH/USDT", -8)
    pm.on_trade_closed("ETH/USDT", -12)
    print(f"  ETH/USDT bloqué : {pm.is_blocked('ETH/USDT')}")
    print(f"\n  Blacklist :\n{pm.format_status()}")
    pm.reset("ETH/USDT")
    print(f"\n  Après reset : {pm.is_blocked('ETH/USDT')}")
