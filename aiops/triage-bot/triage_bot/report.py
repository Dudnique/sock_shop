"""The incident as a document, not as a message.

The chat is a notification channel: messages scroll, are unreadable in a week and
die with the chat. A report is the artifact somebody opens when the question is
"what did the bot decide, on what grounds, and who pressed what?" - so it keeps
the tier-by-tier comparison that the card deliberately hides, because this
reader is looking for exactly that.

Markdown, delivered by `sendDocument`: the bot needs no storage and no outbound
route beyond the Telegram API it already uses. Nothing here is executed, and
nothing here is a source of truth about the cluster - the cluster is.
"""

from __future__ import annotations

import time

from .telegram import short

#: A report is a record, not a log dump: the card already shows the first six.
MAX_EVIDENCE = 12


def _ts(value) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return "?"


def _cell(value, limit: int = 120) -> str:
    """A table cell: no pipes, no newlines, no runaway length."""
    text = str(value if value is not None else "").replace("|", "\\|")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def build_report(incident: dict, events: list | None = None) -> str:
    """One incident plus its human decisions, as Markdown."""
    labels = incident.get("labels") or {}
    annotations = incident.get("annotations") or {}
    final = incident.get("final") or {}
    ident = incident.get("id", "?")

    status = ("эскалация: бот не предлагает действий" if not incident.get("executable")
              else "план M1: команда показана человеку, но не исполнена")
    lines = [
        f"# Инцидент #{ident} · {incident.get('alertname', '?')}",
        "",
        f"- **начало:** {_ts(incident.get('ts'))}",
        f"- **severity:** {labels.get('severity', '?')}",
        f"- **статус:** {status}",
    ]
    scope = [f"`{key}={labels[key]}`" for key in ("namespace", "deployment", "pod", "node")
             if labels.get(key)]
    if scope:
        lines.append("- **scope:** " + ", ".join(scope))
    if incident.get("fingerprint"):
        lines.append(f"- **fingerprint:** `{incident['fingerprint']}`")

    lines += ["", "## Что случилось", "",
              "> " + (annotations.get("summary") or "(у алерта нет summary)")]
    if annotations.get("description"):
        lines += ["", annotations["description"]]

    lines += ["", "## Почему бот так решил", "",
              final.get("reason") or "(причина не записана)"]

    evidence = final.get("evidence") or []
    if evidence:
        lines += ["", "## На чём это основано", ""]
        lines += [f"- {short(item, 300)}" for item in evidence[:MAX_EVIDENCE]]

    command = final.get("command") or ""
    if command:
        lines += ["", "## Что сделать", "", "```text", command, "```"]
    elif annotations.get("runbook"):
        lines += ["", "## Что проверить (runbook алерта)", "",
                  "```text", annotations["runbook"], "```"]
    if final.get("proposal"):
        lines += ["", "## Предложение вне whitelist", "",
                  "Не исполняется: такого шага нет в `model.ACTIONS`.", "",
                  "> " + final["proposal"]]

    lines += ["", "## Решение", "",
              "| поле | значение |", "|---|---|",
              f"| действие | `{_cell(final.get('action_id', '?'))}` |",
              f"| уверенность | {_cell(final.get('confidence', '?'))} |",
              f"| исполнимо в M1 | {'да' if incident.get('executable') else 'нет'} |",
              f"| модель | {_cell(final.get('adapter') or '?')} |"]
    if final.get("total_tokens"):
        lines.append(f"| токены | {_cell(final['total_tokens'])} |")
    if final.get("validation_note"):
        lines.append(f"| валидатор | {_cell(final['validation_note'])} |")

    tiers = incident.get("tiers") or []
    if len(tiers) > 1:
        lines += ["", "## Каскад (для разбора качества)", "",
                  "| tier | адаптер | действие | уверенность | токены |",
                  "|---|---|---|---|---|"]
        for tier in tiers:
            decision = tier.get("decision") or {}
            lines.append("| " + " | ".join([
                _cell(tier.get("tier", "?")), _cell(tier.get("adapter", "?")),
                _cell(decision.get("action_id", "?")),
                _cell(decision.get("confidence", "?")),
                _cell(tier.get("tokens") or 0)]) + " |")
    if incident.get("route_note"):
        lines += ["", f"_{incident['route_note']}_"]

    missing = incident.get("missing") or []
    if missing:
        lines += ["", "## Не хватило данных", ""]
        lines += [f"- {short(item, 300)}" for item in missing]

    # The journal is append-only, so this is the honest answer to "and then
    # what?" - including presses that did nothing, which is the point.
    ordered = sorted(events or [], key=lambda item: float(item.get("ts") or 0))
    if ordered:
        lines += ["", "## Действия человека", ""]
        for event in ordered:
            detail = ""
            if event.get("command"):
                # The command belonged to the card, not to the press: saying so
                # keeps "advice" from reading like "approved this command".
                detail = f" · шаг: `{short(event['command'], 120)}`"
            lines.append(f"- {_ts(event.get('ts'))} · "
                         f"**{_cell(event.get('user') or '—')}** · "
                         f"{_cell(event.get('action') or event.get('kind') or '?')}{detail}")

    lines += ["", "## Метаданные", "",
              f"- отчёт собран: {_ts(time.time())}",
              f"- источники: {', '.join(incident.get('sources') or []) or '—'}",
              f"- сбор контекста: {incident.get('context_ms', 0)} мс",
              f"- dry_run: {incident.get('dry_run')}",
              ""]
    return "\n".join(lines)
