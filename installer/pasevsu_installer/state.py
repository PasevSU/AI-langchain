"""First-run state, atomic persistence and a cooperative process lock.

The state file is the single source of truth for "has the installer agent ever
run here". Its existence is what makes the very first start trigger the real
migration; every later start is an idempotent re-check.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import LockError, StateError
from .util import atomic_write_text, dump_json, read_text, utc_now

STATE_SCHEMA = "pasevsu.installer.state.v1"
LOCK_TTL_S = 3600
GITIGNORE_BODY = """# Managed by the installer agent -- runtime artefacts, never committed.
backups/
*.tmp
.lock
report.md
journal.jsonl
selected-agents.json
"""


class StateStore:
    """Reads and writes ``<root>/.installer/state.json``."""

    def __init__(self, root: Path, state_dir: str = ".installer") -> None:
        """Bind the store to a repository root and a state directory name."""
        self.root = root
        self.state_dir = root / state_dir
        self.path = self.state_dir / "state.json"
        self.lock_path = self.state_dir / ".lock"

    # -- layout ---------------------------------------------------------
    def ensure(self) -> None:
        """Create the state directory (with a self-ignoring ``.gitignore``)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        ignore = self.state_dir / ".gitignore"
        if not ignore.exists():
            atomic_write_text(ignore, GITIGNORE_BODY)

    def artifacts(self, name: str) -> Path:
        """Return a path inside the state directory."""
        return self.state_dir / name

    # -- persistence ----------------------------------------------------
    def exists(self) -> bool:
        """Whether a state file has been written before."""
        return self.path.exists()

    def load(self) -> dict[str, Any]:
        """Load the state, returning an empty mapping when absent."""
        if not self.path.exists():
            return {}
        try:
            return json.loads(read_text(self.path))
        except json.JSONDecodeError as exc:
            raise StateError(f"повреден state файл: {self.path} ({exc})") from exc

    def save(self, data: dict[str, Any]) -> dict[str, Any]:
        """Persist ``data`` atomically, refreshing the ``updated_at`` stamp."""
        self.ensure()
        payload = dict(data)
        payload["schema"] = STATE_SCHEMA
        payload.setdefault("created_at", utc_now())
        payload["updated_at"] = utc_now()
        atomic_write_text(self.path, dump_json(payload))
        return payload

    def fresh(self, version: str) -> dict[str, Any]:
        """Return a brand-new state document for a first run."""
        return {
            "schema": STATE_SCHEMA,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "version": version,
            "phase": "new",
            "runs": 0,
            "first_run_started_at": None,
            "first_run_completed_at": None,
            "plan_fingerprint": None,
            "backup": None,
            "tasks": {},
            "suites": [],
            "publish": None,
            "history": [],
        }

    def is_first_run(self) -> bool:
        """Return ``True`` only when the installer has never run in this root."""
        state = self.load()
        if not state:
            return True
        return not state.get("first_run_started_at")

    def mark(self, state: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
        """Set the phase (and optional fields) and persist immediately."""
        merged = dict(state)
        merged["phase"] = phase
        merged.update(extra)
        return self.save(merged)

    # -- locking --------------------------------------------------------
    @contextmanager
    def lock(self, purpose: str = "install", ttl_s: int = LOCK_TTL_S) -> Iterator[None]:
        """Hold an exclusive lock for the duration of the block."""
        self.ensure()
        if self.lock_path.exists():
            holder = self._read_lock()
            age = self._lock_age_s()
            if age is not None and age < ttl_s:
                raise LockError(
                    f"друг процес на инсталатора държи ключалката "
                    f"(pid={holder.get('pid')}, цел={holder.get('purpose')}, "
                    f"преди {int(age)} s) — изтрийте {self.lock_path} ако е забил"
                )
            self.lock_path.unlink(missing_ok=True)
        self.lock_path.write_text(
            dump_json({"pid": _pid(), "purpose": purpose, "at": utc_now()}),
            encoding="utf-8",
        )
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def _read_lock(self) -> dict[str, Any]:
        try:
            return json.loads(read_text(self.lock_path))
        except (OSError, json.JSONDecodeError):
            return {}

    def _lock_age_s(self) -> float | None:
        try:
            return time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return None


def _pid() -> int:
    return os.getpid()
