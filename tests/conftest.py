"""tests/conftest.py — Configuration globale pytest"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Désactiver tous les imports externes coûteux pendant les tests
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DEPLOYMENT_ENV", "test")
os.environ.pop("DATABASE_URL", None)  # force SQLite
