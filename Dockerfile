FROM python:3.11-slim

WORKDIR /app

# Installer les dépendances système
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copier et installer les requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copier le code source
COPY . .

# Variables d'environnement (surchargées par Railway)
ENV PYTHONUNBUFFERED=1

# Lancer le bot
CMD ["python", "main.py"]
