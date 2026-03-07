# 🤖 Bot de Trading Automatique — BTC/USDT

Bot de trading automatique sur **Binance Testnet** (démo gratuite).  
Stratégie : **Triple Confluence** (EMA + RSI + MACD) avec gestion du risque intégrée.

---

## 🗂️ Structure

```
trading-bot/
├── main.py               ← Point d'entrée — lancer le bot ici
├── config.py             ← Tous les paramètres (modifier ici)
├── data_fetcher.py       ← Données OHLCV Binance Testnet
├── strategy.py           ← Signaux EMA/RSI/MACD
├── risk_manager.py       ← Taille position, SL/TP, drawdown
├── order_executor.py     ← Exécution des ordres
├── telegram_notifier.py  ← Alertes Telegram
├── logger.py             ← Logs console + fichier
├── .env.example          ← Template credentials
├── requirements.txt
└── logs/                 ← Créé automatiquement
```

---

## 🚀 Installation (macOS)

### 1. Cloner / ouvrir le dossier
```bash
cd trading-bot
```

### 2. Créer et activer un environnement virtuel Python
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Installer les dépendances
```bash
pip install -r requirements.txt
```

### 4. Configurer les credentials
```bash
cp .env.example .env
```
Editer `.env` avec tes clés :

---

## 🔑 Obtenir les credentials (gratuit)

### Binance Testnet (démo crypto)
1. Aller sur **https://testnet.binance.vision/**
2. Se connecter avec GitHub
3. Cliquer **"Generate HMAC_SHA256 Key"**
4. Copier `API Key` et `Secret Key` dans `.env`
5. ⚠️ Le testnet te donne automatiquement **des fonds fictifs** (BTC, USDT, ETH...)

### Telegram Bot
1. Ouvrir **Telegram** → chercher `@BotFather`
2. Envoyer `/newbot` → donner un nom → récupérer le **Token**
3. Chercher `@userinfobot` → envoyer `/start` → récupérer ton **Chat ID**
4. Copier dans `.env` :
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-xxx
   TELEGRAM_CHAT_ID=987654321
   ```

---

## ▶️ Lancer le bot

```bash
# Activer l'env virtuel si pas déjà fait
source venv/bin/activate

# Lancer
python main.py
```

Pour arrêter : **CTRL + C** (arrêt propre)

---

## ⚙️ Configurer la stratégie

Modifier `config.py` :

| Paramètre | Défaut | Signification |
|-----------|--------|---------------|
| `SYMBOL` | `BTC/USDT` | Paire tradée |
| `TIMEFRAME` | `15m` | Durée des bougies |
| `EMA_FAST` | `9` | EMA rapide |
| `EMA_SLOW` | `21` | EMA lente |
| `RSI_PERIOD` | `14` | Période RSI |
| `RISK_PER_TRADE` | `0.01` (1%) | Risque par trade |
| `RR_RATIO` | `2.0` | Ratio Risk:Reward |
| `MAX_OPEN_TRADES` | `3` | Trades simultanés max |
| `DAILY_DRAWDOWN_LIMIT` | `-0.05` (-5%) | Pause automatique |
| `LOOP_INTERVAL_SECONDS` | `60` | Fréquence de vérification |

---

## 📊 Stratégie

Le bot génère un signal d'achat ou de vente lorsque **au moins 2 conditions sur 3** sont remplies :

| Condition | Achat (BUY) | Vente (SELL) |
|-----------|-------------|--------------|
| **EMA** | EMA9 croise EMA21 vers le **haut** | EMA9 croise EMA21 vers le **bas** |
| **RSI** | RSI entre 30 et 65 | RSI entre 35 et 70 |
| **MACD** | MACD line passe **au-dessus** du signal | MACD line passe **en-dessous** du signal |

Stop-Loss dynamique basé sur l'**ATR × 1.5**  
Take-Profit = Stop-Loss × **2.0** (ratio 1:2)

---

## 🛡️ Protections intégrées

- ✅ Maximum **3 trades simultanés**
- ✅ Pause automatique si **perte journalière > 5%**
- ✅ SL/TP surveillés à chaque tick
- ✅ Arrêt propre sur CTRL+C
- ✅ Logs rotatifs (10 MB max, 7 jours)

---

## 📱 Alertes Telegram

Le bot t'envoie un message pour :
- 🚀 Démarrage du bot
- 🟢 Ordre d'achat exécuté (avec SL/TP)
- 🔴 Ordre de vente exécuté
- 🔒 Clôture de trade (avec PnL)
- ⛔ Alerte drawdown
- ⚠️ Erreurs critiques
