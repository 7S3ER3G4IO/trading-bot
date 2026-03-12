# Dockerfile — Nemesis Trading Bot (python:3.11-slim = build rapide, wheels binaires)
FROM python:3.11-slim

LABEL maintainer="Nemesis Trading" version="2.0-leviathan"

# Dependencies système minimales
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Créer user non-root
RUN groupadd -r nemesis && useradd -r -g nemesis nemesis

WORKDIR /app

# Installer les deps Python AVANT de copier le code (layer cache)
COPY requirements.txt .

# Wheels binaires disponibles sur slim → pas de compilation → 2-3 min max
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir psycopg2-binary redis hmmlearn 2>/dev/null || true

# Copier le code + créer dossiers writables
COPY --chown=nemesis:nemesis . .
RUN mkdir -p /app/logs /app/data && chown -R nemesis:nemesis /app/logs /app/data

# Volume pour les modèles persistants (RL weights)
VOLUME ["/tmp"]

USER nemesis

# Sentinel Docker healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import os; exit(0 if os.path.exists('/tmp/.nemesis_alive') else 1)" || exit 1

CMD ["python3", "main.py"]
