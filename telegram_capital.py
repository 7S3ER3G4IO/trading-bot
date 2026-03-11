"""
telegram_capital.py — Notifications Capital.com améliorées.
Inclut : graphique, barre de progression, résumé de session,
         tableau de bord quotidien, alertes news, boutons interactifs.
"""
import os
import io
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict
from loguru import logger
import requests
try:
    from telegram import InlineKeyboardMarkup, InlineKeyboardButton
except ImportError:
    InlineKeyboardMarkup = None
    InlineKeyboardButton = None


TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")
_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""


# ─── Helpers internes ────────────────────────────────────────────────────────

def _stars(score: int, max_score: int = 3) -> str:
    """Affiche le score en étoiles : ⭐⭐☆ pour score=2/3."""
    return "⭐" * score + "☆" * (max_score - score)


def _progress_bar(current: float, sl: float, tp: float,
                  width: int = 10) -> str:
    """
    Barre visuelle entre SL et TP avec position actuelle du prix.
    Exemple : ▓▓▓▓●░░░░░  (+42%)
    """
    try:
        total = abs(tp - sl)
        if total == 0:
            return ""
        progress = abs(current - sl) / total
        progress = max(0.0, min(1.0, progress))
        pos = int(progress * width)
        bar = "▓" * pos + "●" + "░" * (width - pos)
        pct = progress * 100
        return f"{bar}  ({pct:.0f}%)"
    except Exception:
        return ""


def _send(text: str, markup=None):
    """Envoi texte HTML."""
    if not _API or not TELEGRAM_CHAT_ID:
        return
    try:
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        if markup:
            import json
            payload["reply_markup"] = json.dumps(markup.to_dict())
        r = requests.post(f"{_API}/sendMessage", json=payload, timeout=10)
        if not r.ok:
            logger.warning(f"⚠️ Telegram: {r.status_code} {r.text[:80]}")
    except Exception as e:
        logger.error(f"❌ Telegram send: {e}")


def _send_photo(image_bytes: bytes, caption: str, markup=None):
    """Envoi photo + légende."""
    if not _API or not TELEGRAM_CHAT_ID or not image_bytes:
        return
    try:
        files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
        data  = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        if markup:
            import json
            data["reply_markup"] = json.dumps(markup.to_dict())
        r = requests.post(f"{_API}/sendPhoto", data=data, files=files, timeout=30)
        if not r.ok:
            logger.warning(f"⚠️ Telegram photo: {r.status_code} {r.text[:80]}")
    except Exception as e:
        logger.error(f"❌ Telegram send_photo: {e}")


def _trade_buttons(instrument: str):
    """Boutons inline pour gérer le trade depuis Telegram. Retourne None si lib manquante."""
    if not InlineKeyboardMarkup or not InlineKeyboardButton:
        return None
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Fermer maintenant", callback_data=f"close:{instrument}"),
            InlineKeyboardButton("🟡 Passer en BE",      callback_data=f"be:{instrument}"),
        ],
        [
            InlineKeyboardButton("⏸ Pause bot",  callback_data="pause"),
            InlineKeyboardButton("▶️ Reprendre",  callback_data="resume"),
        ],
    ])


# ─── 1. Notification d'entrée complète (chart + texte + boutons) ───────────────

def notify_capital_entry(
    instrument: str,
    name: str,
    sig: str,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    size: float,
    score: int,
    session: str,
    range_pct: float,
    range_high: float,
    range_low: float,
    confirmations: list,
    df=None,            # DataFrame OHLCV pour le graphique
):
    """Notification d'ouverture de trade avec graphique attaché."""
    from brokers.capital_client import PIP_FACTOR as PIP
    pip   = PIP.get(instrument, 0.0001)
    emoji = "🟢 LONG" if sig == "BUY" else "🔴 SHORT"

    def pips(a, b): return round(abs(a - b) / pip)

    caption = (
        f"📈 <b>Capital.com Breakout — {name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji}   Session : {session} Open\n"
        f"📍 Entrée : <code>{entry:.5f}</code>\n"
        f"🛑 SL     : <code>{sl:.5f}</code>  ({pips(entry,sl)} pips)\n"
        f"🎯 TP1    : <code>{tp1:.5f}</code>  ({pips(entry,tp1)} pips)\n"
        f"🎯 TP2    : <code>{tp2:.5f}</code>  ({pips(entry,tp2)} pips)\n"
        f"🎯 TP3    : <code>{tp3:.5f}</code>  ({pips(entry,tp3)} pips)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{_stars(score)}  Score {score}/3  |  Range {range_pct:.2f}%\n"
        f"📦 3 × {size} unités\n"
        f"🔍 {' | '.join(confirmations)}"
    )

    buttons = _trade_buttons(instrument)

    # Génère et envoie le graphique
    if df is not None:
        try:
            from signal_card import generate_signal_card
            chart_bytes = generate_signal_card(
                df=df, instrument=instrument, direction="BUY" if sig == "BUY" else "SELL",
                entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                score=score, confirmations=confirmations,
                session=session,
            )
            if chart_bytes:
                _send_photo(chart_bytes, caption, markup=buttons)
                return
        except Exception as e:
            logger.debug(f"Signal card: {e}")

    # Fallback texte seul si chart échoue
    _send(caption, markup=buttons)


# ─── 2. Alerte TP1 + activation Break-Even ────────────────────────────────────

def notify_tp1_be(name: str, instrument: str, entry: float,
                  pips_tp1: float, size: float):
    """Notification quand TP1 est touché et SL déplacé en BE."""
    _send(
        f"🎯 <b>TP1 TOUCHÉ — {name}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ +{pips_tp1:.0f} pips | Position 1/{3} fermée\n"
        f"🟡 <b>Break-Even activé</b> sur TP2 + TP3\n"
        f"📍 SL déplacé à l'entrée : <code>{entry:.5f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔒 Risque = 0  |  {2} positions encore ouvertes"
    )


# ─── 3. Barre de progression (mise à jour prix) ────────────────────────────────

def notify_capital_progress(
    name: str,
    instrument: str,
    current_price: float,
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    tp3: float,
    tp1_hit: bool,
):
    """Message de suivi : montre la position du prix entre SL et TP."""
    from brokers.capital_client import PIP_FACTOR as PIP
    pip = PIP.get(instrument, 0.0001)
    pnl_pips = (current_price - entry) / pip if entry < tp1 else (entry - current_price) / pip

    # Barre vers TP2 (objectif principal)
    bar = _progress_bar(current_price, sl, tp2, width=12)
    be_status = "🟡 BE actif — risque 0" if tp1_hit else "⚠️ SL actif"

    tp1_str = '✅' if tp1_hit else f'{tp1:.5f}'
    _send(
        f"📊 <b>{name}</b> — Suivi position\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"SL ──{bar}── TP2\n"
        f"📍 Prix : <code>{current_price:.5f}</code>  "
        f"({'+' if pnl_pips >= 0 else ''}{pnl_pips:.0f} pips)\n"
        f"{be_status}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 TP1={tp1_str} | "
        f"TP2 {tp2:.5f} | TP3 {tp3:.5f}"
    )


# ─── 4. Résumé de session (fin London/NY) ──────────────────────────────────────

class SessionTracker:
    """Suit les trades d'une session pour le résumé de fin."""

    def __init__(self):
        self.trades: List[dict] = []
        self._session_start: Optional[str] = None

    def record_entry(self, name: str, sig: str, entry: float, size: float):
        self.trades.append({
            "name": name, "sig": sig, "entry": entry,
            "size": size, "pnl": None, "result": None,
        })

    def record_close(self, name: str, pnl: float, result: str):
        for t in reversed(self.trades):
            if t["name"] == name and t["pnl"] is None:
                t["pnl"] = pnl
                t["result"] = result
                break

    def send_session_recap(self, session: str, balance: float):
        """Envoie le résumé de session."""
        total  = len(self.trades)
        closed = [t for t in self.trades if t["pnl"] is not None]
        wins   = sum(1 for t in closed if t["pnl"] and t["pnl"] > 0)
        pnl    = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        wr     = wins / len(closed) * 100 if closed else 0

        lines = ""
        for t in self.trades:
            e  = "✅" if (t["pnl"] or 0) > 0 else ("⬜" if t["pnl"] is None else "❌")
            r  = f"{t['pnl']:+.2f}€" if t["pnl"] is not None else "🔄 ouvert"
            lines += f"{e} {t['name']}  {r}\n"

        trend = "📈" if pnl >= 0 else "📉"
        emoji_session = "🇬🇧" if session == "London" else "🗽"

        _send(
            f"{emoji_session} <b>Résumé session {session}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{lines}"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Trades : {total}  |  WR : <b>{wr:.0f}%</b>\n"
            f"{trend} PnL session : <b>{pnl:+.2f}€</b>\n"
            f"💰 Capital : <b>{balance:,.2f}€</b>"
        )
        self.trades.clear()


# ─── 5. Dashboard quotidien (à envoyer à 21h UTC) ──────────────────────────────

def send_daily_dashboard(
    balance: float,
    initial_balance: float,
    day_trades: List[dict],
    win_rate_instrument: Dict[str, float],
):
    """Dashboard quotidien complet."""
    total  = len(day_trades)
    wins   = sum(1 for t in day_trades if t.get("pnl", 0) > 0)
    total_pnl = sum(t.get("pnl", 0) for t in day_trades)
    wr     = wins / total * 100 if total > 0 else 0
    gain_pct = (balance - initial_balance) / initial_balance * 100 if initial_balance else 0
    trend  = "📈" if total_pnl >= 0 else "📉"

    # Top instruments
    instr_lines = ""
    for instr, wr_val in sorted(win_rate_instrument.items(),
                                 key=lambda x: x[1], reverse=True)[:5]:
        bar_w = int(wr_val / 10)
        instr_lines += f"<code>{instr:<10}</code> {'█' * bar_w}{'░' * (10-bar_w)} {wr_val:.0f}%\n"

    d = datetime.now(timezone.utc)
    date_str = d.strftime("%d/%m/%Y")

    no_trade_msg = "Aucun trade aujourd'hui"
    _send(
        f"📊 <b>DASHBOARD QUOTIDIEN — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Capital     : <b>{balance:,.2f}€</b>  ({gain_pct:+.2f}%)\n"
        f"{trend} PnL du jour : <b>{total_pnl:+.2f}€</b>\n"
        f"📋 Trades      : {total}  |  WR : <b>{wr:.0f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Performance par instrument :</b>\n"
        f"{instr_lines if instr_lines else no_trade_msg}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 Prochaine session : London 08h00 UTC 🇬🇧"
    )



# ─── 6. Alerte news économiques ────────────────────────────────────────────────

def notify_news_alert(event_name: str, currency: str, impact: str,
                      minutes_before: int):
    """Alerte avant un événement macro important."""
    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(impact.upper(), "⚪")
    _send(
        f"⚠️ <b>NEWS ÉCONOMIQUE IMMINENTE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{impact_emoji} <b>{event_name}</b>\n"
        f"💱 Devise concernée : {currency}\n"
        f"⏱ Dans <b>{minutes_before} min</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 Nouveaux signaux suspendus jusqu'après la publication ✅"
    )


def notify_news_resume(event_name: str):
    """Le bot reprend après la publication d'un événement."""
    _send(
        f"▶️ <b>Trading repris</b> — {event_name} publié\n"
        f"Bot actif et en surveillance ✅"
    )


# ─── 7. Notification macro (pause / reprise bot) ───────────────────────────────

def notify_bot_paused(reason: str = ""):
    _send(
        f"⏸ <b>BOT EN PAUSE</b>\n"
        f"{reason}\n"
        f"Utilisez /resume pour reprendre."
    )


def notify_bot_resumed():
    _send("▶️ <b>BOT REPRIS</b> — Surveillance active ✅")
