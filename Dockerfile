FROM python:3.11-slim

WORKDIR /app

# Dépendances système pour matplotlib + mplfinance sur Linux (Debian/Ubuntu)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    libcairo2-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Variables d'environnement non-interactif
ENV MPLBACKEND=Agg
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Installer d'abord les dépendances lourdes séparément (meilleur cache Docker)
# Note: mplfinance n'a que des versions beta sur PyPI — on force la version exacte
RUN pip install --no-cache-dir numpy pandas matplotlib Pillow mplfinance==0.12.9b7

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
