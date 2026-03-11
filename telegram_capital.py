"""
telegram_capital.py — Notifications Capital.com v3.0
Intègre le design system Nemesis et les formatters premium.
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

from nemesis_ui.renderer import NemesisRenderer as R
from nemesis_ui.notifications import NotificationFormatter as NF


TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")
_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""


# ─── Helpers internes ────────────────────────────────────────────────────────

def _stars(score: int, max_score: int = 3) -> str:
    return R.score_bar(score, max_score)


def _progress_bar(current: float, sl: float, tp: float, width: int = 10) -> str:
    return R.progress_bar(current, sl, tp, width)


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
    if not InlineKeyboardMarkup or not InlineKeyboardButton:
        return None
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❌ Fermer maintenant", callback_data=f"close:{instrument}"),
            InlineKeyboardButton("🟡 Passer en BE",      callback_data=f"be:{instrument}"),
        ],
        [
            InlineKeyboardButton("⏸ Pause bot",  callback_data="action:pause"),
            InlineKeyboardButton("▶️ Reprendre",  callback_data="action:resume"),
        ],
    ])


# ─── 1. Notification d'entrée complète (chart + texte + boutons) ───────────────

def notify_capital_entry(
    instrument: str, name: str, sig: str,
    entry: float, sl: float, tp1: float, tp2: float, tp3: float,
    size: float, score: int, session: str,
    range_pct: float, range_high: float, range_low: float,
    confirmations: list, df=None,
):
    """Notification d'ouverture de trade premium avec graphique."""
    from brokers.capital_client import PIP_FACTOR as PIP
    pip = PIP.get(instrument, 0.0001)

    def pips(a, b): return round(abs(a - b) / pip)

    # Premium formatted caption
    rr = R.format_rr(entry, sl, tp2)
    score_bar = R.score_bar(score, 3)

    caption = (
        f"{R.box_header(f'📈 Capital.com — {name}')}\n\n"
        f"{'🟢 LONG' if sig == 'BUY' else '🔴 SHORT'}  ·  {session} Open\n"
        f"{score_bar}  Score {score}/3  ·  {rr}\n\n"
        f"╭── NIVEAUX ─────────────────╮\n"
        f"│ 📍 Entrée <code>{entry:.5f}</code>\n"
        f"│ 🛑 SL    <code>{sl:.5f}</code>  ({pips(entry,sl)} pips)\n"
        f"│ 🎯 TP1   <code>{tp1:.5f}</code>  ({pips(entry,tp1)} pips)\n"
        f"│ 🎯 TP2   <code>{tp2:.5f}</code>  ({pips(entry,tp2)} pips)\n"
        f"│ 🎯 TP3   <code>{tp3:.5f}</code>  ({pips(entry,tp3)} pips)\n"
        f"╰────────────────────────────╯\n\n"
        f"📦 3 × {size} unités  ·  Range {range_pct:.2f}%\n"
        f"🔬 {' · '.join(confirmations)}"
    )

    buttons = _trade_buttons(instrument)

    if df is not None:
        try:
            from signal_card import generate_signal_card
            chart_bytes = generate_signal_card(
                df=df, instrument=instrument,
                direction="BUY" if sig == "BUY" else "SELL",
                entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                score=score, confirmations=confirmations,
                session=session,
            )
            if chart_bytes:
                _send_photo(chart_bytes, caption, markup=buttons)
                return
        except Exception as e:
            logger.debug(f"Signal card: {e}")

    _send(caption, markup=buttons)


# ─── 2. Alerte TP1 + activation Break-Even ────────────────────────────────────

def notify_tp1_be(name: str, instrument: str, entry: float,
                  pips_tp1: float, size: float):
    header = R.box_header(f"🎯 TP1 TOUCHÉ — {name}")
    _send(
        f"{header}\n\n"
        f"✅ +{pips_tp1:.0f} pips | Position 1/3 fermée\n"
        f"🟡 <b>Break-Even activé</b> sur TP2 + TP3\n"
        f"📍 SL déplacé à l'entrée : <code>{entry:.5f}</code>\n\n"
        f"🔒 Risque = 0  ·  2 positions encore ouvertes"
    )


# ─── 3. Barre de progression (mise à jour prix) ────────────────────────────────

def notify_capital_progress(
    name: str, instrument: str, current_price: float,
    entry: float, sl: float,
    tp1: float, tp2: float, tp3: float, tp1_hit: bool,
):
    """Message de suivi — position du prix entre SL et TP."""
    from brokers.capital_client import PIP_FACTOR as PIP
    pip = PIP.get(instrument, 0.0001)
    pnl_pips = (current_price - entry) / pip if entry < tp1 else (entry - current_price) / pip

    bar = R.progress_bar(current_price, sl, tp2, width=12)
    be_status = "🟡 BE actif — risque 0" if tp1_hit else "⚠️ SL actif"

    tp1_str = '✅' if tp1_hit else f'{tp1:.5f}'

    header = R.box_header(f"📊 {name} — Suivi")
    _send(
        f"{header}\n\n"
        f"{bar}\n"
        f"📍 Prix : <code>{current_price:.5f}</code>  "
        f"({'+' if pnl_pips >= 0 else ''}{pnl_pips:.0f} pips)\n"
        f"{be_status}\n\n"
        f"🎯 TP1={tp1_str} | TP2 {tp2:.5f} | TP3 {tp3:.5f}"
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

        header = R.box_header(f"{emoji_session} Résumé {session}")
        _send(
            f"{header}\n\n"
            f"{lines}\n"
            f"Trades : {total}  ·  WR : <b>{wr:.0f}%</b>\n"
            f"{trend} PnL session : <b>{pnl:+.2f}€</b>\n"
            f"💰 Capital : <b>{balance:,.2f}€</b>"
        )
        self.trades.clear()


# ─── 5. Dashboard quotidien ──────────────────────────────────────────────────

def send_daily_dashboard(
    balance: float, initial_balance: float,
    day_trades: List[dict],
    win_rate_instrument: Dict[str, float],
):
    total  = len(day_trades)
    wins   = sum(1 for t in day_trades if t.get("pnl", 0) > 0)
    total_pnl = sum(t.get("pnl", 0) for t in day_trades)
    wr = wins / total * 100 if total > 0 else 0
    gain_pct = (balance - initial_balance) / initial_balance * 100 if initial_balance else 0
    trend = "📈" if total_pnl >= 0 else "📉"

    instr_lines = ""
    for instr, wr_val in sorted(win_rate_instrument.items(),
                                 key=lambda x: x[1], reverse=True)[:5]:
        bar = R.wr_bar(wr_val, 100, 10)
        instr_lines += f"<code>{instr:<10}</code> {bar} {wr_val:.0f}%\n"

    d = datetime.now(timezone.utc)
    date_str = d.strftime("%d/%m/%Y")

    header = R.box_header(f"📊 DASHBOARD — {date_str}")

    _send(
        f"{header}\n\n"
        f"💰 Capital : <b>{balance:,.2f}€</b>  ({gain_pct:+.2f}%)\n"
        f"{trend} PnL du jour : <b>{total_pnl:+.2f}€</b>\n"
        f"📋 Trades : {total}  ·  WR : <b>{wr:.0f}%</b>\n\n"
        + (f"<b>Performance par instrument :</b>\n{instr_lines}" if instr_lines else "Aucun trade aujourd'hui\n")
        + f"\n🔄 Prochaine session : London 08h00 UTC 🇬🇧"
    )


# ─── 6. Alerte news économiques ────────────────────────────────────────────────

def notify_news_alert(event_name: str, currency: str, impact: str,
                      minutes_before: int):
    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(impact.upper(), "⚪")
    header = R.box_header("⚠️ NEWS ÉCONOMIQUE")
    _send(
        f"{header}\n\n"
        f"{impact_emoji} <b>{event_name}</b>\n"
        f"💱 Devise : {currency}\n"
        f"⏱ Dans <b>{minutes_before} min</b>\n\n"
        f"🤖 Signaux suspendus jusqu'après publication ✅"
    )


def notify_news_resume(event_name: str):
    _send(
        f"▶️ <b>Trading repris</b> — {event_name} publié\n"
        f"Bot actif et en surveillance ✅"
    )


# ─── 7. Notification macro ───────────────────────────────────────────────────

def notify_bot_paused(reason: str = ""):
    header = R.box_header("⏸ BOT EN PAUSE")
    _send(f"{header}\n\n{reason}\nUtilisez /resume pour reprendre.")


def notify_bot_resumed():
    _send("▶️ <b>BOT REPRIS</b> — Surveillance active ✅")
