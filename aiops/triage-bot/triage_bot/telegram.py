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
DECISIONS = {
    "approve": "применить (план M1)",
    "advice": "только совет",
    "ignore": "игнор 4 ч",
}


def esc(value) -> str:
    """Escape for Telegram's HTML parse mode (quotes included, like any HTML)."""
    return html.escape(str(value), quote=True)


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


_TIER_NAMES = {"rules": "правила (tier 0)", "model": "модель (tier 1)"}


def render_card(incident: dict) -> str:
    """The message a human receives: what fired, what was decided, why, cost."""
    labels = incident.get("labels") or {}
    annotations = incident.get("annotations") or {}
    lines = [
        f"🔥 <b>{esc(incident.get('alertname', '?'))}</b>"
        f" [{esc(labels.get('severity', '?'))}] · <code>{esc(incident.get('id', ''))}</code>",
    ]
    scope = [f"{key}=<code>{esc(labels[key])}</code>"
             for key in ("namespace", "deployment", "pod", "node")
             if labels.get(key)]
    if scope:
        lines.append(" · ".join(scope))
    if annotations.get("summary"):
        lines.append(esc(annotations["summary"]))

    tiers = incident.get("tiers") or []
    if len(tiers) > 1:
        lines.append("\n<b>варианты:</b>")
        for tier in tiers:
            decision = tier.get("decision") or {}
            lines.append(
                f"• {esc(_TIER_NAMES.get(tier.get('tier'), tier.get('tier', '?')))}"
                f" — <code>{esc(decision.get('action_id', '?'))}</code>"
                f" {esc(decision.get('confidence', '?'))}"
                f" ({esc(tier.get('adapter', ''))}, {esc(tier.get('latency_ms', 0))} мс)")

    final = incident.get("final") or {}
    lines.append(f"\n<b>итог:</b> <code>{esc(final.get('action_id', '?'))}</code>"
                 f" · уверенность {esc(final.get('confidence', '?'))}"
                 f" · ответил: {esc(final.get('adapter', '?'))}")
    if incident.get("route_note"):
        lines.append(f"<i>{esc(incident['route_note'])}</i>")

    if final.get("reason"):
        lines.append(f"\n<b>почему:</b> {esc(final['reason'])}")

    evidence = final.get("evidence") or []
    if evidence:
        lines.append("\n<b>доказательства:</b>")
        lines.extend(f"• {esc(item)}" for item in evidence[:6])

    command = final.get("command") or ""
    if command:
        lines.append(f"\n<b>команда (M1 не исполняет):</b>\n<code>{esc(command)}</code>")
    if final.get("proposal"):
        lines.append("\n⚠️ <b>ПРЕДЛОЖЕНИЕ ВНЕ WHITELIST (не исполняется, "
                     "нужен инженер):</b>")
        lines.append(esc(final["proposal"]))

    sources = incident.get("sources") or []
    missing = incident.get("missing") or []
    footer = [f"контекст: {esc(', '.join(sources) if sources else 'нет')}"]
    if missing:
        footer.append(f"пробелы: {esc('; '.join(str(m) for m in missing[:2]))}")
    tokens = final.get("total_tokens")
    if tokens:
        footer.append(f"токенов: {esc(tokens)}")
    lines.append("\n<i>" + " · ".join(footer) + "</i>")
    return "\n".join(lines)
