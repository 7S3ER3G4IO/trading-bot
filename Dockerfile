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
# Step 1a: torch CPU-only 2.6+ (transformers requires >=2.6 for safe torch.load)
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir \
    torch==2.6.0 \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Step 1b: Core requirements
RUN pip install --no-cache-dir -r requirements.txt

# Step 2: Optional packages (can fail)
RUN pip install --no-cache-dir psycopg2-binary redis hmmlearn 2>/dev/null || true

# Step 3: MetaApi SDK — installed with --no-deps to avoid httpx version conflict
# (metaapi wants httpx>=0.28, telegram-bot needs httpx~=0.26.0 — SDK works fine with 0.26)
RUN pip install --no-cache-dir --no-deps \
    metaapi-cloud-sdk \
    metaapi-cloud-copyfactory-sdk \
    metaapi-cloud-metastats-sdk \
 && pip install --no-cache-dir \
    iso8601 \
    python-engineio==3.14.2 \
    python-socketio==4.6.1 \
    rapidfuzz \
    psutil \
    2>/dev/null || true

# Patch permanent SDK MetaApi v29 — bug socket_instances[None] KeyError
# (appliqué ici, après install MetaApi, pour que le fichier existe)
RUN python3 -c "\
f='/usr/local/lib/python3.11/site-packages/metaapi_cloud_sdk/clients/metaapi/metaapi_websocket_client.py';\
c=open(f).read();\
old='                    self._socket_instances[region].keys(),';\
new='                    self._socket_instances.get(region, {}).keys(),';\
open(f,'w').write(c.replace(old,new,1)) if old in c else None;\
print('MetaApi SDK v29 patch OK' if old in c else 'Already patched')\
"

# Copier le code + créer dossiers writables
COPY --chown=nemesis:nemesis . .
# GOD MODE: rule files (Alpha Factory + Black Ops + Lazarus)
COPY --chown=nemesis:nemesis optimized_rules.json black_ops_rules.json lazarus_rules.json ./
RUN mkdir -p /app/logs /app/data /app/.metaapi \
    && chown -R nemesis:nemesis /app/logs /app/data /app/.metaapi

# Volume pour les modèles persistants (RL weights)
VOLUME ["/tmp"]

USER nemesis

# Sentinel Docker healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 -c "import os; exit(0 if os.path.exists('/tmp/.nemesis_alive') else 1)" || exit 1

CMD ["python3", "main.py"]
