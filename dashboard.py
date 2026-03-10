"""
dashboard.py — Nemesis Web Dashboard
Interface premium dark — accessible via URL Railway.
Auto-refresh toutes les 15s. Zéro dépendance externe JS.
"""
import os, sys, threading, time, json, logging
from datetime import datetime, timezone

sys.path.insert(0, ".")
# Silence werkzeug proprement (sans WERKZEUG_RUN_MAIN qui casse tout)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

try:
    from flask import Flask, jsonify, render_template_string, Response, request, abort
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "flask", "-q"])
    from flask import Flask, jsonify, render_template_string, Response, request, abort

app   = Flask("nemesis_dashboard")
app.logger.disabled = True

# ─── Auth token (TASK-076/100) ────────────────────────────────────────────────
_DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")

@app.before_request
def _auth():
    """Vérifie le token sur toutes les routes sauf /health."""
    if request.path == "/health":
        return  # health check libre (Railway probes)
    if not _DASHBOARD_TOKEN:
        return  # pas de token configuré = pas d'auth (dev local)
    token = request.args.get("token") or request.headers.get("X-Dashboard-Token", "")
    if token != _DASHBOARD_TOKEN:
        abort(401)


# ─── State partagé ────────────────────────────────────────────────
# BUG FIX #V : Lock pour protéger _state contre les race conditions
# Flask (thread serveur) lit _state / main.py (thread trading) écrit _state
_state_lock = threading.Lock()
_state: dict = {
    "balance":         0.0,
    "futures_balance": 0.0,
    "initial":         0.0,
    "pnl_today":       0.0,
    "pnl_total":       0.0,
    "trades":          [],
    "history":         [],
    "n_today":         0,
    "wr_today":        0.0,
    "wr_overall":      0.0,
    "n_total":         0,
    "last_update":     "—",
    "paused":          False,
    "symbols":         [],
    "max_slots":       4,   # MAX_OPEN_TRADES (mis à jour via update_state)
    "filters": {
        "fear_greed":   "—",
        "volatility":   "—",
        "funding":      "—",
        "news":         "—",
        "drift":        "—",
    },
    "sharpe":          0.0,
    "max_dd":          0.0,
}

_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>⚡ Nemesis Dashboard</title>
<style>
  :root {
    --bg:      #060911;
    --bg2:     #0d1220;
    --bg3:     #141a2e;
    --border:  #1e2a45;
    --text:    #c8d6f0;
    --muted:   #5a6a8a;
    --accent:  #4f9eff;
    --accent2: #7c5cfc;
    --green:   #22d3a0;
    --red:     #ff4f6e;
    --gold:    #f0b429;
    --radius:  14px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Pro Display', 'Segoe UI', system-ui, sans-serif;
    min-height: 100vh;
    padding: 28px 24px;
  }

  /* ── Header ── */
  .header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 32px; }
  .logo { display: flex; align-items: center; gap: 12px; }
  .logo h1 { font-size: 1.5rem; font-weight: 700; background: linear-gradient(90deg, #4f9eff, #7c5cfc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .logo .badge { background: var(--bg3); border: 1px solid var(--border); padding: 4px 12px; border-radius: 20px; font-size: 0.72rem; color: var(--green); font-weight: 600; letter-spacing: 1px; }
  .header-right { font-size: 0.78rem; color: var(--muted); text-align: right; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--green); margin-right: 6px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100%{opacity:1}50%{opacity:.4} }

  /* ── Grid KPI ── */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 14px; margin-bottom: 24px; }
  .kpi {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 18px;
    position: relative;
    overflow: hidden;
    transition: transform .15s;
  }
  .kpi:hover { transform: translateY(-2px); }
  .kpi::before { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; }
  .kpi.blue::before  { background: linear-gradient(90deg, #4f9eff, #7c5cfc); }
  .kpi.green::before { background: linear-gradient(90deg, #22d3a0, #4f9eff); }
  .kpi.gold::before  { background: linear-gradient(90deg, #f0b429, #ff8c00); }
  .kpi.red::before   { background: linear-gradient(90deg, #ff4f6e, #ff8c42); }
  .kpi label { font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: 1.2px; display: block; margin-bottom: 10px; }
  .kpi .value { font-size: 1.7rem; font-weight: 700; line-height: 1; }
  .kpi .sub   { font-size: 0.75rem; color: var(--muted); margin-top: 6px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neu { color: var(--accent); }
  .gold-c { color: var(--gold); }

  /* ── Section ── */
  .section  { margin-bottom: 24px; }
  .section h2 { font-size: 0.85rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px; }
  .section h2::after { content:''; flex:1; height:1px; background:var(--border); }

  /* ── Two col ── */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 24px; }
  @media(max-width:700px) { .two-col { grid-template-columns: 1fr; } }

  /* ── Table ── */
  .card { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  table { width: 100%; border-collapse: collapse; }
  th { background: var(--bg3); padding: 10px 16px; text-align: left; font-size: 0.68rem; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  td { padding: 11px 16px; border-top: 1px solid var(--border); font-size: 0.85rem; }
  tr:hover td { background: rgba(79,158,255,.04); }
  .badge-side { padding: 3px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; letter-spacing: .5px; }
  .badge-buy  { background: rgba(34,211,160,.12); color: var(--green); }
  .badge-sell { background: rgba(255,79,110,.12); color: var(--red); }
  .empty { padding: 32px; text-align: center; color: var(--muted); font-size: 0.85rem; }

  /* ── Filters ── */
  .filters { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 10px; }
  .filter-item { background: var(--bg3); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; display: flex; align-items: center; gap: 10px; }
  .filter-item .fi-label { font-size: 0.72rem; color: var(--muted); }
  .filter-item .fi-val   { font-size: 0.82rem; font-weight: 600; }

  /* ── Status bar ── */
  .status-bar { background: var(--bg2); border: 1px solid var(--border); border-radius: var(--radius); padding: 14px 20px; display: flex; align-items: center; justify-content: space-between; margin-bottom: 24px; flex-wrap: wrap; gap: 10px; }
  .status-bar .s-item { font-size: 0.8rem; color: var(--muted); }
  .status-bar .s-item strong { color: var(--text); }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="logo">
    <h1>⚡ Nemesis</h1>
    <span class="badge">{{ "PAUSED" if s.paused else "LIVE" }}</span>
  </div>
  <div class="header-right">
    <span class="dot"></span>Railway Production<br>
    Mis à jour : {{ s.last_update }} · refresh 15s
  </div>
</div>

<!-- Status bar -->
<div class="status-bar">
  <span class="s-item">🎯 Actifs : <strong>{{ s.symbols|join(' · ') if s.symbols else 'ETH · XRP · ADA · DOGE' }}</strong></span>
  <span class="s-item">📊 Sharpe 30j : <strong class="{{ 'pos' if s.sharpe > 0 else 'neg' }}">{{ '%.3f'|format(s.sharpe) }}</strong></span>
  <span class="s-item">📉 Max DD : <strong class="neg">{{ '%.1f'|format(s.max_dd) }}%</strong></span>
  <span class="s-item">🔢 Trades total : <strong>{{ s.n_total }}</strong></span>
</div>

<!-- KPI Grid -->
<div class="kpi-grid">
  <div class="kpi blue">
    <label>💰 Balance Capital.com (€)</label>
    <div class="value neu">{{ '{:,.2f}'.format(s.balance) }}</div>
    <div class="sub">Initial : {{ '{:,.0f}'.format(s.initial) }} €</div>
  </div>
  {% if s.futures_balance > 0 %}
  <div class="kpi blue">
    <label>🟣 Capital.com Demo</label>
    <div class="value neu">{{ '{:,.2f}'.format(s.futures_balance) }}</div>
    <div class="sub">Solde compte démo</div>
  </div>
  {% endif %}
  <div class="kpi {{ 'green' if s.pnl_total >= 0 else 'red' }}">
    <label>📈 PnL Total</label>
    <div class="value {{ 'pos' if s.pnl_total >= 0 else 'neg' }}">{{ '+' if s.pnl_total >= 0 else '' }}{{ '{:,.2f}'.format(s.pnl_total) }} €</div>
    <div class="sub">Aujourd'hui : {{ '+' if s.pnl_today >= 0 else '' }}{{ '{:.2f}'.format(s.pnl_today) }} €</div>
  </div>
  <div class="kpi gold">
    <label>🎯 Win Rate</label>
    <div class="value {{ 'pos' if s.wr_overall >= 50 else 'neg' }}">{{ '%.1f'|format(s.wr_overall) }}%</div>
    <div class="sub">Aujourd'hui : {{ '%.0f'|format(s.wr_today) }}% ({{ s.n_today }} trades)</div>
  </div>
  <div class="kpi {{ 'green' if s.trades|length > 0 else 'blue' }}">
    <label>⚡ Positions ouvertes</label>
    <div class="value neu">{{ s.trades|length }}/{{ s.max_slots }}</div>
    <div class="sub">Slots disponibles : {{ s.max_slots - s.trades|length }}</div>
  </div>
</div>

<!-- Trades ouverts + Filtres -->
<div class="two-col">

  <!-- Trades ouverts -->
  <div class="section">
    <h2>Positions ouvertes</h2>
    <div class="card">
      {% if s.trades %}
      <table>
        <tr><th>Actif</th><th>Sens</th><th>Entrée</th><th>PnL</th></tr>
        {% for t in s.trades %}
        <tr>
          <td><strong>{{ t.symbol }}</strong></td>
          <td><span class="badge-side {{ 'badge-buy' if t.side == 'BUY' else 'badge-sell' }}">{{ t.side }}</span></td>
          <td>{{ '{:.4f}'.format(t.entry) }}</td>
          <td class="{{ 'pos' if t.pnl >= 0 else 'neg' }}"><strong>{{ '+' if t.pnl >= 0 else '' }}{{ '{:.2f}'.format(t.pnl) }} €</strong></td>
        </tr>
        {% endfor %}
      </table>
      {% else %}
      <div class="empty">🔍 Aucune position ouverte — surveillance en cours</div>
      {% endif %}
    </div>
  </div>

  <!-- Filtres actifs -->
  <div class="section">
    <h2>Filtres actifs 2026</h2>
    <div class="filters">
      <div class="filter-item">
        <span>😨</span>
        <div><div class="fi-label">Fear & Greed</div><div class="fi-val">{{ s.filters.fear_greed }}</div></div>
      </div>
      <div class="filter-item">
        <span>🌡️</span>
        <div><div class="fi-label">Volatilité</div><div class="fi-val">{{ s.filters.volatility }}</div></div>
      </div>
      <div class="filter-item">
        <span>💸</span>
        <div><div class="fi-label">Funding Rate</div><div class="fi-val">{{ s.filters.funding }}</div></div>
      </div>
      <div class="filter-item">
        <span>📰</span>
        <div><div class="fi-label">News Sentiment</div><div class="fi-val">{{ s.filters.news }}</div></div>
      </div>
      <div class="filter-item">
        <span>📉</span>
        <div><div class="fi-label">Drift détecté</div><div class="fi-val">{{ s.filters.drift }}</div></div>
      </div>
    </div>
  </div>
</div>

<!-- Historique trades -->
<div class="section">
  <h2>Derniers trades</h2>
  <div class="card">
    {% if s.history %}
    <table>
      <tr><th>Date</th><th>Actif</th><th>Sens</th><th>Résultat</th><th>PnL</th></tr>
      {% for t in s.history %}
      <tr>
        <td style="color:var(--muted);font-size:.8rem">{{ t.ts }}</td>
        <td><strong>{{ t.symbol }}</strong></td>
        <td><span class="badge-side {{ 'badge-buy' if t.side == 'LONG' else 'badge-sell' }}">{{ t.side }}</span></td>
        <td style="color:var(--{{ 'green' if t.result in ['TP1','TP2','TP3'] else 'red' }})">{{ t.result }}</td>
        <td class="{{ 'pos' if t.pnl >= 0 else 'neg' }}"><strong>{{ '+' if t.pnl >= 0 else '' }}{{ '{:.2f}'.format(t.pnl) }} €</strong></td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="empty">📋 Aucun trade historique disponible</div>
    {% endif %}
  </div>
</div>

<div style="text-align:center;color:var(--muted);font-size:.72rem;margin-top:12px">
  Nemesis v2.0 · Audit 2026 · Refresh automatique 15s
</div>

<script>setTimeout(()=>location.reload(), 15000);</script>
</body>
</html>"""


@app.route("/")
def index():
    with _state_lock:
        snap = dict(_state)   # snapshot atomique pour le rendu
    return render_template_string(_HTML, s=snap)


@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify(dict(_state))


@app.route("/health")
def health():
    return jsonify({"ok": True, "bot": "Nemesis v2.0", "balance": _state["balance"]})


@app.route("/ip")
def get_ip():
    """Retourne l'IP publique de Railway."""
    try:
        import requests as _req
        ip = _req.get("https://ifconfig.me", timeout=5).text.strip()
    except Exception:
        ip = "Impossible de récupérer l'IP"
    return jsonify({"railway_ip": ip, "note": "IP du serveur Railway"})


# ─── API pour mise à jour depuis le bot ────────────────────────────────────

def update_state(**kwargs):
    """Appelé par main.py pour mettre à jour le state du dashboard. Thread-safe."""
    with _state_lock:
        _state.update(kwargs)
        _state["last_update"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def update_trade_open(symbol: str, side: str, entry: float, qty: float):
    with _state_lock:
        trades = _state.setdefault("trades", [])
        trades = [t for t in trades if t["symbol"] != symbol]
        trades.append({"symbol": symbol, "side": side, "entry": entry,
                       "qty": qty, "pnl": 0.0, "ts": datetime.now(timezone.utc).strftime("%H:%M")})
        _state["trades"] = trades


def update_trade_close(symbol: str, pnl: float, result: str, side: str):
    with _state_lock:
        _state["trades"] = [t for t in _state.get("trades", []) if t["symbol"] != symbol]
        hist = _state.setdefault("history", [])
        hist.insert(0, {
            "symbol": symbol, "side": side, "pnl": pnl, "result": result,
            "ts": datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        })
        _state["history"] = hist[:20]


def update_pnl(symbol: str, current_pnl: float):
    with _state_lock:
        for t in _state.get("trades", []):
            if t["symbol"] == symbol:
                t["pnl"] = current_pnl


def update_filter(name: str, value: str):
    with _state_lock:
        _state["filters"][name] = value


# ─── Démarrage ─────────────────────────────────────────────────────────────

def start_dashboard(port: int = None):
    """Lance le serveur Flask dashboard en thread daemon."""
    if port is None:
        port = int(os.getenv("PORT", "8080"))

    def _run():
        # Supprime le warning 'development server' de werkzeug
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True, name="dashboard")
    t.start()
    return port


if __name__ == "__main__":
    print("\n⚡ Nemesis Dashboard — mode standalone")
    port = start_dashboard()
    print(f"   → http://localhost:{port}")
    # Données de démo
    update_state(balance=11035.82, initial=10000.0, pnl_total=1035.82,
                 pnl_today=42.50, wr_overall=58.3, wr_today=66.7,
                 n_today=3, n_total=47, sharpe=0.42, max_dd=4.2,
                 symbols=["GOLD","EURUSD","GBPUSD","USDJPY","US500","US100","DE40","OIL_BRENT"])
    update_filter("fear_greed", "Neutre 52")
    update_filter("volatility", "NORMAL")
    update_filter("funding", "OK")
    update_filter("news", "Neutre")
    update_filter("drift", "Stable")
    update_trade_open("GOLD", "LONG", 2315.50, 1.0)
    update_trade_close("EURUSD", 18.30, "TP2", "LONG")
    update_trade_close("DE40", -12.10, "SL", "LONG")
    while True:
        time.sleep(60)
