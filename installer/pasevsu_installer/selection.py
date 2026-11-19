"""Selection: deciding which agents and tasks may run.

Rule of the install: **only agents with explicitly declared change tasks are
executed**. Everything else is excluded, and every exclusion carries a reason an
operator can read. Tasks already applied with an identical fingerprint are
skipped, so repeated runs converge instead of duplicating work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .errors import RegistryError
from .model import AgentDeclaration, Task, dump_fingerprint
from .util import sha256_text

APPLIED = "applied"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class Exclusion:
    """A deliberate, explained non-execution."""

    kind: str
    id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {"kind": self.kind, "id": self.id, "reason": self.reason}


@dataclass(frozen=True)
class Selection:
    """The ordered work list plus the audit trail of what was left out."""

    tasks: tuple[Task, ...] = ()
    exclusions: tuple[Exclusion, ...] = ()
    regenerated: tuple[str, ...] = ()
    fingerprint: str = ""

    @property
    def task_ids(self) -> tuple[str, ...]:
        """Identifiers of the selected tasks, in execution order."""
        return tuple(task.id for task in self.tasks)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        """De-duplicated agent identifiers in execution order."""
        seen: list[str] = []
        for task in self.tasks:
            if task.agent_id not in seen:
                seen.append(task.agent_id)
        return tuple(seen)

    @property
    def empty(self) -> bool:
        """Whether there is no work to do."""
        return not self.tasks

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "fingerprint": self.fingerprint,
            "agents": list(self.agent_ids),
            "tasks": [
                {
                    "id": task.id,
                    "agent": task.agent_id,
                    "title": task.title,
                    "risk": task.risk,
                    "changes": len(task.changes),
                    "fingerprint": task.fingerprint(),
                }
                for task in self.tasks
            ],
            "regenerated": list(self.regenerated),
            "exclusions": [item.to_dict() for item in self.exclusions],
        }


def select(
    declarations: Sequence[AgentDeclaration],
    state: Mapping[str, Any] | None = None,
    *,
    only: Iterable[str] | None = None,
) -> Selection:
    """Build the execution plan from declarations, state and an optional filter."""
    wanted = {value.strip() for value in only} if only is not None else None
    known = {declaration.id for declaration in declarations}
    if wanted is not None:
        unknown = sorted(wanted - known)
        if unknown:
            raise RegistryError(f"непознати агенти в --agents: {', '.join(unknown)}")

    tasks_by_id: dict[str, Task] = {}
    agent_of: dict[str, str] = {}
    exclusions: list[Exclusion] = []
    applied: Mapping[str, Any] = _state_tasks(state)

    for declaration in declarations:
        if declaration.status != "active":
            exclusions.append(
                Exclusion("agent", declaration.id, f"status={declaration.status} → изключен")
            )
            continue
        if wanted is not None and declaration.id not in wanted:
            exclusions.append(
                Exclusion("agent", declaration.id, "не е поискан изрично (--agents)")
            )
            continue
        if not declaration.tasks:
            exclusions.append(Exclusion("agent", declaration.id, "няма дефинирани задачи"))
            continue
        for task in declaration.tasks:
            if not task.declared:
                exclusions.append(
                    Exclusion("task", task.id, "няма ясно заявена промяна (declared=false)")
                )
                continue
            if not task.changes:
                exclusions.append(
                    Exclusion("task", task.id, "заявена, но без нито една промяна")
                )
                continue
            previous = applied.get(task.id) if isinstance(applied, Mapping) else None
            if (
                isinstance(previous, Mapping)
                and previous.get("status") == APPLIED
                and previous.get("fingerprint") == task.fingerprint()
            ):
                exclusions.append(
                    Exclusion("task", task.id, "вече приложена (идентичен fingerprint)")
                )
                continue
            tasks_by_id[task.id] = task
            agent_of[task.id] = declaration.id

    regenerated: list[str] = []
    for task in tasks_by_id.values():
        previous = applied.get(task.id)
        if not isinstance(previous, Mapping):
            continue
        if previous.get("status") != APPLIED:
            continue
        if previous.get("fingerprint") != task.fingerprint():
            regenerated.append(task.id)

    ordered = _topological(list(tasks_by_id.values()), tasks_by_id, applied, exclusions)
    fingerprint = sha256_text(
        dump_fingerprint([[task.id, task.fingerprint()] for task in ordered])
    )
    return Selection(
        tasks=tuple(ordered),
        exclusions=tuple(exclusions),
        regenerated=tuple(regenerated),
        fingerprint=fingerprint,
    )


def _state_tasks(state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(state, Mapping):
        return {}
    tasks = state.get("tasks")
    return tasks if isinstance(tasks, Mapping) else {}


def _topological(
    tasks: list[Task],
    index: Mapping[str, Task],
    applied: Mapping[str, Any],
    exclusions: list[Exclusion],
) -> list[Task]:
    """Order tasks so every dependency runs first; reject cycles."""
    pending = list(tasks)
    ordered: list[Task] = []
    placed: set[str] = set()
    dropped: set[str] = set()

    while pending:
        progressed = False
        remaining: list[Task] = []
        for task in pending:
            blocked: str | None = None
            for dependency in task.depends_on:
                if dependency in placed:
                    continue
                if dependency in index:
                    blocked = f"зависимост {dependency} още не е изпълнена"
                    break
                if dependency in dropped:
                    blocked = f"зависимост {dependency} е изключена"
                    break
                previous = applied.get(dependency)
                if isinstance(previous, Mapping) and previous.get("status") == APPLIED:
                    continue
                raise RegistryError(
                    f"задача {task.id!r}: зависимост {dependency!r} не съществува "
                    "в декларациите"
                )
            if blocked is None:
                ordered.append(task)
                placed.add(task.id)
                progressed = True
            else:
                remaining.append(task)
        if not progressed:
            remaining_ids = {task.id for task in remaining}
            cyclic = sorted(
                task.id
                for task in remaining
                if any(dependency in remaining_ids for dependency in task.depends_on)
            )
            if cyclic:
                raise RegistryError(
                    f"циклична зависимост между задачи: {', '.join(cyclic)}"
                )
            for task in remaining:
                dropped.add(task.id)
                exclusions.append(Exclusion("task", task.id, "блокирана зависимост"))
            break
        pending = remaining
    return ordered


@dataclass
class SelectionReport:
    """Flat view used by the CLI and the markdown report."""

    selection: Selection
    excluded_agents: tuple[Exclusion, ...] = field(default_factory=tuple)
    excluded_tasks: tuple[Exclusion, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        payload = self.selection.to_dict()
        payload["excluded_agents"] = [item.to_dict() for item in self.excluded_agents]
        payload["excluded_tasks"] = [item.to_dict() for item in self.excluded_tasks]
        return payload


def report(selection: Selection) -> SelectionReport:
    """Split the exclusions into agent-level and task-level buckets."""
    return SelectionReport(
        selection=selection,
        excluded_agents=tuple(item for item in selection.exclusions if item.kind == "agent"),
        excluded_tasks=tuple(item for item in selection.exclusions if item.kind == "task"),
    )
