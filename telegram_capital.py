"""
telegram_capital.py — Notifications Capital.com v3.0 Multi-Channel
Routes all notifications to dedicated Nemesis channels.
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
from channels.router import ChannelRouter


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}" if TELEGRAM_TOKEN else ""

# ── Channel Router (singleton-like) ──────────────────────────────────────────
_router = ChannelRouter(TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None


# ─── Helpers internes ────────────────────────────────────────────────────────

def _stars(score: int, max_score: int = 3) -> str:
    return R.score_bar(score, max_score)


def _progress_bar(current: float, sl: float, tp: float, width: int = 10) -> str:
    return R.progress_bar(current, sl, tp, width)


def _format_duration(open_time) -> str:
    """Format trade duration as human-readable string."""
    if not open_time:
        return ""
    try:
        from datetime import datetime, timezone
        delta = datetime.now(timezone.utc) - open_time
        total_min = int(delta.total_seconds() / 60)
        if total_min < 60:
            return f"⏱ {total_min}min"
        hours = total_min // 60
        mins = total_min % 60
        return f"⏱ {hours}h{mins:02d}min"
    except Exception:
        return ""


def _calc_rr(entry: float, exit_price: float, sl: float) -> str:
    """Calculate actual R:R achieved."""
    risk = abs(entry - sl)
    if risk == 0:
        return ""
    reward = abs(exit_price - entry)
    rr = reward / risk
    return f"📐 R:R réel : <b>{rr:.1f}x</b>"


def _send_to_channel(channel: str, text: str):
    """Route to the correct dedicated channel."""
    if _router:
        _router.send_to(channel, text)


# Pre-signal dedup cache: avoid spamming same setup
_pre_signal_cache: dict = {}


def notify_pre_signal_alert(pre: dict):
    """
    Send rich pre-signal alert to Trades channel.
    pre: dict from Strategy.check_pre_signal()
    """
    if not pre or not _router:
        return

    symbol = pre.get("symbol", "?")

    # Dedup: skip if we already alerted this symbol in the same direction recently
    cache_key = f"{symbol}_{pre['direction']}"
    import time
    now = time.time()
    last_alert = _pre_signal_cache.get(cache_key, 0)
    if now - last_alert < 900:  # 15 min cooldown
        return
    _pre_signal_cache[cache_key] = now

    direction = pre["direction"]
    dir_emoji = "📈 LONG" if direction == "BUY" else "📉 SHORT"
    entry = pre["entry_est"]
    sl = pre["sl_est"]
    tp1 = pre["tp1_est"]
    tp2 = pre["tp2_est"]
    prox = int(pre.get("proximity_pct", 0))
    confs = pre.get("confirmations", [])
    missing = pre.get("missing", "?")
    current = pre.get("current_price", entry)

    # Proximity bar
    filled = min(prox // 10, 10)
    prox_bar = "█" * filled + "░" * (10 - filled)

    # R:R estimate
    risk = abs(entry - sl)
    rr_est = abs(tp2 - entry) / risk if risk > 0 else 0

    from config import CAPITAL_NAMES
    name = CAPITAL_NAMES.get(symbol, symbol)

    header = R.box_header(f"⏳ SETUP EN FORMATION — {name}")
    _send_to_channel("trades",
        f"{header}\n\n"
        f"{dir_emoji}  ·  Proximité : {prox_bar} <b>{prox}%</b>\n\n"
        f"╭── NIVEAUX ESTIMÉS ─────────╮\n"
        f"│ 📍 Entrée  <code>{entry:.5f}</code>\n"
        f"│ 🛑 SL      <code>{sl:.5f}</code>\n"
        f"│ 🎯 TP1     <code>{tp1:.5f}</code>\n"
        f"│ 🎯 TP2     <code>{tp2:.5f}</code>\n"
        f"╰────────────────────────────╯\n\n"
        f"📍 Prix actuel : <code>{current:.5f}</code>\n"
        f"📐 R:R estimé : <b>{rr_est:.1f}x</b>\n\n"
        f"✅ Confirmé : {' · '.join(confs) if confs else '—'}\n"
        f"⏳ Manque : <b>{missing}</b>\n\n"
        f"<i>Le bot ouvrira automatiquement si la dernière condition est remplie ✅</i>"
    )


def _send_photo_to_channel(channel: str, image_bytes: bytes, caption: str):
    """Send photo to a dedicated channel."""
    if not _API or not image_bytes:
        return
    from config import CHANNELS
    ch = CHANNELS.get(channel)
    if not ch:
        return
    try:
        files = {"photo": ("chart.png", io.BytesIO(image_bytes), "image/png")}
        data  = {"chat_id": ch["id"], "caption": caption, "parse_mode": "HTML"}
        r = requests.post(f"{_API}/sendPhoto", data=data, files=files, timeout=30)
        if not r.ok:
            logger.warning(f"⚠️ Telegram photo ({channel}): {r.status_code} {r.text[:80]}")
    except Exception as e:
        logger.error(f"❌ Telegram send_photo: {e}")


# ─── 1. Notification d'entrée complète → canal TRADES ─────────────────────────

def notify_capital_entry(
    instrument: str, name: str, sig: str,
    entry: float, sl: float, tp1: float, tp2: float, tp3: float,
    size: float, score: int, session: str,
    range_pct: float, range_high: float, range_low: float,
    confirmations: list, df=None,
):
    """Notification d'ouverture de trade premium → TRADES channel."""
    from brokers.capital_client import PIP_FACTOR as PIP
    pip = PIP.get(instrument, 0.0001)

    def pips(a, b): return round(abs(a - b) / pip)

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
                _send_photo_to_channel("trades", chart_bytes, caption)
                return
        except Exception as e:
            logger.debug(f"Signal card: {e}")

    _send_to_channel("trades", caption)


# ─── 2. Alerte TP1 + activation Break-Even → canal TRADES ─────────────────────

def notify_tp1_be(name: str, instrument: str, entry: float,
                  pips_tp1: float, size: float):
    header = R.box_header(f"🎯 TP1 TOUCHÉ — {name}")
    _send_to_channel("trades",
        f"{header}\n\n"
        f"✅ +{pips_tp1:.0f} pips | Position 1/3 fermée\n"
        f"🟡 <b>Break-Even activé</b> sur TP2 + TP3\n"
        f"📍 SL déplacé à l'entrée : <code>{entry:.5f}</code>\n\n"
        f"🔒 Risque = 0  ·  2 positions encore ouvertes"
    )


# ─── 3. Barre de progression → canal TRADES ──────────────────────────────────

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
    _send_to_channel("trades",
        f"{header}\n\n"
        f"{bar}\n"
        f"📍 Prix : <code>{current_price:.5f}</code>  "
        f"({'+' if pnl_pips >= 0 else ''}{pnl_pips:.0f} pips)\n"
        f"{be_status}\n\n"
        f"🎯 TP1={tp1_str} | TP2 {tp2:.5f} | TP3 {tp3:.5f}"
    )


# ─── 4. Résumé de session → canal PERFORMANCE ─────────────────────────────────

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
        _send_to_channel("performance",
            f"{header}\n\n"
            f"{lines}\n"
            f"Trades : {total}  ·  WR : <b>{wr:.0f}%</b>\n"
            f"{trend} PnL session : <b>{pnl:+.2f}€</b>\n"
            f"💰 Capital : <b>{balance:,.2f}€</b>"
        )
        self.trades.clear()


# ─── 5. Dashboard quotidien → canal DASHBOARD ────────────────────────────────

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

    _send_to_channel("dashboard",
        f"{header}\n\n"
        f"💰 Capital : <b>{balance:,.2f}€</b>  ({gain_pct:+.2f}%)\n"
        f"{trend} PnL du jour : <b>{total_pnl:+.2f}€</b>\n"
        f"📋 Trades : {total}  ·  WR : <b>{wr:.0f}%</b>\n\n"
        + (f"<b>Performance par instrument :</b>\n{instr_lines}" if instr_lines else "Aucun trade aujourd'hui\n")
        + f"\n🔄 Prochaine session : London 08h00 UTC 🇬🇧"
    )


# ─── 6. Alerte news économiques → canal RISK ──────────────────────────────────

def notify_news_alert(event_name: str, currency: str, impact: str,
                      minutes_before: int):
    impact_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(impact.upper(), "⚪")
    header = R.box_header("⚠️ NEWS ÉCONOMIQUE")
    _send_to_channel("risk",
        f"{header}\n\n"
        f"{impact_emoji} <b>{event_name}</b>\n"
        f"💱 Devise : {currency}\n"
        f"⏱ Dans <b>{minutes_before} min</b>\n\n"
        f"🤖 Signaux suspendus jusqu'après publication ✅"
    )


def notify_news_resume(event_name: str):
    _send_to_channel("risk",
        f"▶️ <b>Trading repris</b> — {event_name} publié\n"
        f"Bot actif et en surveillance ✅"
    )


# ─── 7. Notification macro → canal RISK ──────────────────────────────────────

def notify_bot_paused(reason: str = ""):
    header = R.box_header("⏸ BOT EN PAUSE")
    _send_to_channel("risk", f"{header}\n\n{reason}\nUtilisez /resume pour reprendre.")


def notify_bot_resumed():
    _send_to_channel("dashboard", "▶️ <b>BOT REPRIS</b> — Surveillance active ✅")
