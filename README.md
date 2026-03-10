# ⚡ Nemesis v2.0 — Capital.com CFD Trading Bot

> **Mode : 100% DEMO — Aucun argent réel.**  
> Capital.com démo · Railway · PostgreSQL (Supabase) · Telegram

## Instruments tradés (8 actifs CFD)

| Epic | Actif |
|------|-------|
| GOLD | Or / Gold |
| EURUSD | EUR/USD |
| GBPUSD | GBP/USD |
| USDJPY | USD/JPY |
| US500 | S&P 500 |
| US100 | NASDAQ 100 |
| DE40 | DAX 40 |
| OIL_BRENT | Brent Oil |

## Stratégie

- **Timeframe** : 5 minutes (scalping)
- **Sessions** : London 7h-10h UTC + NY 13h-16h UTC
- **Signal** : EMA9/21, RSI, MACD, ADX, Volume, Session Range
- **Score minimum** : 2/3 confirmations
- **Risk/trade** : 1% du capital
- **Max trades simultanés** : 2 (max 1 par instrument)

## Modules actifs

- `strategy.py` — Signal generation (score 0-3)
- `risk_manager.py` — Position sizing, drawdown limit, per-instrument guard
- `protection_model.py` — Blacklist après 3 SL consécutifs
- `equity_curve.py` — Circuit breaker + graphique hebdomadaire
- `mtf_filter.py` — Filtre multi-timeframe (1h/4h)
- `drift_detector.py` — Détection de dérive statistique
- `economic_calendar.py` — Pause avant/après news HIGH impact
- `daily_reporter.py` — Bilan journalier + hebdomadaire Telegram
- `morning_brief.py` — Morning brief avec chart technique 8h
- `dashboard.py` — Dashboard web Railway (auth token)
- `capital_websocket.py` — Connexion WebSocket temps réel
- `brokers/capital_client.py` — REST API Capital.com

## Déploiement (Railway)

### Variables d'environnement requises

```
CAPITAL_API_KEY=...
CAPITAL_IDENTIFIER=...
CAPITAL_PASSWORD=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DATABASE_URL=postgresql://...
DASHBOARD_TOKEN=votre_token_secret   # auth dashboard
```

### Lancement

```bash
python3 main.py
```

## Tests

```bash
python3 -m pytest tests/ -q
# 66+ tests attendus
```

## Architecture

```
main.py                 # Bot principal (London/NY breakout)
├── strategy.py         # Signaux (EMA, RSI, MACD, ADX)
├── risk_manager.py     # Risk (DD, MAX_TRADES, per-instrument)
├── mtf_filter.py       # Multi-timeframe 1h/4h confirmation
├── equity_curve.py     # Circuit breaker + métriques
├── protection_model.py # Blacklist instruments
├── drift_detector.py   # Dérive statistique
├── daily_reporter.py   # Bilans Telegram
├── morning_brief.py    # Matinale + charts
├── dashboard.py        # Web UI (Flask)
├── brokers/
│   └── capital_client.py  # Capital.com REST API
├── legacy/             # Anciens backtester Binance (archivés)
└── tests/              # 66+ tests pytest
```

## Notes

- **Pas de passage en LIVE prévu** — bot d'observation et de recherche en démo
- Données : Capital.com API (`fetch_ohlcv`) — plus de dépendance Binance/ccxt
- Dashboard protégé par `DASHBOARD_TOKEN` (Railway env var)
