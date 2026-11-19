"""Executor: applies the selected tasks in order and verifies each one.

The executor is the only component that mutates the repository. It walks the
selection produced by :mod:`pasevsu_installer.selection`, applies the declared
operations through the closed operation set, then runs the task's read-only
verification operations. The first failure stops the run so the backup is always
one step away from a consistent tree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import operations
from .errors import OperationError
from .journal import Journal
from .model import Task
from .operations import FAILED, OpContext, OpResult, describe, plan_change
from .selection import Selection
from .util import utc_now

SUCCESS = "applied"
PARTIAL = "unchanged"
BROKEN = "failed"
PLANNED = "planned"


@dataclass(frozen=True)
class TaskOutcome:
    """Result of applying and verifying one task."""

    id: str
    agent: str
    title: str
    status: str
    changes: tuple[OpResult, ...] = ()
    verify: tuple[OpResult, ...] = ()
    duration_ms: float = 0.0
    error: str = ""
    fingerprint: str = ""

    @property
    def ok(self) -> bool:
        """Whether both the changes and the verification succeeded."""
        return self.status != BROKEN

    @property
    def wrote(self) -> bool:
        """Whether at least one mutation produced a new file version."""
        return any(item.status == operations.WRITE for item in self.changes)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every path this task touched or inspected."""
        seen: list[str] = []
        for item in (*self.changes, *self.verify):
            for path in item.paths:
                if path not in seen:
                    seen.append(path)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "agent": self.agent,
            "title": self.title,
            "status": self.status,
            "fingerprint": self.fingerprint,
            "duration_ms": round(self.duration_ms, 1),
            "paths": list(self.paths),
            "changes": [item.to_dict() for item in self.changes],
            "verify": [item.to_dict() for item in self.verify],
            "error": self.error,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Aggregate result of one migration run."""

    outcomes: tuple[TaskOutcome, ...] = ()
    dry_run: bool = False
    stopped_at: str | None = None
    fingerprint: str = ""

    @property
    def ok(self) -> bool:
        """Whether every task completed."""
        return all(outcome.ok for outcome in self.outcomes)

    @property
    def failed(self) -> tuple[TaskOutcome, ...]:
        """The tasks that failed."""
        return tuple(outcome for outcome in self.outcomes if not outcome.ok)

    @property
    def applied(self) -> tuple[TaskOutcome, ...]:
        """The tasks that actually changed files."""
        return tuple(outcome for outcome in self.outcomes if outcome.wrote)

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """Every path mutated by this run, de-duplicated and sorted."""
        seen: set[str] = set()
        for outcome in self.applied:
            seen.update(outcome.paths)
        return tuple(sorted(seen))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "dry_run": self.dry_run,
            "ok": self.ok,
            "fingerprint": self.fingerprint,
            "stopped_at": self.stopped_at,
            "tasks": [outcome.to_dict() for outcome in self.outcomes],
            "changed_paths": list(self.changed_paths),
        }


def execute(
    selection: Selection,
    root: Path,
    *,
    dry_run: bool = False,
    journal: Journal | None = None,
) -> ExecutionResult:
    """Apply every selected task, stopping at the first failure."""
    context = OpContext(root=root, selection=selection.to_dict())
    outcomes: list[TaskOutcome] = []
    stopped_at: str | None = None

    for task in selection.tasks:
        started = time.monotonic()
        outcome = _run_task(task, context, dry_run=dry_run)
        elapsed = (time.monotonic() - started) * 1000.0
        recorded = TaskOutcome(
            id=outcome.id,
            agent=outcome.agent,
            title=outcome.title,
            status=outcome.status,
            changes=outcome.changes,
            verify=outcome.verify,
            duration_ms=elapsed,
            error=outcome.error,
            fingerprint=outcome.fingerprint,
        )
        outcomes.append(recorded)
        if journal is not None:
            journal.append(
                "task",
                task=recorded.id,
                agent=recorded.agent,
                status=recorded.status,
                dry_run=dry_run,
                duration_ms=round(elapsed, 1),
                paths=list(recorded.paths),
                error=recorded.error,
            )
        if not recorded.ok:
            stopped_at = recorded.id
            break

    return ExecutionResult(
        outcomes=tuple(outcomes),
        dry_run=dry_run,
        stopped_at=stopped_at,
        fingerprint=selection.fingerprint,
    )


def _run_task(task: Task, context: OpContext, *, dry_run: bool) -> TaskOutcome:
    """Plan (dry-run) or apply one task and, when applying, run its checks."""
    changes: list[OpResult] = []
    verified: list[OpResult] = []
    error = ""
    status = SUCCESS

    for change in task.changes:
        try:
            result = plan_change(context, change) if dry_run else operations.apply_change(
                context, change
            )
        except OperationError as exc:
            changes.append(
                OpResult(change.op, FAILED, str(change.params.get("path") or ""), str(exc))
            )
            error = str(exc)
            status = BROKEN
            break
        changes.append(result)
        if result.status == FAILED:
            error = f"{describe(change)}: {result.detail}"
            status = BROKEN
            break

    if status != BROKEN and not dry_run:
        for change in task.verify:
            try:
                result = operations.apply_change(context, change)
            except OperationError as exc:
                verified.append(
                    OpResult(change.op, FAILED, str(change.params.get("path") or ""), str(exc))
                )
                error = str(exc)
                status = BROKEN
                break
            verified.append(result)
            if result.status == FAILED:
                error = f"проверка {describe(change)}: {result.detail}"
                status = BROKEN
                break

    if dry_run:
        return TaskOutcome(
            id=task.id,
            agent=task.agent_id,
            title=task.title,
            status=status if status == BROKEN else PLANNED,
            changes=tuple(changes),
            verify=tuple(verified),
            error=error,
            fingerprint=task.fingerprint(),
        )

    if status != BROKEN and not any(item.status == operations.WRITE for item in changes):
        status = PARTIAL

    return TaskOutcome(
        id=task.id,
        agent=task.agent_id,
        title=task.title,
        status=status,
        changes=tuple(changes),
        verify=tuple(verified),
        error=error,
        fingerprint=task.fingerprint(),
    )


def merge_state_tasks(
    previous: dict[str, Any], result: ExecutionResult, *, dry_run: bool
) -> dict[str, Any]:
    """Return the updated ``state["tasks"]`` mapping for a run."""
    tasks: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    for outcome in result.outcomes:
        if dry_run:
            continue
        entry = tasks.get(outcome.id)
        history = list(entry.get("history", [])) if isinstance(entry, dict) else []
        history.append(
            {
                "status": outcome.status,
                "at": utc_now(),
                "paths": list(outcome.paths),
            }
        )
        tasks[outcome.id] = {
            "agent": outcome.agent,
            "title": outcome.title,
            "status": outcome.status,
            "fingerprint": outcome.fingerprint,
            "paths": list(outcome.paths),
            "runs": int(entry.get("runs", 0)) + 1 if isinstance(entry, dict) else 1,
            "history": history[-20:],
        }
    return tasks


def summarise(result: ExecutionResult) -> str:
    """Return a one-line Bulgarian summary of a run."""
    applied = len(result.applied)
    total = len(result.outcomes)
    prefix = "пробен пуск (dry-run): " if result.dry_run else ""
    if result.ok:
        return f"{prefix}{total} задачи изпълнени, {applied} с реални промени"
    failed = ", ".join(outcome.id for outcome in result.failed) or "?"
    return f"{prefix}{applied}/{total} задачи с промени, спряно на {failed}"


@dataclass
class ExecutionLedger:
    """Mutable accumulator used by the CLI while a run is in progress."""

    outcomes: list[TaskOutcome] = field(default_factory=list)

    def add(self, outcome: TaskOutcome) -> None:
        """Record one outcome."""
        self.outcomes.append(outcome)
