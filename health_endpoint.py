"""
health_endpoint.py — ⚡ Phase 4.2: HTTP Health Endpoint for Uptime Monitoring

Simple Flask/Bottle health endpoint that external monitors (UptimeRobot, Healthchecks.io)
can ping every 60s. Returns 200 if bot is alive, 503 if watchdog hasn't seen a tick.

Usage:
    # In bot_init.py or as separate process:
    from health_endpoint import start_health_server
    start_health_server(port=8080, watchdog=self.watchdog)

    # External: curl http://localhost:8080/health
"""

import json
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from loguru import logger


_watchdog_ref = None
_bot_start_time = datetime.now(timezone.utc)


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for /health endpoint."""

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            status = self._build_status()
            code = 200 if status["healthy"] else 503
            self.send_response(code)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status, indent=2).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _build_status(self) -> dict:
        global _watchdog_ref
        now = datetime.now(timezone.utc)
        uptime = (now - _bot_start_time).total_seconds()

        result = {
            "service": "nemesis-trading-bot",
            "version": "2.0",
            "timestamp": now.isoformat(),
            "uptime_seconds": int(uptime),
            "healthy": True,
        }

        if _watchdog_ref:
            elapsed = _watchdog_ref.seconds_since_last_tick
            result["last_tick_seconds_ago"] = round(elapsed, 1)
            result["ws_fallback"] = _watchdog_ref.ws_fallback
            if elapsed > 300:  # 5 min
                result["healthy"] = False
                result["reason"] = f"No tick for {int(elapsed)}s"

        return result

    def log_message(self, format, *args):
        """Suppress default access logs."""
        pass


def start_health_server(port: int = 8081, watchdog=None):
    """Start health endpoint in background thread."""
    global _watchdog_ref
    _watchdog_ref = watchdog

    def _serve():
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logger.info(f"🏥 Health endpoint: http://0.0.0.0:{port}/health")
        server.serve_forever()

    t = threading.Thread(target=_serve, daemon=True, name="health_endpoint")
    t.start()
    return port
