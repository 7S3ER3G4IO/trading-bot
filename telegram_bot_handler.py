"""
telegram_bot_handler.py — Nemesis v3.0 Hub Navigation + Polling
Routes /commands et inline buttons vers les pages du Hub.
"""
import os
import time
import threading
from typing import Optional, Callable, Tuple
from loguru import logger
from dotenv import load_dotenv
import requests

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    class InlineKeyboardButton:  # type: ignore
        def __init__(self, *a, **kw): pass
    class InlineKeyboardMarkup:  # type: ignore
        def __init__(self, *a, **kw): pass
        def to_json(self): return "{}"

load_dotenv()

POLL_TIMEOUT = 30


class TelegramBotHandler:
    """Polling Telegram + Hub page navigation via callbacks."""

    def __init__(self):
        self.token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._base   = f"https://api.telegram.org/bot{self.token}"
        self._offset  = 0
        self._paused  = False
        self._running = True

        # Callbacks depuis main.py
        self._get_status:      Optional[Callable[[], str]] = None
        self._get_trades:      Optional[Callable[[], Tuple[str, Optional[InlineKeyboardMarkup]]]] = None
        self._close_trade:     Optional[Callable[[str], str]] = None
        self._force_be:        Optional[Callable[[str], str]] = None
        self._pause_cb:        Optional[Callable[[], None]]   = None
        self._resume_cb:       Optional[Callable[[], None]]   = None
        self._get_performance: Optional[Callable[[], str]]    = None
        self._get_count:       Optional[Callable[[], str]]    = None
        self._get_equity:      Optional[Callable[[], str]]    = None
        # Hub page callbacks
        self._get_dashboard:   Optional[Callable] = None
        self._get_risk_page:   Optional[Callable] = None
        self._get_regime_page: Optional[Callable] = None
        self._get_stats_page:  Optional[Callable] = None
        self._get_hub_data:    Optional[Callable] = None  # returns (balance, pnl_today)

        if not self.token or not self.chat_id:
            logger.warning("⚠️  TelegramBotHandler désactivé")
            return
        logger.info("🤖 TelegramBotHandler v3.0 initialisé — Hub & Pages")

    def register_callbacks(self, get_status, get_trades, close_trade,
                           force_be, pause, resume,
                           get_performance=None, get_count=None, get_equity=None,
                           send_brief=None, send_backtest=None,
                           get_best_pair=None, get_risk=None, get_regime=None,
                           get_dashboard=None, get_risk_page=None,
                           get_regime_page=None, get_stats_page=None,
                           get_hub_data=None):
        self._get_status      = get_status
        self._get_trades      = get_trades
        self._close_trade     = close_trade
        self._force_be        = force_be
        self._pause_cb        = pause
        self._resume_cb       = resume
        self._get_performance = get_performance
        self._get_count       = get_count
        self._get_equity      = get_equity
        self._send_brief      = send_brief
        self._send_backtest   = send_backtest
        self._get_best_pair   = get_best_pair
        self._get_risk        = get_risk
        self._get_regime      = get_regime
        # Hub pages
        self._get_dashboard   = get_dashboard
        self._get_risk_page   = get_risk_page
        self._get_regime_page = get_regime_page
        self._get_stats_page  = get_stats_page
        self._get_hub_data    = get_hub_data

    def is_paused(self) -> bool:
        return self._paused

    def start_polling(self):
        if not self.token:
            return
        t = threading.Thread(target=self._poll_loop, daemon=True)
        t.start()
        logger.info("📱 Polling Telegram démarré (Hub mode)")

    def stop(self):
        self._running = False

    # ─── Core polling ─────────────────────────────────────────────────────────

    def _poll_loop(self):
        while self._running:
            try:
                updates = self._get_updates()
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    self._dispatch(upd)
            except Exception as e:
                logger.error(f"❌ Polling error : {e}")
                time.sleep(5)

    def _get_updates(self) -> list:
        try:
            r = requests.get(
                f"{self._base}/getUpdates",
                params={"timeout": POLL_TIMEOUT, "offset": self._offset},
                timeout=POLL_TIMEOUT + 5,
            )
            data = r.json()
            return data.get("result", []) if data.get("ok") else []
        except Exception:
            return []

    def _dispatch(self, upd: dict):
        if "message" in upd:
            self._handle_message(upd["message"])
        elif "callback_query" in upd:
            self._handle_callback(upd["callback_query"])

    # ─── Message Handler ──────────────────────────────────────────────────────

    def _handle_message(self, msg: dict):
        chat = str(msg.get("chat", {}).get("id", ""))
        if chat != str(self.chat_id):
            return
        text = msg.get("text", "").strip()
        parts = text.split()
        cmd   = parts[0].lower().split("@")[0] if parts else ""

        # All text commands redirect to Hub pages or send inline response
        if cmd == "/start":
            self._send_hub_message()

        elif cmd == "/help":
            self._reply(
                "⚡ <b>Nemesis v3.0 — Hub Mode</b>\n\n"
                "Utilisez /start pour ouvrir le <b>Command Center</b>\n"
                "avec navigation par boutons.\n\n"
                "Commandes directes disponibles :\n"
                "📊 /status — Solde & état\n"
                "📋 /trades — Positions actives\n"
                "📈 /performance — Stats complètes\n"
                "⏸️ /pause — Pauser le bot\n"
                "▶️ /resume — Reprendre\n"
                "🔴 /close GOLD — Fermer un trade\n"
            )
        elif cmd == "/status":
            text_out = self._get_status() if self._get_status else "Chargement..."
            self._reply(text_out)
        elif cmd == "/trades":
            if self._get_trades:
                text_out, markup = self._get_trades()
                self._reply(text_out, markup)
            else:
                self._reply("Aucun trade actif.")
        elif cmd == "/pause":
            self._paused = True
            if self._pause_cb:
                self._pause_cb()
            self._reply("⏸️ <b>Bot en pause</b> — /resume pour reprendre.")
        elif cmd == "/resume":
            self._paused = False
            if self._resume_cb:
                self._resume_cb()
            self._reply("▶️ <b>Bot actif</b> — Surveillance reprise.")
        elif cmd == "/close":
            if len(parts) < 2:
                self._reply("Usage : <code>/close GOLD</code>")
                return
            symbol = parts[1].upper()
            result = self._close_trade(symbol) if self._close_trade else "Erreur"
            self._reply(result)

        elif cmd == "/performance":
            if self._get_performance:
                self._reply(self._get_performance())
            else:
                self._reply("<code>Données non disponibles.</code>")
        elif cmd == "/count":
            if self._get_count:
                self._reply(self._get_count())
            else:
                self._reply("<code>Trades ouverts : 0</code>")
        elif cmd == "/equity":
            if self._get_equity:
                self._reply(self._get_equity())
            else:
                self._reply("<code>Historique equity non disponible.</code>")
        elif cmd == "/brief":
            self._reply("☕ <b>Morning Brief en cours...</b>")
            if hasattr(self, '_send_brief') and self._send_brief:
                threading.Thread(target=self._send_brief, daemon=True).start()
        elif cmd == "/backtest":
            symbol_raw = parts[1].upper() if len(parts) > 1 else "ETH"
            days = int(parts[2]) if len(parts) > 2 else 30
            self._reply(f"🧪 <b>Backtest {symbol_raw} {days}j lancé...</b>")
            if hasattr(self, '_send_backtest') and self._send_backtest:
                threading.Thread(
                    target=lambda: self._send_backtest(symbol_raw, days),
                    daemon=True
                ).start()

        elif cmd == "/best_pair":
            if self._get_best_pair:
                self._reply(self._get_best_pair())
            else:
                self._reply("🏆 <b>Données disponibles après 5+ trades.</b>")
        elif cmd == "/risk":
            if self._get_risk:
                self._reply(self._get_risk())
            else:
                self._reply("🛡️ <b>Données non disponibles.</b>")
        elif cmd == "/regime":
            if self._get_regime:
                self._reply(self._get_regime())
            else:
                self._reply("🧠 <b>Module HMM non disponible.</b>")
        elif cmd == "/hyperopt":
            self._reply("⚙️ <b>Optimisation</b>\nL'AB Tester intégré ajuste les paramètres automatiquement.")
        elif cmd in ("/force_close", "/fc"):
            if len(parts) < 2:
                self._reply("Usage : <code>/force_close GOLD</code>")
                return
            symbol = parts[1].upper()
            if self._close_trade:
                result = self._close_trade(symbol)
                self._reply(f"🔧 <b>Force-Close {symbol}</b>\n{result}")

    # ─── Callback Handler (Hub Navigation) ────────────────────────────────────

    def _handle_callback(self, cq: dict):
        cq_id = cq["id"]
        data  = cq.get("data", "")
        msg   = cq.get("message", {})
        msg_id = msg.get("message_id")
        self._answer_callback(cq_id)

        # ── Page Navigation ───────────────────────────────────────────────────
        if data == "nav:hub":
            self._edit_to_hub(msg_id)

        elif data == "page:dashboard":
            self._edit_to_dashboard(msg_id)

        elif data == "page:trades":
            self._edit_to_trades(msg_id)

        elif data == "page:performance":
            self._edit_to_performance(msg_id)

        elif data == "page:briefing":
            self._edit_to_briefing(msg_id)

        elif data == "page:risk":
            self._edit_to_risk(msg_id)

        elif data == "page:regime":
            self._edit_to_regime(msg_id)

        elif data == "page:stats":
            self._edit_to_stats(msg_id)

        elif data == "page:settings":
            self._edit_to_settings(msg_id)

        # ── Actions ───────────────────────────────────────────────────────────
        elif data == "action:pause":
            self._paused = True
            if self._pause_cb:
                self._pause_cb()
            self._edit_to_settings(msg_id)  # refresh settings page

        elif data == "action:resume":
            self._paused = False
            if self._resume_cb:
                self._resume_cb()
            self._edit_to_settings(msg_id)

        elif data.startswith("close:"):
            symbol = data.split(":")[1]
            result = self._close_trade(symbol) if self._close_trade else "Erreur"
            self._reply(result)

        elif data.startswith("be:"):
            symbol = data.split(":")[1]
            result = self._force_be(symbol) if self._force_be else "Erreur"
            self._reply(result)

    # ─── Page Builders (edit in-place) ────────────────────────────────────────

    def _send_hub_message(self):
        """Send a fresh Hub message (for /start command)."""
        from nemesis_ui.pages import PageBuilder
        bal, pnl = self._get_hub_balance()
        text, markup = PageBuilder.build_hub(bal, pnl)
        self._reply(text, markup)

    def _edit_to_hub(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        bal, pnl = self._get_hub_balance()
        text, markup = PageBuilder.build_hub(bal, pnl)
        self._edit_message(msg_id, text, markup)

    def _edit_to_dashboard(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_dashboard:
            text, markup = self._get_dashboard()
        else:
            bal, pnl = self._get_hub_balance()
            text, markup = PageBuilder.build_dashboard(
                balance=bal, pnl_today=pnl, pnl_total=0,
            )
        self._edit_message(msg_id, text, markup)

    def _edit_to_trades(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_trades:
            text_old, _ = self._get_trades()
            # Build new premium trades page
            text, markup = PageBuilder.build_trades()
            # Use old text if new page is empty
            if "Aucune position" in text and text_old:
                text = text_old
                markup = None
        else:
            text, markup = PageBuilder.build_trades()
        self._edit_message(msg_id, text, markup)

    def _edit_to_performance(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_performance:
            perf_text = self._get_performance()
            self._edit_message(msg_id, perf_text, self._back_markup())
        else:
            text, markup = PageBuilder.build_performance(
                wr=0, total_trades=0, wins=0,
                pnl_today=0, pnl_week=0, pnl_month=0, pnl_total=0,
                win_streak=0,
            )
            self._edit_message(msg_id, text, markup)

    def _edit_to_briefing(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        text, markup = PageBuilder.build_briefing_page()
        self._edit_message(msg_id, text, markup)
        # Trigger brief generation in background
        if hasattr(self, '_send_brief') and self._send_brief:
            threading.Thread(target=self._send_brief, daemon=True).start()

    def _edit_to_risk(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_risk_page:
            text, markup = self._get_risk_page()
        elif self._get_risk:
            text = self._get_risk()
            markup = self._back_markup()
        else:
            text, markup = PageBuilder.build_risk(
                balance=0, open_count=0, max_trades=10,
                dd_daily=0, dd_daily_limit=3, dd_monthly=0,
                paused=self._paused,
            )
        self._edit_message(msg_id, text, markup)

    def _edit_to_regime(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_regime_page:
            text, markup = self._get_regime_page()
        elif self._get_regime:
            text = self._get_regime()
            markup = self._back_markup()
        else:
            text, markup = PageBuilder.build_regime([])
        self._edit_message(msg_id, text, markup)

    def _edit_to_stats(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        if self._get_stats_page:
            text, markup = self._get_stats_page()
        else:
            text, markup = PageBuilder.build_stats(
                stats_block="Données en cours de chargement...",
                achievements_block="",
            )
        self._edit_message(msg_id, text, markup)

    def _edit_to_settings(self, msg_id: int):
        from nemesis_ui.pages import PageBuilder
        text, markup = PageBuilder.build_settings(paused=self._paused)
        self._edit_message(msg_id, text, markup)

    def _get_hub_balance(self) -> tuple:
        """Get balance and PnL for hub display."""
        if self._get_hub_data:
            try:
                return self._get_hub_data()
            except Exception:
                pass
        return 0.0, 0.0

    @staticmethod
    def _back_markup():
        """Simple back-to-hub markup."""
        try:
            from telegram import InlineKeyboardButton as IKB, InlineKeyboardMarkup as IKM
            return IKM([[IKB("🔙 Menu", callback_data="nav:hub")]])
        except ImportError:
            return None

    # ─── Helpers HTTP ─────────────────────────────────────────────────────────

    def _reply(self, text: str, markup=None):
        payload = {
            "chat_id":    self.chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        if markup:
            import json
            try:
                payload["reply_markup"] = json.dumps(markup.to_dict())
            except AttributeError:
                try:
                    payload["reply_markup"] = markup.to_json()
                except AttributeError:
                    pass
        try:
            requests.post(f"{self._base}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.error(f"❌ Reply error : {e}")

    def _edit_message(self, message_id: int, text: str, markup=None):
        """Edit a message in-place (Hub navigation)."""
        payload = {
            "chat_id":    self.chat_id,
            "message_id": message_id,
            "text":       text,
            "parse_mode": "HTML",
        }
        if markup:
            import json
            try:
                payload["reply_markup"] = json.dumps(markup.to_dict())
            except AttributeError:
                try:
                    payload["reply_markup"] = markup.to_json()
                except AttributeError:
                    pass
        try:
            r = requests.post(f"{self._base}/editMessageText", json=payload, timeout=10)
            if not r.ok:
                desc = r.json().get("description", "")
                if "not modified" not in desc:
                    logger.warning(f"⚠️ Edit: {r.status_code} {desc[:80]}")
        except Exception as e:
            logger.error(f"❌ Edit error : {e}")

    def _answer_callback(self, cq_id: str):
        try:
            requests.post(
                f"{self._base}/answerCallbackQuery",
                json={"callback_query_id": cq_id}, timeout=5
            )
        except Exception:
            pass

    # ─── Keyboards ────────────────────────────────────────────────────────────

    @staticmethod
    def trade_keyboard(symbol: str) -> InlineKeyboardMarkup:
        base = symbol.replace("/USDT", "")
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"🔴 Fermer {base}", callback_data=f"close:{symbol}"),
                InlineKeyboardButton("🔒 Forcer BE",      callback_data=f"be:{symbol}"),
            ],
            [
                InlineKeyboardButton("⏸️ Pause bot", callback_data="action:pause"),
                InlineKeyboardButton("▶️ Reprendre", callback_data="action:resume"),
            ]
        ])
