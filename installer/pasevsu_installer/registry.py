"""Loading and validating agent declarations (``installer/agents/*.json``).

An agent that wants changes applied to the repository must first *declare* them
in a file here. The declaration is the contract the selector uses: an agent with
no declared change task is never executed, and that exclusion is recorded with a
measurable reason.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import RegistryError
from .model import AGENT_SCHEMA, AGENT_STATUSES, AgentDeclaration, Change, Task
from .operations import KNOWN_OPS, is_read_only
from .util import read_text

ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_ID_LENGTH = 64


def load_declarations(registry_dir: Path) -> tuple[AgentDeclaration, ...]:
    """Load every declaration in ``registry_dir`` sorted by file name."""
    if not registry_dir.exists():
        raise RegistryError(f"липсва директория с декларации: {registry_dir}")
    files = sorted(path for path in registry_dir.glob("*.json") if path.is_file())
    if not files:
        raise RegistryError(f"в {registry_dir} няма нито една декларация (*.json)")

    declarations: list[AgentDeclaration] = []
    seen_agents: dict[str, str] = {}
    seen_tasks: dict[str, str] = {}
    for path in files:
        declaration = load_declaration(path)
        if declaration.id in seen_agents:
            raise RegistryError(
                f"дублиран агент {declaration.id!r} в {path.name} "
                f"(вече дефиниран в {seen_agents[declaration.id]})"
            )
        seen_agents[declaration.id] = path.name
        for task in declaration.tasks:
            if task.id in seen_tasks:
                raise RegistryError(
                    f"дублирана задача {task.id!r} в {path.name} (вече в {seen_tasks[task.id]})"
                )
            seen_tasks[task.id] = path.name
        declarations.append(declaration)
    return tuple(declarations)


def load_declaration(path: Path) -> AgentDeclaration:
    """Load and validate a single declaration file."""
    if not path.exists():
        raise RegistryError(f"липсва декларация: {path}")
    try:
        raw = json.loads(read_text(path))
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{path.name}: невалиден JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise RegistryError(f"{path.name}: очаква се JSON обект")

    schema = raw.get("schema", AGENT_SCHEMA)
    if schema != AGENT_SCHEMA:
        raise RegistryError(f"{path.name}: неподдържана схема {schema!r}")

    agent_id = _identifier(raw.get("id"), f"{path.name}: 'id'")
    status = str(raw.get("status", "active")).strip().lower()
    if status not in AGENT_STATUSES:
        raise RegistryError(
            f"{path.name}: невалиден status {status!r} (позволени: {', '.join(AGENT_STATUSES)})"
        )

    raw_tasks = raw.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raise RegistryError(f"{path.name}: 'tasks' трябва да е списък")

    tasks = tuple(
        _task(agent_id, item, path.name, index) for index, item in enumerate(raw_tasks)
    )
    return AgentDeclaration(
        id=agent_id,
        title=str(raw.get("title", agent_id)).strip() or agent_id,
        status=status,
        tasks=tasks,
        source=path.name,
        purpose=str(raw.get("purpose", "")).strip(),
        scope=tuple(str(item) for item in raw.get("scope", []) if str(item).strip()),
    )


def _task(agent_id: str, item: Any, source: str, index: int) -> Task:
    where = f"{source}: tasks[{index}]"
    if not isinstance(item, dict):
        raise RegistryError(f"{where}: очаква се обект")
    task_id = _identifier(item.get("id"), f"{where}.id")
    declared = item.get("declared", False)
    if not isinstance(declared, bool):
        raise RegistryError(f"{where}: 'declared' трябва да е true/false")
    depends = item.get("depends_on", [])
    if not isinstance(depends, list):
        raise RegistryError(f"{where}: 'depends_on' трябва да е списък")
    return Task(
        id=task_id,
        title=str(item.get("title", task_id)).strip() or task_id,
        agent_id=agent_id,
        declared=declared,
        changes=_changes(item.get("changes", []), f"{where}.changes", mutating_required=True),
        verify=_changes(item.get("verify", []), f"{where}.verify", mutating_required=False),
        depends_on=tuple(_identifier(value, f"{where}.depends_on") for value in depends),
        scope=tuple(str(value) for value in item.get("scope", []) if str(value).strip()),
        risk=str(item.get("risk", "low")).strip() or "low",
    )


def _changes(raw: Any, where: str, *, mutating_required: bool) -> tuple[Change, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RegistryError(f"{where}: очаква се списък")
    changes: list[Change] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise RegistryError(f"{where}[{index}]: очаква се обект")
        op = entry.get("op")
        if op not in KNOWN_OPS:
            raise RegistryError(
                f"{where}[{index}]: непозната операция {op!r} "
                f"(позволени: {', '.join(sorted(KNOWN_OPS))})"
            )
        if is_read_only(op) == mutating_required:
            kind = "променяща" if mutating_required else "само за четене"
            raise RegistryError(f"{where}[{index}]: {op!r} не е {kind} операция")
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise RegistryError(f"{where}[{index}]: 'params' трябва да е обект")
        changes.append(Change(op=str(op), params=dict(params), note=entry.get("note")))
    return tuple(changes)


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value) or len(value) > MAX_ID_LENGTH:
        raise RegistryError(
            f"{where}: {value!r} не е валиден идентификатор "
            f"(малки латински букви/цифри/тирета, започва с буква)"
        )
    return value


def summarise(declarations: tuple[AgentDeclaration, ...]) -> list[dict[str, Any]]:
    """Return a compact table of declarations for reports and the CLI."""
    return [declaration.to_dict() for declaration in declarations]
