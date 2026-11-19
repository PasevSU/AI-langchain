"""pasevsu installer agent — gated first-run migration for the AI-langchain repo.

The package is deliberately stdlib-only: it must be able to run before any
dependency is installed, because its job is to *perform* the installation.

Public surface::

    from pasevsu_installer import cli, load_config

    config = load_config(Path("."))
    raise SystemExit(cli.run(["install", "--dry-run"]))
"""

from __future__ import annotations

from .backup import Backup, create_backup, verify_backup
from .cli import main, run
from .config import Config, SuiteSpec, load_config
from .errors import (
    BackupError,
    ConfigError,
    InstallerError,
    LockError,
    OperationError,
    PublishError,
    RegistryError,
    StateError,
)
from .executor import ExecutionResult, TaskOutcome, execute
from .gui import build_snapshot, write_gui, write_snapshot
from .journal import Journal
from .model import AgentDeclaration, Change, Task
from .publisher import Publication, publish
from .registry import load_declaration, load_declarations
from .selection import Selection, SelectionReport, select
from .state import StateStore
from .tests_runner import SuiteOutcome, TestReport, advisories, blockers, run_suites

__version__ = "0.1.13"

__all__ = [
    "AgentDeclaration",
    "Backup",
    "BackupError",
    "Change",
    "Config",
    "ConfigError",
    "ExecutionResult",
    "InstallerError",
    "Journal",
    "LockError",
    "OperationError",
    "Publication",
    "PublishError",
    "RegistryError",
    "Selection",
    "SelectionReport",
    "StateError",
    "StateStore",
    "SuiteOutcome",
    "SuiteSpec",
    "Task",
    "TaskOutcome",
    "TestReport",
    "__version__",
    "advisories",
    "blockers",
    "build_snapshot",
    "create_backup",
    "execute",
    "load_config",
    "load_declaration",
    "load_declarations",
    "main",
    "publish",
    "run",
    "run_suites",
    "select",
    "verify_backup",
    "write_gui",
    "write_snapshot",
]
