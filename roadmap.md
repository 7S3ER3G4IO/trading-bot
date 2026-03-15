# 🗺️ MASTER ROADMAP — Paper Trading → Live 100K€

> **Date** : 12 mars 2026
> **Statut** : ✅ ALL 4 PHASES IMPLEMENTED — God Mode + Full Hardening
> **Objectif** : ~~Identifier tout ce qui sépare l'état actuel d'un compte Live institutionnel~~ FAIT

---

## Sommaire Exécutif

L'architecture actuelle est **solide pour du Paper Trading** : GOD MODE router, 6 moteurs de stratégie (BK/TF/MR/M51/M52/M53), Kelly Criterion, circuit breakers, multi-channel Telegram. Mais un compte à 100K€ exige de la **résilience opérationnelle**, de la **gestion de risque adaptative**, et une **maintenance IA automatisée**.

Les 4 phases ci-dessous couvrent 22 chantiers classés par criticité.

---

## Phase 1 : Critical Hardening 🔴
**Pré-requis absolus avant l'Argent Réel**
*Durée estimée : 1-2 semaines*

### 1.1 Résilience Réseau & API

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Retry 429 + backoff | ✅ Implémenté | — |
| Session renewal | ✅ 9min TTL | — |
| **Reconnexion WebSocket** | ⚠️ Thread daemon | Ajouter watchdog + auto-reconnecter si le heartbeat saute 3×30s |
| **Dead-man switch** | ❌ Absent | Si aucun scan en 5 min → alerte Telegram critique + tentative restart |
| **Fallback REST** | ❌ Absent | Si WebSocket mort > 60s → basculer en polling REST automatiquement |

### 1.2 Gestion des Erreurs d'Ordre

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Auto-retry SL rejeté | ✅ Implémenté | — |
| Auto-retry guaranteed stop | ✅ Implémenté | — |
| **Slippage tracking** | ⚠️ Module existant | Activer `slippage_injector.py` en mode **logging réel** (enregistrer l'écart entre prix demandé et prix exécuté) |
| **Order confirmation loop** | ❌ Absent | Après `place_order()`, boucle de vérification que la position existe dans `get_positions()` sous 5s |
| **Orphan position detector** | ❌ Absent | Scan périodique (5 min) de `get_positions()` vs état interne — alerter si désynchronisé |

### 1.3 Sauvegarde & Persistance

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| PostgreSQL | ✅ Docker volume | — |
| Redis cache | ✅ Docker volume | — |
| **pg_dump automatisé** | ❌ Absent | Cron daily à 03h UTC → dump SQL dans `/app/data/backups/` avec rotation 7 jours |
| **Export état critique** | ⚠️ Partiel | Sauvegarder `equity_history.json` + `drift_state.json` + `protection_state.json` dans le backup |

### 1.4 Limites d'API Broker

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Rate limiter | ✅ `rate_limiter.py` | — |
| **Margin check pre-trade** | ❌ Absent | Vérifier le margin disponible AVANT de placer un ordre (évite les rejets en cascade) |
| **Max positions broker** | ⚠️ Config 20 | Synchroniser `MAX_OPEN_TRADES` avec la limite réelle du compte Live Capital.com |

---

## Phase 2 : Advanced Risk Management 🟡
**Gestion de capital pour protéger 100K€**
*Durée estimée : 2-3 semaines*

### 2.1 Position Sizing Dynamique

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Half-Kelly | ✅ `risk_manager.py` | — |
| Score × vol adjustment | ✅ Implémenté | — |
| **Drawdown monthly limit** | ❌ Absent | Ajouter `MONTHLY_DRAWDOWN_LIMIT = -0.15` (current: daily seulement) |
| **Equity curve circuit breaker** | ⚠️ Basique | Affiner: si equity < SMA20 de l'equity curve → réduire toutes les tailles de 50% |
| **Correlation filter** | ❌ Absent | Limiter les positions corrélées (ex: max 3 Forex simultanés, max 2 crypto) |

### 2.2 Stop-Loss Adaptatif

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Trailing stop | ✅ Implémenté | — |
| Break-even post-TP1 | ✅ Implémenté | — |
| **ATR-scaled trailing** | ❌ Absent | Trailing distance = 1.5 × ATR au lieu d'un pourcentage fixe |
| **Volatility regime SL** | ❌ Absent | En régime CRISIS (HMM) → élargir SL de 25% pour éviter les whipsaws |

### 2.3 Portfolio-Level Controls

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Daily drawdown -10% | ✅ Config | — |
| **Sector exposure limit** | ❌ Absent | Max 30% du capital sur un même secteur (forex/crypto/indices) |
| **VaR quotidien** | ❌ Absent | Calculer la Value at Risk du portefeuille ouvert (Monte Carlo 1000 sims) |
| **Weekend risk auto-reduce** | ⚠️ Friday kill → clôture tout | Option: garder les positions crypto (marchés 24/7) mais clôturer les TradFi |

---

## Phase 3 : AI & Data Maintenance 🟢
**Pipeline automatisé sans intervention humaine**
*Durée estimée : 2-4 semaines*

### 3.1 Réentraînement ML (M52)

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| RandomForest training | ✅ `lazarus_lab.py` | — |
| **Auto-retrain pipeline** | ❌ Absent | Script cron mensuel : télécharger données → re-fit → sauvegarder modèle → comparer accuracy vs ancien → swap si meilleur |
| **Model versioning** | ❌ Absent | Sauvegarder chaque modèle avec timestamp dans `/app/data/models/` + log dans PostgreSQL |
| **Drift detection actif** | ⚠️ `drift_detector.py` existe | Connecter les alerts de drift au pipeline de retrain (trigger automatique si PSI > 0.25) |

### 3.2 Optimisation Continue des Règles

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| optimized_rules.json | ✅ Statique | — |
| **Walk-forward optimization** | ❌ Absent | Chaque mois : relancer `alpha_factory.py` sur les données récentes, comparer les résultats, mettre à jour les seuils/TF si delta > 10% |
| **Performance attribution** | ❌ Absent | Logger chaque trade avec le moteur utilisé → rapport mensuel de performance par stratégie |

### 3.3 Pairs Trading Maintenance (M53)

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Co-intégration statique | ✅ `lazarus_rules.json` | — |
| **Re-test co-intégration** | ❌ Absent | Mensuel : re-tester la co-intégration de chaque paire → retirer les paires qui ne passent plus le test ADF (p > 0.05) |

---

## Phase 4 : Monitoring & Scaling 🔵
**Dashboard, observabilité, multi-instance**
*Durée estimée : 3-4 semaines*

### 4.1 Dashboard Web Temps Réel

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Monitoring | ✅ Telegram (6 canaux) | — |
| **Dashboard web** | ❌ Absent | Streamlit ou Grafana dashboard avec: PnL live, positions ouvertes, equity curve, heatmap par actif |
| **Metrics export** | ❌ Absent | Exporter métriques Prometheus (ou JSON endpoint) pour Grafana: trades/heure, latence API, balance |

### 4.2 Alerting & Observabilité

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Telegram alerts | ✅ Complet | — |
| **Uptime monitoring** | ❌ Absent | Heartbeat externe (UptimeRobot, Healthchecks.io) : ping /health endpoint toutes les 60s |
| **Structured logging** | ⚠️ Loguru text | Ajouter un JSON log exporter pour analyse dans Loki/ELK |

### 4.3 Scaling

| Composant | État actuel | Action requise |
|-----------|-------------|----------------|
| Single Docker instance | ✅ Local Mac Mini | — |
| **Cloud migration** | Prévu | Préparer le déploiement sur VPS dédié (Hetzner/OVH) pour <5ms latence API |
| **Multi-broker** | ❌ Absent | Architecture pour ajouter un second broker (diversification du risque de contrepartie) |

---

## Priorités de Lancement

```
CRITIQUE (avant Live) : Phase 1 intégralement
IMPORTANT (semaine 1-2 Live) : Phase 2.1 + 2.3
SOUHAITABLE (mois 1-2) : Phase 2.2 + 3.1 + 3.2
CONFORT (mois 2-3) : Phase 4
```

---

## Checklist de Passage Live

- [ ] Phase 1 complète (4 blocs = 12 items)
- [ ] 30 jours consécutifs de Paper Trading positif
- [ ] Slippage logging actif avec ratio < 0.5%
- [ ] Backup PostgreSQL quotidien vérifié
- [ ] Dead-man switch testé (kill Docker → alerte reçue < 2 min)
- [ ] Margin check pre-trade validé sur compte Demo
- [ ] Switch `CAPITAL_DEMO=false` dans `.env`
- [ ] Capital initial déposé sur compte Live
