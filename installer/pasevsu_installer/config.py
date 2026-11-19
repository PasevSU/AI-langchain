"""Installer configuration (``installer/installer.config.json``).

The configuration is intentionally a plain JSON file so an operator can review
and change every gate — registry location, backup policy, test suites and
publish behaviour — without touching Python code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .util import read_text

CONFIG_SCHEMA = "pasevsu.installer.config.v1"
DEFAULT_CONFIG_REL = "installer/installer.config.json"


@dataclass(frozen=True)
class SuiteSpec:
    """One executable verification gate."""

    id: str
    command: tuple[str, ...]
    cwd: str = "."
    required: bool = True
    requires: tuple[str, ...] = ()
    skip_reason: str = ""
    timeout_s: int = 3600
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "command": list(self.command),
            "cwd": self.cwd,
            "required": self.required,
            "requires": list(self.requires),
            "skip_reason": self.skip_reason,
            "timeout_s": self.timeout_s,
            "description": self.description,
        }


@dataclass(frozen=True)
class Config:
    """Fully resolved installer configuration."""

    root: Path
    path: Path
    name: str = "pasevsu-installer-agent"
    version_file: str = "installer/VERSION"
    registry_dir: str = "installer/agents"
    state_dir: str = ".installer"
    release_notes_dir: str = "installer/RELEASE-NOTES"
    commit_scope: str = "installer"
    tag_prefix: str = "installer-v"
    backup_tag_prefix: str = "installer-backup"
    publish_enabled: bool = True
    push: bool = False
    allow_master: bool = False
    suites: tuple[SuiteSpec, ...] = field(default_factory=tuple)

    @property
    def registry_path(self) -> Path:
        """Absolute path of the agent declaration directory."""
        return self.root / self.registry_dir

    @property
    def version_path(self) -> Path:
        """Absolute path of the version file."""
        return self.root / self.version_file

    @property
    def release_notes_path(self) -> Path:
        """Absolute path of the release-notes directory."""
        return self.root / self.release_notes_dir

    def read_version(self) -> str:
        """Read the declared installer version (``0.0.0`` when absent)."""
        if not self.version_path.exists():
            return "0.0.0"
        return read_text(self.version_path).strip() or "0.0.0"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "schema": CONFIG_SCHEMA,
            "name": self.name,
            "version_file": self.version_file,
            "registry_dir": self.registry_dir,
            "state_dir": self.state_dir,
            "release_notes_dir": self.release_notes_dir,
            "commit_scope": self.commit_scope,
            "tag_prefix": self.tag_prefix,
            "backup_tag_prefix": self.backup_tag_prefix,
            "publish": {
                "enabled": self.publish_enabled,
                "push": self.push,
                "allow_master": self.allow_master,
            },
            "suites": [suite.to_dict() for suite in self.suites],
        }


def load_config(root: Path, path: Path | None = None) -> Config:
    """Load and validate the installer configuration for ``root``."""
    config_path = path or (root / DEFAULT_CONFIG_REL)
    if not config_path.exists():
        raise ConfigError(f"липсва конфигурация: {config_path}")
    try:
        raw = json.loads(read_text(config_path))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"невалиден JSON в {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: очаква се JSON обект")

    schema = raw.get("schema", CONFIG_SCHEMA)
    if schema != CONFIG_SCHEMA:
        raise ConfigError(f"{config_path}: неподдържана схема {schema!r}")

    suites = tuple(_suite_spec(item, config_path) for item in raw.get("test_suites", []))
    publish = raw.get("publish", {}) if isinstance(raw.get("publish", {}), dict) else {}
    backup = raw.get("backup", {}) if isinstance(raw.get("backup", {}), dict) else {}

    return Config(
        root=root,
        path=config_path,
        name=str(raw.get("name", "pasevsu-installer-agent")),
        version_file=str(raw.get("version_file", "installer/VERSION")),
        registry_dir=str(raw.get("registry_dir", "installer/agents")),
        state_dir=str(raw.get("state_dir", ".installer")),
        release_notes_dir=str(raw.get("release_notes_dir", "installer/RELEASE-NOTES")),
        commit_scope=str(raw.get("commit_scope", "installer")),
        tag_prefix=str(raw.get("tag_prefix", "installer-v")),
        backup_tag_prefix=str(backup.get("tag_prefix", "installer-backup")),
        publish_enabled=bool(publish.get("enabled", True)),
        push=bool(publish.get("push", False)),
        allow_master=bool(publish.get("allow_master", False)),
        suites=suites,
    )


def _suite_spec(item: Any, config_path: Path) -> SuiteSpec:
    if not isinstance(item, dict):
        raise ConfigError(f"{config_path}: всеки test_suite трябва да е обект")
    suite_id = item.get("id")
    command = item.get("command")
    if not suite_id or not isinstance(command, list) or not command:
        raise ConfigError(f"{config_path}: test_suite изисква 'id' и непразен 'command'")
    return SuiteSpec(
        id=str(suite_id),
        command=tuple(str(part) for part in command),
        cwd=str(item.get("cwd", ".")),
        required=bool(item.get("required", True)),
        requires=tuple(str(tool) for tool in item.get("requires", [])),
        skip_reason=str(item.get("skip_reason", "")),
        timeout_s=int(item.get("timeout_s", 3600)),
        description=str(item.get("description", "")),
    )
