"""Incident journal and small state store.

Two files live in `JOURNAL_DIR` (an `emptyDir` on this stand, so the journal is
deliberately ephemeral - a pod restart starts a new page):

* `incidents.jsonl` - append-only, one JSON object per line, for every triaged
  incident and every human decision. This is the artifact a human reads when
  asking "what did the bot do?", and it is the input for the M3 postmortem step.
* `state.json` - the Telegram update offset plus the dedupe table (fingerprint
  -> first seen time), written atomically so a crash cannot truncate it.

Nothing here is a database and nothing here is a source of truth for the
cluster: the cluster is.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class Journal:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / "incidents.jsonl"
        self.state_path = self.directory / "state.json"
        # RLock, not Lock: `dedupe_check_and_record` holds the lock and calls
        # `save_state`, which takes it again. With a plain Lock that is a
        # deadlock - and it presents as "the tests hang", not as an error.
        self._lock = threading.RLock()

    # --- events --------------------------------------------------------------
    def append_event(self, kind: str, payload: dict) -> dict:
        record = {"ts": time.time(), "kind": kind}
        record.update(payload)
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self._lock:
            with open(self.events_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return record

    def recent(self, limit: int = 20) -> list[dict]:
        """Last `limit` records, newest first. Tolerates a truncated last line."""
        if not self.events_path.exists():
            return []
        with self._lock:
            with open(self.events_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        records: list[dict] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a half-written line after a crash is not fatal
            if len(records) >= limit:
                break
        return records

    # --- state ---------------------------------------------------------------
    def load_state(self) -> dict:
        if not self.state_path.exists():
            return {"telegram_offset": 0, "dedupe": {}}
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"telegram_offset": 0, "dedupe": {}}
        state.setdefault("telegram_offset", 0)
        state.setdefault("dedupe", {})
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)

    # --- dedupe helpers ------------------------------------------------------
    def dedupe_check_and_record(self, key: str, window_seconds: int) -> bool:
        """True if `key` was NOT seen inside the window (and records it now)."""
        now = time.time()
        with self._lock:
            state = self.load_state()
            dedupe = {k: v for k, v in state.get("dedupe", {}).items()
                      if now - float(v) < window_seconds}
            seen = key in dedupe
            dedupe[key] = now
            state["dedupe"] = dedupe
            self.save_state(state)
        return not seen
