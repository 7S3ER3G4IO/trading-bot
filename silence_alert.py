"""
silence_alert.py — Alerte Discord si le bot est silencieux > 5 min
Lancé toutes les 5 min via cron sur le VPS :
  */5 * * * * python3 /root/trading-bot/silence_alert.py >> /root/backups/silence.log 2>&1
"""
import subprocess
import os
import requests
from datetime import datetime, timezone

DISCORD_WEBHOOK = os.getenv("DISCORD_MONITORING_WEBHOOK", "")
SILENCE_THRESHOLD_SECS = 300  # 5 minutes

def get_last_log_age_secs() -> float:
    """Retourne l'âge en secondes du dernier log du bot."""
    try:
        result = subprocess.run(
            ["docker", "logs", "nemesis_bot", "--tail", "1", "--timestamps"],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return float("inf")
        # Format: 2026-03-15T17:46:21.123456789Z LOG TEXT
        ts_str = output.split(" ")[0].replace("Z", "+00:00")
        last_ts = datetime.fromisoformat(ts_str[:26] + "+00:00")
        now = datetime.now(timezone.utc)
        return (now - last_ts).total_seconds()
    except Exception as e:
        print(f"Erreur lecture logs: {e}")
        return float("inf")


def get_bot_status() -> dict:
    """Vérifie que le container est running et healthy."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}}|{{.State.Health.Status}}", "nemesis_bot"],
            capture_output=True, text=True, timeout=10
        )
        parts = result.stdout.strip().split("|")
        return {
            "running": parts[0] == "running",
            "health": parts[1] if len(parts) > 1 else "unknown"
        }
    except Exception:
        return {"running": False, "health": "unknown"}


def send_alert(message: str):
    """Envoie une alerte sur Discord via webhook."""
    if not DISCORD_WEBHOOK:
        print(f"[ALERT] {message}")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": f"🚨 **NEMESIS ALERT** — {message}"}, timeout=10)
    except Exception as e:
        print(f"Discord alert failed: {e}")


def check():
    status = get_bot_status()
    age_secs = get_last_log_age_secs()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not status["running"]:
        send_alert(f"🔴 Container nemesis_bot est **arrêté** ! ({now})")
        return

    if age_secs >= SILENCE_THRESHOLD_SECS:
        mins = int(age_secs // 60)
        send_alert(
            f"⚠️ Bot silencieux depuis **{mins} minutes** (dernier log il y a {int(age_secs)}s) — vérifie Docker ! ({now})"
        )
    else:
        print(f"[OK] Bot actif — dernier log il y a {int(age_secs)}s")


if __name__ == "__main__":
    check()
