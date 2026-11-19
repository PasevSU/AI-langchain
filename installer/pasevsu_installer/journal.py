"""Append-only JSONL journal.

Every decision the installer agent makes — selection, backup, each applied
change, each test suite, the published release — is written here so the run can
be audited afterwards without re-running anything.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .util import utc_now


class Journal:
    """Append-only event log stored next to the installer state."""

    def __init__(self, path: Path) -> None:
        """Bind the journal to ``path`` and ensure its directory exists."""
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one event and return the recorded record."""
        record: dict[str, Any] = {"at": utc_now(), "event": event, "pid": os.getpid()}
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=False)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
        return record

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the recorded events, newest last."""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    records.append(json.loads(stripped))
                except json.JSONDecodeError:
                    records.append({"at": None, "event": "unparsable", "raw": stripped})
        if limit is not None and limit >= 0:
            return records[-limit:]
        return records

    def count(self) -> int:
        """Return the number of recorded events."""
        return len(self.events())
