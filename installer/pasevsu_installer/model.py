"""Typed data model shared by every installer-agent component.

The model is intentionally immutable: a *declaration* describes what an agent
promises to change, a *task* is the unit of that promise, and a *change* is one
deterministic file operation. Nothing here touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AGENT_SCHEMA = "pasevsu.installer.agent.v1"
STATE_SCHEMA = "pasevsu.installer.state.v1"
CONFIG_SCHEMA = "pasevsu.installer.config.v1"

AGENT_STATUSES = ("active", "disabled")


@dataclass(frozen=True)
class Change:
    """A single deterministic file operation declared by a task."""

    op: str
    params: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        payload: dict[str, Any] = {"op": self.op, "params": dict(self.params)}
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class Task:
    """One declared unit of work owned by exactly one agent."""

    id: str
    title: str
    agent_id: str
    declared: bool
    changes: tuple[Change, ...]
    depends_on: tuple[str, ...] = ()
    verify: tuple[Change, ...] = ()
    scope: tuple[str, ...] = ()
    risk: str = "low"

    @property
    def writes_files(self) -> bool:
        """Whether the task contains at least one mutating operation."""
        return any(change.op in MUTATING_OPS for change in self.changes)

    def fingerprint(self) -> str:
        """Return a stable digest of the task's declared behaviour."""
        from .util import sha256_text  # local import keeps the model dependency-free

        payload = dump_fingerprint(
            {
                "id": self.id,
                "agent": self.agent_id,
                "changes": [change.to_dict() for change in self.changes],
                "verify": [change.to_dict() for change in self.verify],
            }
        )
        return sha256_text(payload)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "title": self.title,
            "agent": self.agent_id,
            "declared": self.declared,
            "depends_on": list(self.depends_on),
            "risk": self.risk,
            "scope": list(self.scope),
            "changes": [change.to_dict() for change in self.changes],
            "verify": [change.to_dict() for change in self.verify],
            "fingerprint": self.fingerprint(),
        }


@dataclass(frozen=True)
class AgentDeclaration:
    """A file-level agent declaration loaded from the registry directory."""

    id: str
    title: str
    status: str
    tasks: tuple[Task, ...]
    source: str
    purpose: str = ""
    scope: tuple[str, ...] = ()

    @property
    def declared_tasks(self) -> tuple[Task, ...]:
        """Tasks that carry at least one declared change."""
        return tuple(task for task in self.tasks if task.declared and task.changes)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "purpose": self.purpose,
            "scope": list(self.scope),
            "source": self.source,
            "tasks": [task.id for task in self.tasks],
            "declared_tasks": [task.id for task in self.declared_tasks],
        }


MUTATING_OPS = frozenset(
    {
        "write_file_if_absent",
        "write_file",
        "append_block_if_absent",
        "replace_once",
        "emit_selection_manifest",
    }
)


def dump_fingerprint(payload: Any) -> str:
    """Serialise ``payload`` canonically for hashing."""
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
