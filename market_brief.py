"""
market_brief.py — Briefing marché quotidien QUANT
===================================================
Chaque jour de marché (lun-ven) à 7h30 UTC :
→ Prix live via yfinance (GOLD, EURUSD, BTC, SPX)
→ Actualités via NewsAPI (facultatif)
→ Post dans le canal Telegram QUANT Signals PRO

Variables .env requises :
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHANNEL_ID=-1003526967892
  NEWSAPI_KEY=... (facultatif)
  DATABASE_URL=... (facultatif)
"""

import os
import asyncio
import requests
from datetime import datetime, timezone, date
from loguru import logger

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

TG_API  = f"https://api.telegram.org/bot{BOT_TOKEN}"
TIMEOUT = 10

# Proxy WARP (SOCKS5 via gost sur le host)
_PROXY = os.getenv("HTTPS_PROXY", "") or os.getenv("HTTP_PROXY", "")
_PROXIES = {"https": _PROXY, "http": _PROXY} if _PROXY else {}

# Stooq ticker map (fallback si Yahoo bloqué)
_STOOQ_MAP = {
    "GOLD":   "gc.f",
    "EURUSD": "eurusd",
    "BTC":    "btc-usd",
    "SPX":    "^spx",
}

# ── Envoi Telegram ─────────────────────────────────────────────────────────────
def _send(text: str) -> bool:
    if not BOT_TOKEN or not CHANNEL_ID:
        logger.warning("[Brief] BOT_TOKEN ou CHANNEL_ID manquant")
        return False
    try:
        resp = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": CHANNEL_ID, "text": text, "parse_mode": "HTML"},
            timeout=TIMEOUT,
        )
        ok = resp.status_code == 200
        if not ok:
            logger.warning(f"[Brief] Telegram {resp.status_code}: {resp.text[:100]}")
        return ok
    except Exception as e:
        logger.error(f"[Brief] send: {e}")
        return False

# ── Prix via yfinance (Yahoo) avec fallback Stooq ─────────────────────────────
def _fetch_price_stooq(name: str, stooq_sym: str) -> dict:
    """Fallback Stooq (CSV gratuit, accessible depuis Hetzner)."""
    try:
        url = f"https://stooq.com/q/d/l/?s={stooq_sym}&i=d"
        r = requests.get(url, timeout=8, headers={"User-Agent": "QuantBot/1.0"})
        if r.status_code != 200 or len(r.text) < 50:
            return {}
        lines = r.text.strip().split("\n")
        if len(lines) < 3:
            return {}
        # CSV: Date,Open,High,Low,Close,Volume
        today_row = lines[-1].split(",")
        prev_row  = lines[-2].split(",")
        close_t = float(today_row[4])
        close_p = float(prev_row[4])
        return {"price": close_t, "change": (close_t - close_p) / close_p * 100}
    except Exception as e:
        logger.debug(f"[Brief] Stooq {name}: {e}")
        return {}

def _fetch_prices() -> dict:
    """Prix GOLD, EURUSD, BTC, SPX — Yahoo via proxy WARP, fallback Stooq."""
    result = {}
    tickers = {
        "GOLD":   "GC=F",
        "EURUSD": "EURUSD=X",
        "BTC":    "BTC-USD",
        "SPX":    "^GSPC",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    for name, symbol in tickers.items():
        try:
            url = (
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?interval=1d&range=2d"
            )
            resp = requests.get(url, headers=headers, timeout=TIMEOUT,
                                proxies=_PROXIES)
            if resp.status_code == 200:
                data   = resp.json()
                closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                closes = [c for c in closes if c is not None]
                if len(closes) >= 2:
                    ct = closes[-1]; cp = closes[-2]
                    result[name] = {"price": ct, "change": (ct - cp) / cp * 100}
                    continue
        except Exception as e:
            logger.debug(f"[Brief] Yahoo {name}: {e}")
        # Fallback Stooq
        fb = _fetch_price_stooq(name, _STOOQ_MAP.get(name, ""))
        if fb:
            result[name] = fb
    return result

# ── News via NewsAPI ───────────────────────────────────────────────────────────
def _fetch_news(n: int = 3) -> list[str]:
    """Retourne les n premiers titres d'actualités financières du jour."""
    if not NEWSAPI_KEY:
        return []
    try:
        today = date.today().isoformat()
        resp  = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q":          "gold forex trading bourse",
                "from":       today,
                "sortBy":     "publishedAt",
                "language":   "fr",
                "pageSize":   n,
                "apiKey":     NEWSAPI_KEY,
            },
            timeout=TIMEOUT,
            proxies=_PROXIES,
        )
        if resp.status_code != 200:
            logger.debug(f"[Brief] NewsAPI {resp.status_code}")
            return []
        articles = resp.json().get("articles", [])
        headlines = []
        for a in articles[:n]:
            title = a.get("title", "").split(" - ")[0].strip()
            if title and len(title) > 10:
                headlines.append(title[:90])
        return headlines
    except Exception as e:
        logger.debug(f"[Brief] news: {e}")
        return []

# ── Analyse courte ────────────────────────────────────────────────────────────
def _analyse(change: float) -> str:
    if change > 0.5:
        return "Momentum haussier — setup BUY possible 📈"
    elif change < -0.5:
        return "Pression vendeuse — setup SELL possible 📉"
    else:
        return "Range en cours — attente breakout ⏳"

# ── Bâtir et envoyer le briefing ─────────────────────────────────────────────
def post_morning_brief() -> bool:
    """Construit et poste le briefing matin dans le canal PRO."""
    now    = datetime.now(timezone.utc)
    jours  = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
    jour   = jours[now.weekday()]
    date_s = now.strftime("%d/%m/%Y")

    prices  = _fetch_prices()
    news    = _fetch_news(3)

    # ── Section marchés ────────────────────────────────────────────────────
    gold   = prices.get("GOLD",   {})
    eurusd = prices.get("EURUSD", {})
    btc    = prices.get("BTC",    {})
    spx    = prices.get("SPX",    {})

    def fmt_price(p: dict, prefix="", suffix="", decimals=2) -> str:
        if not p:
            return "N/A"
        sign = "+" if p["change"] >= 0 else ""
        return f"{prefix}{p['price']:,.{decimals}f}{suffix} ({sign}{p['change']:.{1 if decimals >= 2 else 2}f}%)"

    gold_str   = fmt_price(gold,   prefix="",  suffix="$", decimals=0)
    eur_str    = fmt_price(eurusd, prefix="",  suffix="",  decimals=4)
    btc_str    = fmt_price(btc,    prefix="",  suffix="$", decimals=0)
    spx_change = f"{'+' if spx.get('change',0)>=0 else ''}{spx.get('change',0):.1f}%" if spx else "N/A"

    # ── Analyses ────────────────────────────────────────────────────────────
    gold_analyse   = _analyse(gold.get("change", 0))
    eurusd_analyse = _analyse(eurusd.get("change", 0))

    # ── Construction message ────────────────────────────────────────────────
    msg = (
        f"☀️ <b>BRIEFING QUANT — {jour} {date_s}</b>\n\n"
        f"📈 <b>MARCHÉS EN DIRECT :</b>\n"
        f"• 🥇 GOLD : {gold_str}\n"
        f"• 💶 EURUSD : {eur_str}\n"
        f"• ₿ BTC : {btc_str}\n"
        f"• 📊 S&amp;P500 : {spx_change} hier\n"
    )

    if news:
        msg += "\n📰 <b>NEWS DU JOUR :</b>\n"
        for h in news:
            msg += f"• {h}\n"

    msg += (
        f"\n🎯 <b>ACTIFS SURVEILLÉS AUJOURD'HUI :</b>\n"
        f"• GOLD — {gold_analyse}\n"
        f"• EURUSD — {eurusd_analyse}\n"
        f"\n⚡ Signaux en approche — restez attentifs 👀"
    )

    ok = _send(msg)
    if ok:
        logger.info(f"[Brief] ✅ Briefing posté ({jour} {date_s})")
    return ok

# ── Boucle principale ─────────────────────────────────────────────────────────
async def _scheduler_loop() -> None:
    """Vérifie toutes les 55s — envoie le briefing lun-ven à 07:30 UTC.
    Lock fichier /tmp/brief_YYYY-MM-DD pour éviter le re-envoi si redémarrage."""
    logger.info("☀️ market_brief démarré — briefing lun-ven 07:30 UTC")

    while True:
        now = datetime.now(timezone.utc)

        # Jours de marché: lundi(0) à vendredi(4)
        if now.weekday() <= 4:
            # 07:30 UTC ± 1 minute
            if now.hour == 7 and now.minute in (30, 31):
                lock_file = f"/tmp/brief_{now.strftime('%Y-%m-%d')}"
                if not os.path.exists(lock_file):
                    try:
                        post_morning_brief()
                        open(lock_file, "w").close()  # lock créé APRÈS envoi
                        logger.info(f"🔒 Anti-spam lock: {lock_file}")
                    except Exception as e:
                        logger.error(f"[Brief] scheduler: {e}")

        await asyncio.sleep(55)

if __name__ == "__main__":
    # Test immédiat en ligne de commande
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--test":
        logger.info("[Brief] Mode test — envoi immédiat")
        ok = post_morning_brief()
        print("✅ Envoyé" if ok else "❌ Échec")
    else:
        asyncio.run(_scheduler_loop())
