"""Shared helpers for the installer test suite."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pasevsu_installer.model import AgentDeclaration, Change, Task

INSTALLER_DIR = Path(__file__).resolve().parents[1]
AGENTS_DIR = INSTALLER_DIR / "agents"
CONFIG_PATH = INSTALLER_DIR / "installer.config.json"


@contextmanager
def temp_root() -> Iterator[Path]:
    """Yield a throwaway directory that acts as a repository root."""
    with tempfile.TemporaryDirectory(prefix="pasevsu-installer-test-") as tmp:
        root = Path(tmp).resolve()
        yield root


def write_text(root: Path, relative: str, text: str) -> Path:
    """Write ``text`` to ``root/relative`` and return the path."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def dump_json(root: Path, relative: str, payload: Any) -> Path:
    """Serialise ``payload`` as JSON under ``root/relative``."""
    return write_text(root, relative, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def change(op: str, **params: Any) -> Change:
    """Build a :class:`Change` with keyword parameters."""
    return Change(op=op, params=dict(params))


def task(
    task_id: str,
    *,
    changes: tuple[Change, ...] = (),
    verify: tuple[Change, ...] = (),
    declared: bool = True,
    depends_on: tuple[str, ...] = (),
    agent_id: str = "test-agent",
) -> Task:
    """Build a declared task without going through the registry."""
    return Task(
        id=task_id,
        title=task_id,
        agent_id=agent_id,
        declared=declared,
        changes=changes,
        depends_on=depends_on,
        verify=verify,
    )


def declaration(
    agent_id: str = "test-agent",
    *,
    status: str = "active",
    tasks: tuple[Task, ...] = (),
) -> AgentDeclaration:
    """Build an agent declaration for selection tests."""
    return AgentDeclaration(
        id=agent_id,
        title=agent_id,
        status=status,
        tasks=tasks,
        source=f"{agent_id}.json",
    )
