# ╔══════════════════════════════════════════════════════════════════╗
# ║   Dockerfile — Nemesis Trading Bot                              ║
# ║   Image ultralegère (Python 3.11 Alpine)                        ║
# ║   Build: docker build -t nemesis-bot .                          ║
# ╚══════════════════════════════════════════════════════════════════╝

# ─── Stage 1: Builder (compile les dépendances C) ────────────────
FROM python:3.11-alpine AS builder

# Dépendances système pour psycopg2, numpy, scikit-learn
RUN apk add --no-cache \
    gcc \
    g++ \
    musl-dev \
    postgresql-dev \
    libffi-dev \
    openssl-dev \
    linux-headers \
    make \
    && rm -rf /var/cache/apk/*

WORKDIR /build

# Copier seulement requirements pour layer cache
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir \
        psycopg2-binary \
        redis \
        hmmlearn

# ─── Stage 2: Runtime (image minimale) ───────────────────────────
FROM python:3.11-alpine AS runtime

LABEL maintainer="Nemesis Trading"
LABEL version="2.0-leviathan"

# Runtime libs seulement (pas de compilo)
RUN apk add --no-cache \
    libpq \
    libstdc++ \
    libgcc \
    curl \
    && rm -rf /var/cache/apk/*

# Créer user non-root pour la sécurité
RUN addgroup -S nemesis && adduser -S nemesis -G nemesis

WORKDIR /app

# Copier le code
COPY --chown=nemesis:nemesis . .

# Installer les deps depuis le stage builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Ajouter les dépendances spécifiques local (redis, hmmlearn)
RUN pip install --no-cache-dir psycopg2-binary redis hmmlearn 2>/dev/null || true

# Volume pour les modèles persistants (RL weights etc.)
VOLUME ["/tmp"]

USER nemesis

# Healthcheck — vérifie que le bot répond
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import os; exit(0 if os.path.exists('/tmp/.nemesis_alive') else 1)" || exit 1

CMD ["python3", "main.py"]
