FROM python:3.11-slim

WORKDIR /app

# Dépendances système pour matplotlib + mplfinance sur Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libfreetype6-dev \
    libpng-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Variable d'environnement pour matplotlib sans écran (serveur)
ENV MPLBACKEND=Agg

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Exclure .env (variables injectées par Railway)
RUN rm -f .env

CMD ["python", "main.py"]
