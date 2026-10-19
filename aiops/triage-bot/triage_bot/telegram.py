"""Telegram notification and approval.

Two things this module is careful about:

* **HTML escaping.** Everything that comes from a model, an alert or a log line
  is escaped before it reaches `parse_mode=HTML`. A log line containing `<` was
  the classic way to make a Telegram send fail with a 400 - and that failure
  looks like "the bot is broken" while the bot is fine.
* **The keyboard is built from the whitelist, not from the text.** The approve
  button exists only when the final decision is an action from `model.ACTIONS`.
  An escalation (including one carrying a free-text `proposal`) gets no approve
  button at all: there is nothing executable to approve, and pretending
  otherwise is how a human learns to click without reading.

Long polling (`getUpdates`) is used instead of a webhook, so the bot needs no
incoming route from the internet - it only makes outbound calls, exactly like
Alertmanager does with its own Telegram receiver.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from .config import Settings

log = logging.getLogger("aiops.telegram")

API_BASE = "https://api.telegram.org"

#: Decisions a human can press. `approve` is only offered for whitelist actions.
#: The labels say what actually happens in M1 - an earlier "только совет" promised
#: advice and only wrote a journal line, which is exactly the kind of label a
#: human stops trusting.
DECISIONS = {
    "approve": "✅ применить (план M1)",
    "advice": "📝 без действий",
    "ignore": "🔇 не беспокоить 4 ч",
}


def esc(value) -> str:
    """Escape for Telegram's HTML parse mode (quotes included, like any HTML)."""
    return html.escape(str(value), quote=True)


def short(value, limit: int = 160) -> str:
    """One evidence line stays one line: a card, not a log dump."""
    text = str(value).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, settings: Settings, metrics=None) -> None:
        self.settings = settings
        self.metrics = metrics
        self._token = settings.telegram_token()
        self.chat_id = settings.telegram_chat_id
        self._sent: deque = deque()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._token and self.chat_id)

    def _call(self, method: str, payload: dict, timeout: float = 15.0) -> dict:
        if not self._token:
            raise TelegramError("no bot token")
        request = urllib.request.Request(
            f"{API_BASE}/bot{self._token}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # The URL carries the token, so only the API's own description is
            # logged - never the request line.
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise TelegramError(f"{method}: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramError(f"{method}: {exc}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TelegramError(f"{method}: not JSON: {body[:200]!r}") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', 'not ok')}")
        return data.get("result") or {}

    # --- outbound ------------------------------------------------------------
    def _rate_limited(self) -> bool:
        """A hard ceiling on messages per hour: a loop must not become spam."""
        now = time.time()
        with self._lock:
            while self._sent and now - self._sent[0] > 3600:
                self._sent.popleft()
            if len(self._sent) >= self.settings.telegram_max_per_hour:
                return True
            self._sent.append(now)
            return False

    def send_message(self, text: str, keyboard: list | None = None) -> bool:
        if not self.enabled:
            log.info("telegram is not configured - message not sent")
            return False
        if self._rate_limited():
            log.warning("telegram rate limit reached (%s/h) - message suppressed",
                        self.settings.telegram_max_per_hour)
            if self.metrics:
                self.metrics.inc("aiops_telegram_suppressed")
            return False
        payload = {"chat_id": self.chat_id, "text": text[:4000],
                   "parse_mode": "HTML", "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self._call("sendMessage", payload)
        except TelegramError as exc:
            log.error("telegram sendMessage failed: %s", exc)
            return False
        if self.metrics:
            self.metrics.inc("aiops_telegram_sent")
        return True

    def answer_callback(self, callback_id: str, text: str) -> None:
        try:
            self._call("answerCallbackQuery",
                       {"callback_query_id": callback_id, "text": text[:190]})
        except TelegramError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    def clear_keyboard(self, message_id) -> None:
        try:
            self._call("editMessageReplyMarkup",
                       {"chat_id": self.chat_id, "message_id": message_id,
                        "reply_markup": {"inline_keyboard": []}})
        except TelegramError as exc:
            log.warning("editMessageReplyMarkup failed: %s", exc)

    def get_updates(self, offset: int, timeout: int = 25) -> list:
        result = self._call("getUpdates",
                            {"offset": offset, "timeout": timeout,
                             "allowed_updates": json.dumps(
                                 ["callback_query", "message"])},
                            timeout=timeout + 10)
        return result if isinstance(result, list) else []

    # --- polling loop --------------------------------------------------------
    def poll_loop(self, on_callback, on_message, state, stop_event,
                  save_state=None) -> None:
        """Long polling in a background thread.

        The offset and the dedupe table live in the journal's `state.json` (via
        `save_state`), so a restart does not replay old button presses.
        """
        if not self.enabled:
            log.info("telegram poller not started (telegram is not configured)")
            return
        log.info("telegram poller started")
        while not stop_event.is_set():
            offset = int(state.get("telegram_offset", 0))
            try:
                updates = self.get_updates(offset)
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                stop_event.wait(5)
                continue
            for update in updates:
                state["telegram_offset"] = int(update.get("update_id", offset)) + 1
                callback = update.get("callback_query")
                if callback:
                    try:
                        on_callback(callback)
                    except Exception as exc:  # never kill the poller
                        log.exception("callback handling failed: %s", exc)
                elif update.get("message"):
                    try:
                        on_message(update["message"])
                    except Exception as exc:
                        log.exception("message handling failed: %s", exc)
                if save_state:
                    save_state()


def build_keyboard(incident_id: str, executable: bool) -> list:
    """Buttons come from the whitelist decision, never from model text.

    `executable=False` (an escalation, or an escalation carrying a proposal)
    means there is nothing to approve - so there is no approve button to press.
    """
    row: list = []
    if executable:
        row.append({"text": DECISIONS["approve"],
                    "callback_data": f"approve:{incident_id}"})
    row.append({"text": DECISIONS["advice"], "callback_data": f"advice:{incident_id}"})
    row.append({"text": DECISIONS["ignore"], "callback_data": f"ignore:{incident_id}"})
    return [row]


def render_card(incident: dict) -> str:
    """The message a human receives: cause -> facts -> what to do -> cost.

    The order is the point. The stand once sent "нужен инженер" with no facts:
    the decision was right and the card was useless. So the cause comes first,
    the evidence it rests on second, and only then the step - a whitelisted
    command, a free-text proposal, or the alert's own runbook when nothing may
    be executed. The bot's own bookkeeping is one compact footer line.
    """
    labels = incident.get("labels") or {}
    annotations = incident.get("annotations") or {}
    final = incident.get("final") or {}

    # A distinct opening line on purpose: this card lands in the same chat as
    # Alertmanager's own plain `[FIRING] ...` messages, and a human scanning the
    # chat has to see at a glance which message carries the analysis.
    lines = [
        f"🤖 <b>AIOps: разбор инцидента</b> · <code>#{esc(incident.get('id', ''))}</code>",
        "",
        f"🔥 <b>{esc(incident.get('alertname', '?'))}</b> · {esc(labels.get('severity', '?'))}",
    ]
    scope = [f"{key} <code>{esc(labels[key])}</code>"
             for key in ("namespace", "deployment", "pod", "node")
             if labels.get(key)]
    if scope:
        lines.append(" · ".join(scope))
    if annotations.get("summary"):
        lines.append(f"<i>{esc(annotations['summary'])}</i>")

    if final.get("reason"):
        # A quoted block, not just a bold word: Telegram draws a vertical bar,
        # so the cause stands out instead of blending into one wall of text.
        lines += ["", "<b>ПОЧЕМУ</b>", f"<blockquote>{esc(final['reason'])}</blockquote>"]

    evidence = final.get("evidence") or []
    if evidence:
        lines += ["", "<b>ЧТО УВИДЕЛ</b>"]
        lines.extend(f"• {esc(short(item))}" for item in evidence[:6])

    command = final.get("command") or ""
    if command:
        lines += ["", "<b>ЧТО СДЕЛАТЬ</b>", f"<pre>{esc(command)}</pre>",
                  "<i>M1 команду не исполняет: это шаг для человека.</i>"]
    if final.get("proposal"):
        lines += ["", "⚠️ <b>ПРЕДЛОЖЕНИЕ ВНЕ WHITELIST</b>",
                  "<i>не исполняется, нужен инженер</i>",
                  f"<blockquote>{esc(final['proposal'])}</blockquote>"]
    # When nothing is executable, the rule's own runbook is the most concrete
    # thing that can be put in front of a human - and the bot did not invent it.
    if annotations.get("runbook") and not command:
        lines += ["", "<b>ЧТО ПРОВЕРИТЬ</b>",
                  f"<pre>{esc(annotations['runbook'])}</pre>"]

    # Only what a reader needs in order to judge the answer: who answered and
    # what it cost. The tier-by-tier comparison stays in the journal - it is a
    # quality measurement, not something a person should read at 3 a.m.
    meta = [esc(final.get("adapter") or "?")]
    tokens = final.get("total_tokens")
    if tokens:
        meta.append(f"{esc(tokens)} токенов")
    lines += ["", f"<i>{' · '.join(meta)}</i>"]

    missing = incident.get("missing") or []
    if missing:
        lines.append("<i>⚠️ не хватило данных: "
                     f"{esc('; '.join(short(item, 90) for item in missing[:2]))}</i>")
    if final.get("validation_note"):
        # Tolerated, not hidden: the reader can see that the bot repaired the
        # answer instead of trusting it.
        lines.append(f"<i>валидатор: {esc(final['validation_note'])}</i>")
    return "\n".join(lines)
