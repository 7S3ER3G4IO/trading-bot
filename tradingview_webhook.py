"""
tradingview_webhook.py — TradingView Webhook Server (#2)

Serveur Flask léger qui reçoit les alertes TradingView
et les injecte dans la file de signaux d'Nemesis.

Configuration TradingView (Alert → Webhook URL) :
  URL : http://YOUR_RAILWAY_URL/webhook/tradingview
  Body JSON :
    {
      "secret": "{{strategy.order.alert_message}}",
      "symbol": "GOLD",
      "action": "buy"        // ou "sell", "close"
    }

Sécurité : token secret dans env var WEBHOOK_SECRET.
"""
import os, json, threading
from datetime import datetime, timezone
from loguru import logger

try:
    from flask import Flask, request, jsonify
    _FLASK_OK = True
except ImportError:
    _FLASK_OK = False
    logger.warning("⚠️  Flask non installé — webhook désactivé")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "nemesis_webhook_2026")
# Sur Railway le port PUBLIC est fourni par $PORT (généralement 8080)
# On utilise ce port pour que Railway route vers notre webhook
WEBHOOK_PORT   = int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8081")))


class WebhookServer:
    """
    Serveur Flask en thread daemon.
    Les signaux reçus sont mis dans une queue Thread-safe.
    """

    def __init__(self):
        self._signals: list = []
        self._lock = threading.Lock()
        self._app  = None
        self._started = False

        if not _FLASK_OK:
            return

        self._app = Flask("tradingview_webhook")
        self._app.logger.disabled = True

        @self._app.route("/webhook/tradingview", methods=["POST"])
        def receive():
            try:
                data   = request.get_json(force=True)
                secret = data.get("secret", "")

                if secret != WEBHOOK_SECRET:
                    logger.warning("⚠️  Webhook : token invalide")
                    return jsonify({"error": "unauthorized"}), 401

                symbol = data.get("symbol", "").upper()
                action = data.get("action", "").lower()

                if not symbol or action not in ("buy", "sell", "close"):
                    return jsonify({"error": "invalid payload"}), 400

                # Normalise le symbole → ex: "GOLD" → "ETH/USDT"
                if "/" not in symbol:
                    symbol = symbol.replace("USDT", "/USDT")

                sig = {
                    "symbol":    symbol,
                    "action":    action,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source":    "tradingview",
                }

                with self._lock:
                    self._signals.append(sig)

                logger.info(f"📡 Webhook TradingView : {action.upper()} {symbol}")
                return jsonify({"ok": True, "signal": sig}), 200

            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return jsonify({"error": str(e)}), 500

        @self._app.route("/health", methods=["GET"])
        def health():
            return jsonify({"status": "ok", "bot": "Nemesis v2.0"}), 200

    def start(self):
        """Démarre le serveur en thread daemon."""
        if not self._app or self._started:
            return
        self._started = True

        def _run():
            import logging, os as _os
            # Désactive complètement tous les logs Flask/werkzeug
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            logging.getLogger("werkzeug").propagate = False
            for port in [WEBHOOK_PORT, WEBHOOK_PORT + 1, 5000]:
                try:
                    self._app.run(
                        host="0.0.0.0",
                        port=port,
                        debug=False,
                        use_reloader=False,
                    )
                    break   # succès → sortir
                except OSError:
                    logger.warning(f"⚠️  Webhook : port {port} occupé, essai suivant...")
                    continue
                except Exception as e:
                    logger.error(f"Webhook server error: {e}")
                    break

        t = threading.Thread(target=_run, daemon=True, name="webhook-server")
        t.start()
        logger.info(f"📡 Webhook TradingView démarrage → port {WEBHOOK_PORT}")

    def pop_signals(self) -> list:
        """Retourne et vide tous les signaux en attente."""
        with self._lock:
            sigs = list(self._signals)
            self._signals.clear()
        return sigs

    def has_signals(self) -> bool:
        with self._lock:
            return len(self._signals) > 0


# Singleton
_instance = None

def get_webhook_server() -> WebhookServer:
    global _instance
    if _instance is None:
        _instance = WebhookServer()
    return _instance


if __name__ == "__main__":
    print(f"\n📡 Webhook TradingView — Nemesis")
    print(f"   Port     : {WEBHOOK_PORT}")
    print(f"   Endpoint : POST /webhook/tradingview")
    print(f"   Secret   : {WEBHOOK_SECRET[:6]}***")
    print(f"\n   Config TradingView Alert:")
    print(f"   URL     : http://VOTRE_URL/webhook/tradingview")
    print(f"""   Payload : {{"secret": "{WEBHOOK_SECRET}", "symbol": "GOLD", "action": "buy"}}""")
    print()
    ws = get_webhook_server()
    ws.start()
    import time
    time.sleep(60)
