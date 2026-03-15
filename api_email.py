"""
api_email.py — Collecte leads + séquence drip 3 emails
POST /api/email  → INSERT leads Postgres + envoi email J0
APScheduler toutes les heures → emails J2 et J5

Configuration .env requis:
  DATABASE_URL=...
  SMTP_EMAIL=quant.signals.pro@gmail.com
  SMTP_PASSWORD=xxxx   # App Password Gmail (16 chars)
"""

import os
import re
import time
import asyncio
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text    import MIMEText
from datetime           import datetime, timezone, timedelta

import psycopg2
from flask      import Flask, jsonify, request
from flask_cors import CORS
from apscheduler.schedulers.background import BackgroundScheduler

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL         = os.getenv("DATABASE_URL", "")
SMTP_EMAIL     = os.getenv("SMTP_EMAIL",    "quant.signals.pro@gmail.com")
SMTP_PASSWORD  = os.getenv("SMTP_PASSWORD", "")
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("api_email")

# ── Postgres ──────────────────────────────────────────────────────────────────
def _pg():
    return psycopg2.connect(DB_URL)

def _init_db():
    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
              id          SERIAL PRIMARY KEY,
              email       TEXT UNIQUE NOT NULL,
              source      TEXT DEFAULT 'site',
              created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
              drip_step   INT DEFAULT 0,
              drip_sent_at TIMESTAMP WITH TIME ZONE
            )
        """)
        conn.commit(); cur.close(); conn.close()
        log.info("✅ Table leads prête")
    except Exception as e:
        log.warning(f"[DB init] {e}")

# ── SMTP ──────────────────────────────────────────────────────────────────────
def send_email(to: str, subject: str, html: str) -> bool:
    if not SMTP_PASSWORD:
        log.warning("[SMTP] SMTP_PASSWORD manquant — email simulé")
        log.info(f"[SMTP SIM] To:{to} Subject:{subject}")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"QUANT Signals <{SMTP_EMAIL}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_EMAIL, SMTP_PASSWORD)
            s.sendmail(SMTP_EMAIL, to, msg.as_string())
        log.info(f"✅ Email envoyé → {to} | {subject}")
        return True
    except Exception as e:
        log.error(f"[SMTP] {e}")
        return False

# ── Templates emails ──────────────────────────────────────────────────────────
STYLE = """
body{font-family:Inter,Arial,sans-serif;background:#0a0a0f;color:#fff;margin:0;padding:0}
.wrap{max-width:580px;margin:40px auto;background:#111122;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.08)}
.hero{background:linear-gradient(135deg,#001a2e 0%,#001040 100%);padding:40px 32px;text-align:center}
.logo{font-size:2rem;font-weight:800;letter-spacing:.12em;background:linear-gradient(90deg,#fff,#00D4FF);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.body{padding:32px}
h2{font-size:1.4rem;margin:0 0 12px}
p{color:rgba(255,255,255,.7);line-height:1.7;margin:0 0 16px}
.btn{display:block;width:fit-content;margin:24px auto;padding:14px 32px;background:linear-gradient(135deg,#00D4FF,#0066CC);color:#000;font-weight:700;border-radius:8px;text-decoration:none;font-size:1rem}
.stat{display:inline-block;padding:6px 14px;background:rgba(0,212,255,.1);border:1px solid rgba(0,212,255,.2);border-radius:6px;color:#00D4FF;font-weight:600;margin:4px}
.footer{padding:20px 32px;border-top:1px solid rgba(255,255,255,.06);text-align:center;font-size:.75rem;color:rgba(255,255,255,.25)}
"""

def email_j0(email: str) -> tuple[str, str]:
    subj = "🚀 Bienvenue dans QUANT — Voici comment démarrer"
    html = f"""<!DOCTYPE html><html><head><style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="hero"><div class="logo">QUANT</div><p style="color:rgba(255,255,255,.6);margin-top:8px;margin-bottom:0">Signals PRO — The Algorithm Never Sleeps</p></div>
  <div class="body">
    <h2>Tu es dans la liste 🎉</h2>
    <p>Bienvenue ! Tu viens de t'inscrire sur la waitlist QUANT Signals PRO.<br>
    Voici comment obtenir ton accès :</p>
    <p><strong>1.</strong> Ouvre un compte chez notre broker affilié XM via notre lien<br>
    <strong>2.</strong> Effectue un dépôt minimum de 300€<br>
    <strong>3.</strong> Envoie <strong>GO</strong> à <a href="https://t.me/QuantAccessBot" style="color:#00D4FF">@QuantAccessBot</a> sur Telegram avec ta preuve</p>
    <div style="text-align:center;margin:20px 0">
      <span class="stat">✅ 60.9% Win Rate</span>
      <span class="stat">📈 +1.8R / trade</span>
      <span class="stat">🔒 Risk -5% max</span>
    </div>
    <a href="https://t.me/QuantAccessBot" class="btn">Démarrer sur Telegram →</a>
    <p style="font-size:.85rem">Des questions ? Réponds directement à cet email ou écris à @QuantAccessBot.</p>
  </div>
  <div class="footer">© 2025 QUANT Signals · <a href="https://cobalt-kuiper.vercel.app" style="color:#00D4FF">quant-signals.com</a></div>
</div></body></html>"""
    return subj, html

def email_j2(email: str) -> tuple[str, str]:
    subj = "📊 QUANT a fait +26.2R cette semaine — tu rates ça"
    html = f"""<!DOCTYPE html><html><head><style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="hero"><div class="logo">QUANT</div></div>
  <div class="body">
    <h2>📈 +26.2R en 7 jours</h2>
    <p>Pendant que tu réfléchis, nos membres ont encaissé <strong>+26.2R</strong> cette semaine.</p>
    <p>Un simple exemple :<br>Sur un compte de 1000€ avec 1% de risque/trade → <strong>+262€ net</strong> en 7 jours.</p>
    <blockquote style="border-left:3px solid #00D4FF;padding-left:16px;color:rgba(255,255,255,.6);font-style:italic">
      "J'ai rejoint QUANT il y a 3 semaines. Mes deux premiers mois de signaux ont déjà couvert mon dépôt." — Maxime, 28 ans
    </blockquote>
    <p><strong>Les places sont limitées.</strong> Le bot maintient un ratio strict.</p>
    <a href="https://t.me/QuantAccessBot" class="btn">Rejoindre maintenant →</a>
  </div>
  <div class="footer">© 2025 QUANT Signals · Tu reçois cet email car tu t'es inscrit sur notre waitlist.</div>
</div></body></html>"""
    return subj, html

def email_j5(email: str) -> tuple[str, str]:
    subj = "⏰ Ta place dans QUANT est encore disponible"
    html = f"""<!DOCTYPE html><html><head><style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="hero"><div class="logo">QUANT</div></div>
  <div class="body">
    <h2>⏰ Dernière chance</h2>
    <p>Tu t'es inscrit sur la waitlist QUANT il y a 5 jours. Ta place est encore réservée — pour l'instant.</p>
    <p>Voici ce que tu continues de rater :</p>
    <div style="text-align:center;margin:16px 0">
      <span class="stat">📊 60.9% Win Rate</span>
      <span class="stat">🤖 Bot automatisé 24/7</span>
      <span class="stat">⚡ Signaux en temps réel</span>
    </div>
    <p>Plus de <strong>47 membres</strong> font confiance au bot QUANT chaque jour.<br>
    Accès via broker affilié — dépôt min 300€.</p>
    <a href="https://t.me/QuantAccessBot" class="btn">Je veux ma place →</a>
    <p style="font-size:.8rem;color:rgba(255,255,255,.3)">Si tu ne souhaites plus recevoir nos emails, ignore simplement ce message.</p>
  </div>
  <div class="footer">© 2025 QUANT Signals</div>
</div></body></html>"""
    return subj, html

# ── Drip scheduler ────────────────────────────────────────────────────────────
def run_drip():
    """Vérifie toutes les heures les leads à contacter (J2, J5)."""
    if not DB_URL:
        return
    try:
        conn = _pg(); cur = conn.cursor()
        now = datetime.now(timezone.utc)

        # J2 : drip_step=0 + inscrit il y a ≥ 48h
        cur.execute("""
            SELECT id, email FROM leads
            WHERE drip_step = 0
              AND created_at <= %s
        """, (now - timedelta(hours=48),))
        for row in cur.fetchall():
            lid, email = row
            subj, html = email_j2(email)
            if send_email(email, subj, html):
                cur.execute("UPDATE leads SET drip_step=1, drip_sent_at=%s WHERE id=%s", (now, lid))

        # J5 : drip_step=1 + drip_sent_at il y a ≥ 72h (3j après J2)
        cur.execute("""
            SELECT id, email FROM leads
            WHERE drip_step = 1
              AND drip_sent_at <= %s
        """, (now - timedelta(hours=72),))
        for row in cur.fetchall():
            lid, email = row
            subj, html = email_j5(email)
            if send_email(email, subj, html):
                cur.execute("UPDATE leads SET drip_step=2, drip_sent_at=%s WHERE id=%s", (now, lid))

        conn.commit(); cur.close(); conn.close()
        log.info(f"[Drip] run OK @ {now.strftime('%H:%M')}")
    except Exception as e:
        log.error(f"[Drip] {e}")

# ── Initialisation DB (module level — fonctionne aussi via gunicorn) ──────────
try:
    _init_db()
except Exception as _e:
    log.warning(f"[DB init] {_e}")

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins=[
    "https://cobalt-kuiper.vercel.app",
    "https://quant-signals.com",
    "http://localhost:3000",
])

# ── Rate limiting par IP (max 3 soumissions / heure) ─────────────────────────
_RATE_LIMIT: dict[str, list[float]] = {}  # ip → [timestamps]
_RATE_WINDOW = 3600  # 1 heure
_RATE_MAX    = 3      # max 3 par heure

def _is_rate_limited(ip: str) -> bool:
    now   = time.time()
    times = _RATE_LIMIT.get(ip, [])
    times = [t for t in times if now - t < _RATE_WINDOW]  # purge anciens
    _RATE_LIMIT[ip] = times
    if len(times) >= _RATE_MAX:
        return True
    times.append(now)
    return False

# Regex email simple mais efficace
_EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

@app.route("/api/email", methods=["POST"])
def api_email():
    # Rate limiting par IP
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _is_rate_limited(ip):
        return jsonify({"error": "Trop de soumissions — réessaie dans 1h"}), 429

    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    # Validation stricte par regex
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "email invalide"}), 400
    if len(email) > 254:
        return jsonify({"error": "email trop long"}), 400

    try:
        conn = _pg(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO leads (email) VALUES (%s)
            ON CONFLICT (email) DO NOTHING
            RETURNING id
        """, (email,))
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        if row:
            # Envoyer J0 immédiatement
            subj, html = email_j0(email)
            send_email(email, subj, html)
            return jsonify({"status": "ok", "message": "Inscription enregistrée"}), 201
        else:
            return jsonify({"status": "exists", "message": "Email déjà inscrit"}), 200
    except Exception as e:
        log.error(f"[/api/email] {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_db()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(run_drip, "interval", hours=1, id="drip")
    scheduler.start()
    log.info("📧 api_email démarré — drip scheduler actif")
    port = int(os.getenv("API_EMAIL_PORT", 8081))
    app.run(host="0.0.0.0", port=port, debug=False)
