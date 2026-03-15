#!/usr/bin/env python3
"""
insomnia_daemon.py — Project Insomnia: MacOS Sleep Prevention Daemon

Empêche macOS de s'endormir tant que le bot Nemesis tourne dans Docker.
Smart Awake: vérifie toutes les 60s si le container est actif.

  Bot UP   → caffeinate actif (Mac reste éveillé, même capot fermé)
  Bot DOWN → caffeinate tué (Mac peut dormir normalement)

Usage (terminal séparé sur le Mac hôte) :
    nohup python3 insomnia_daemon.py &

Ou pour voir les logs en direct :
    python3 insomnia_daemon.py
"""

import subprocess
import signal
import sys
import time
from datetime import datetime

# ─── Configuration ────────────────────────────────────────────────────────────
CONTAINER_NAME = "nemesis_bot"          # Nom du container Docker à surveiller
CHECK_INTERVAL = 60                     # Vérification toutes les 60 secondes
CAFFEINATE_CMD = ["caffeinate", "-d", "-i", "-m", "-s"]
#   -d  Prevent display sleep
#   -i  Prevent system idle sleep
#   -m  Prevent disk idle sleep
#   -s  Prevent system sleep (even on AC power lid close)

# ─── State ────────────────────────────────────────────────────────────────────
_caffeinate_proc = None
_running = True


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _is_bot_running() -> bool:
    """Check if the Nemesis bot Docker container is running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTAINER_NAME}",
             "--filter", "status=running", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        return CONTAINER_NAME in result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as e:
        print(f"[{_ts()}] ⚠️  Docker check failed: {e}")
        return False


def _start_caffeinate():
    """Start the caffeinate process to prevent macOS sleep."""
    global _caffeinate_proc
    if _caffeinate_proc and _caffeinate_proc.poll() is None:
        return  # Already running
    try:
        _caffeinate_proc = subprocess.Popen(
            CAFFEINATE_CMD,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[{_ts()}] ☕ Caffeinate ACTIVÉ (PID {_caffeinate_proc.pid}) — Mac restera éveillé")
    except FileNotFoundError:
        print(f"[{_ts()}] ❌ caffeinate introuvable — êtes-vous sur macOS ?")
    except Exception as e:
        print(f"[{_ts()}] ❌ Erreur lancement caffeinate: {e}")


def _stop_caffeinate():
    """Kill the caffeinate process to allow macOS sleep."""
    global _caffeinate_proc
    if _caffeinate_proc and _caffeinate_proc.poll() is None:
        _caffeinate_proc.terminate()
        try:
            _caffeinate_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _caffeinate_proc.kill()
        print(f"[{_ts()}] 😴 Caffeinate DÉSACTIVÉ — Mac peut dormir")
    _caffeinate_proc = None


def _shutdown(sig, frame):
    """Graceful shutdown on SIGINT/SIGTERM."""
    global _running
    print(f"\n[{_ts()}] 🛑 Insomnia daemon arrêté")
    _running = False
    _stop_caffeinate()
    sys.exit(0)


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    global _running

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("=" * 50)
    print("  ☕ PROJECT INSOMNIA — MacOS Sleep Guard")
    print(f"  Container surveillé : {CONTAINER_NAME}")
    print(f"  Intervalle : {CHECK_INTERVAL}s")
    print("=" * 50)
    print()

    was_running = None  # Track state transitions

    while _running:
        bot_alive = _is_bot_running()

        if bot_alive and was_running is not True:
            # Transition: OFF → ON
            print(f"[{_ts()}] 🟢 {CONTAINER_NAME} détecté UP")
            _start_caffeinate()
            was_running = True

        elif not bot_alive and was_running is not False:
            # Transition: ON → OFF
            print(f"[{_ts()}] 🔴 {CONTAINER_NAME} arrêté — libération du Mac")
            _stop_caffeinate()
            was_running = False

        # Verify caffeinate didn't crash while bot is UP
        if bot_alive and _caffeinate_proc and _caffeinate_proc.poll() is not None:
            print(f"[{_ts()}] ⚠️  Caffeinate crashé — redémarrage...")
            _start_caffeinate()

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
