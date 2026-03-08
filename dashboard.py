"""
dashboard.py — Interface Web AlphaTrader (#8)
Dashboard temps réel : solde, trades ouverts, PnL, WR, signal live.

Usage :
    python3 dashboard.py          # http://localhost:8080
    DASHBOARD_PORT=9090 python3 dashboard.py
"""
import os, sys, json, threading, time
from datetime import datetime, timezone
sys.path.insert(0, ".")

try:
    from flask import Flask, jsonify, render_template_string
except ImportError:
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "flask", "-q"])
    from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

# State partagé — mis à jour par le bot
_state = {
    "balance": 0.0, "initial": 0.0, "pnl_today": 0.0,
    "trades": [], "signals": [], "last_update": "",
    "symbols": [], "n_trades_today": 0, "wr_today": 0.0,
}

HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="30">
<title>AlphaTrader Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #c9d1d9; font-family: 'Segoe UI', sans-serif; padding: 24px; }
  h1 { font-size: 1.8rem; font-weight: 700; color: #58a6ff; margin-bottom: 4px; }
  .sub { color: #8b949e; font-size: 0.85rem; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; }
  .card h3 { font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  .card .val { font-size: 1.8rem; font-weight: 700; }
  .pos { color: #3fb950; } .neg { color: #f85149; } .neu { color: #58a6ff; }
  table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 12px; overflow: hidden; }
  th { background: #21262d; padding: 12px 16px; text-align: left; font-size: 0.75rem; color: #8b949e; text-transform: uppercase; }
  td { padding: 12px 16px; border-top: 1px solid #21262d; font-size: 0.9rem; }
  .badge { padding: 3px 10px; border-radius: 20px; font-size: 0.75rem; font-weight: 600; }
  .buy { background: #0d3321; color: #3fb950; } .sell { background: #3d0f0f; color: #f85149; }
  .section { margin-bottom: 24px; }
  h2 { font-size: 1rem; color: #e6edf3; margin-bottom: 12px; padding-bottom: 8px; border-bottom: 1px solid #21262d; }
</style>
</head>
<body>
<h1>⚡ AlphaTrader Dashboard</h1>
<p class="sub">Mise à jour auto toutes les 30s · {{ state.last_update }}</p>

<div class="grid">
  <div class="card">
    <h3>💰 Balance</h3>
    <div class="val neu">{{ "%.2f"|format(state.balance) }} <span style="font-size:1rem">USDT</span></div>
  </div>
  <div class="card">
    <h3>📈 PnL Aujourd'hui</h3>
    <div class="val {% if state.pnl_today >= 0 %}pos{% else %}neg{% endif %}">
      {{ "%+.2f"|format(state.pnl_today) }} $
    </div>
  </div>
  <div class="card">
    <h3>🎯 Win Rate</h3>
    <div class="val {% if state.wr_today >= 50 %}pos{% else %}neg{% endif %}">{{ "%.0f"|format(state.wr_today) }}%</div>
  </div>
  <div class="card">
    <h3>📊 Trades Aujourd'hui</h3>
    <div class="val neu">{{ state.n_trades_today }}</div>
  </div>
</div>

<div class="section">
  <h2>🔴 Positions Ouvertes ({{ state.trades|length }})</h2>
  {% if state.trades %}
  <table>
    <tr><th>Symbole</th><th>Direction</th><th>Entrée</th><th>SL</th><th>TP</th><th>PnL</th></tr>
    {% for t in state.trades %}
    <tr>
      <td><b>{{ t.symbol }}</b></td>
      <td><span class="badge {{ t.side.lower() }}">{{ t.side }}</span></td>
      <td>{{ "%.4f"|format(t.entry) }}</td>
      <td style="color:#f85149">{{ "%.4f"|format(t.sl) }}</td>
      <td style="color:#3fb950">{{ "%.4f"|format(t.tp) }}</td>
      <td class="{% if t.pnl >= 0 %}pos{% else %}neg{% endif %}">{{ "%+.2f"|format(t.pnl) }}$</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#8b949e;padding:16px">Aucune position ouverte</p>
  {% endif %}
</div>

<div class="section">
  <h2>📡 Signaux Récents</h2>
  {% if state.signals %}
  <table>
    <tr><th>Heure</th><th>Symbole</th><th>Signal</th><th>Score</th></tr>
    {% for s in state.signals[-10:]|reverse %}
    <tr>
      <td style="color:#8b949e">{{ s.time }}</td>
      <td><b>{{ s.symbol }}</b></td>
      <td><span class="badge {{ s.signal.lower() }}">{{ s.signal }}</span></td>
      <td>{{ s.score }}/7</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#8b949e;padding:16px">Aucun signal récent</p>
  {% endif %}
</div>
</body></html>"""

@app.route("/")
def index():
    return render_template_string(HTML, state=type("S", (), _state)())

@app.route("/api/state")
def api_state():
    return jsonify(_state)

@app.route("/api/update", methods=["POST"])
def api_update():
    from flask import request
    global _state
    data = request.get_json(force=True, silent=True) or {}
    _state.update(data)
    _state["last_update"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    return jsonify({"ok": True})


def start_dashboard(port: int = None):
    """Démarre le dashboard dans un thread daemon."""
    port = port or int(os.getenv("DASHBOARD_PORT", "8080"))
    def _run():
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return port


if __name__ == "__main__":
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    print(f"🌐 Dashboard AlphaTrader → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True)
