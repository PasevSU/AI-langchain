"""Deterministic file operations declared by agent tasks.

Every change an agent is allowed to make is expressed as one of these named
operations. The registry is closed: an unknown ``op`` is a registry error, so an
agent can never execute arbitrary code. All paths are resolved inside the
repository root — traversal and absolute targets are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import OperationError
from .model import Change
from .util import dump_json, read_text, sha256_file, sha256_text

WRITE = "applied"
UNCHANGED = "unchanged"
FAILED = "failed"
PLANNED = "planned"

_READ_ONLY_OPS = frozenset({"assert_file_exists", "assert_file_contains", "assert_block_present"})


@dataclass(frozen=True)
class OpResult:
    """Outcome of one operation."""

    op: str
    status: str
    path: str | None = None
    detail: str = ""
    sha256_before: str | None = None
    sha256_after: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the operation applied cleanly or was already satisfied."""
        return self.status in (WRITE, UNCHANGED, PLANNED)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        payload: dict[str, Any] = {"op": self.op, "status": self.status, "detail": self.detail}
        if self.path is not None:
            payload["path"] = self.path
        if self.sha256_before is not None:
            payload["sha256_before"] = self.sha256_before
        if self.sha256_after is not None:
            payload["sha256_after"] = self.sha256_after
        return payload


@dataclass
class OpContext:
    """Everything an operation may read besides the repository itself."""

    root: Path
    selection: dict[str, Any] = field(default_factory=dict)


def is_read_only(op: str) -> bool:
    """Whether ``op`` only inspects the repository."""
    return op in _READ_ONLY_OPS


def resolve_target(root: Path, raw: Any) -> Path:
    """Resolve a declared relative path and guarantee it stays inside ``root``."""
    if not isinstance(raw, str) or not raw.strip():
        raise OperationError("операцията изисква непразен параметър 'path'")
    candidate = PurePosixPath(raw.replace("\\", "/"))
    if candidate.is_absolute() or (len(candidate.parts) > 0 and candidate.parts[0].endswith(":")):
        raise OperationError(f"забранен абсолютен път: {raw}")
    if any(part == ".." for part in candidate.parts):
        raise OperationError(f"забранено излизане извън репото: {raw}")
    resolved = (root / Path(*candidate.parts)).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise OperationError(f"пътят излиза извън репото: {raw}")
    return resolved


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise OperationError(f"операцията изисква непразен низов параметър '{key}'")
    return value


def _normalise_body(text: str) -> str:
    """Guarantee a single trailing newline (``end-of-file-fixer`` parity)."""
    return text.rstrip("\n") + "\n"


def _relative(root: Path, target: Path) -> str:
    return target.relative_to(root.resolve()).as_posix()


# --------------------------------------------------------------------------
# mutating operations
# --------------------------------------------------------------------------
def write_file_if_absent(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Create a file only when it does not exist yet."""
    target = resolve_target(ctx.root, params.get("path"))
    body = _normalise_body(_require_str(params, "content"))
    rel = _relative(ctx.root, target)
    if target.exists():
        return OpResult("write_file_if_absent", UNCHANGED, rel, "файлът вече съществува")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    return OpResult(
        "write_file_if_absent", WRITE, rel, "създаден", sha256_after=sha256_text(body)
    )


def write_file(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Create or replace a file, but only when the task opts into overwriting."""
    if params.get("allow_overwrite") is not True:
        raise OperationError(
            "write_file изисква 'allow_overwrite': true — иначе ползвайте "
            "write_file_if_absent"
        )
    target = resolve_target(ctx.root, params.get("path"))
    body = _normalise_body(_require_str(params, "content"))
    rel = _relative(ctx.root, target)
    before = sha256_file(target) if target.exists() else None
    if before == sha256_text(body):
        return OpResult("write_file", UNCHANGED, rel, "съдържанието е идентично")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    return OpResult(
        "write_file",
        WRITE,
        rel,
        "презаписан",
        sha256_before=before,
        sha256_after=sha256_text(body),
    )


def append_block_if_absent(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Append a marker-delimited block to a file at most once."""
    target = resolve_target(ctx.root, params.get("path"))
    marker = _require_str(params, "marker")
    body = _normalise_body(_require_str(params, "content"))
    rel = _relative(ctx.root, target)
    if marker not in body:
        raise OperationError(f"блокът трябва да съдържа маркера {marker!r}")
    existing = read_text(target) if target.exists() else ""
    if marker in existing:
        return OpResult("append_block_if_absent", UNCHANGED, rel, "блокът вече е наличен")
    separator = "" if not existing else ("" if existing.endswith("\n") else "\n")
    prefix = "\n" if existing.strip() else ""
    combined = existing + separator + prefix + body
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(combined, encoding="utf-8", newline="\n")
    return OpResult(
        "append_block_if_absent",
        WRITE,
        rel,
        "блокът е добавен",
        sha256_before=sha256_text(existing) if existing else None,
        sha256_after=sha256_text(combined),
    )


def replace_once(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Replace exactly one occurrence of a literal snippet."""
    target = resolve_target(ctx.root, params.get("path"))
    search = _require_str(params, "search")
    replacement = params.get("replacement")
    if not isinstance(replacement, str):
        raise OperationError("replace_once изисква низов параметър 'replacement'")
    rel = _relative(ctx.root, target)
    if not target.exists():
        return OpResult("replace_once", FAILED, rel, "файлът липсва")
    existing = read_text(target)
    occurrences = existing.count(search)
    if occurrences == 0:
        return OpResult("replace_once", FAILED, rel, "търсеният фрагмент липсва")
    if occurrences > 1:
        return OpResult(
            "replace_once", FAILED, rel, f"фрагментът се среща {occurrences} пъти (нееднозначно)"
        )
    combined = existing.replace(search, replacement, 1)
    target.write_text(combined, encoding="utf-8", newline="\n")
    return OpResult(
        "replace_once",
        WRITE,
        rel,
        "заменен",
        sha256_before=sha256_text(existing),
        sha256_after=sha256_text(combined),
    )


def emit_selection_manifest(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Write the current agent/task selection as a JSON manifest."""
    target = resolve_target(ctx.root, params.get("path"))
    rel = _relative(ctx.root, target)
    body = dump_json(ctx.selection)
    before = sha256_file(target) if target.exists() else None
    if before == sha256_text(body):
        return OpResult("emit_selection_manifest", UNCHANGED, rel, "манифестът е идентичен")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    return OpResult(
        "emit_selection_manifest",
        WRITE,
        rel,
        "манифестът е записан",
        sha256_before=before,
        sha256_after=sha256_text(body),
    )


# --------------------------------------------------------------------------
# read-only assertions (verification phase)
# --------------------------------------------------------------------------
def assert_file_exists(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Verify that a declared file exists."""
    target = resolve_target(ctx.root, params.get("path"))
    rel = _relative(ctx.root, target)
    if target.exists():
        return OpResult("assert_file_exists", UNCHANGED, rel, "файлът е наличен")
    return OpResult("assert_file_exists", FAILED, rel, "файлът липсва")


def assert_file_contains(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Verify that a file contains a literal snippet."""
    target = resolve_target(ctx.root, params.get("path"))
    needle = _require_str(params, "text")
    rel = _relative(ctx.root, target)
    if not target.exists():
        return OpResult("assert_file_contains", FAILED, rel, "файлът липсва")
    if needle in read_text(target):
        return OpResult("assert_file_contains", UNCHANGED, rel, "фрагментът е наличен")
    return OpResult("assert_file_contains", FAILED, rel, "фрагментът липсва")


def assert_block_present(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Verify that a marker-delimited block was appended exactly once."""
    target = resolve_target(ctx.root, params.get("path"))
    marker = _require_str(params, "marker")
    rel = _relative(ctx.root, target)
    if not target.exists():
        return OpResult("assert_block_present", FAILED, rel, "файлът липсва")
    occurrences = read_text(target).count(marker)
    if occurrences == 1:
        return OpResult("assert_block_present", UNCHANGED, rel, "блокът е наличен веднъж")
    return OpResult(
        "assert_block_present", FAILED, rel, f"маркерът се среща {occurrences} пъти"
    )


OPERATIONS: dict[str, Callable[[OpContext, dict[str, Any]], OpResult]] = {
    "write_file_if_absent": write_file_if_absent,
    "write_file": write_file,
    "append_block_if_absent": append_block_if_absent,
    "replace_once": replace_once,
    "emit_selection_manifest": emit_selection_manifest,
    "assert_file_exists": assert_file_exists,
    "assert_file_contains": assert_file_contains,
    "assert_block_present": assert_block_present,
}

KNOWN_OPS = frozenset(OPERATIONS)


def describe(change: Change) -> str:
    """Return a one-line human description of a change (used by dry runs)."""
    path = change.params.get("path", "?")
    return f"{change.op} → {path}"


def apply_change(ctx: OpContext, change: Change) -> OpResult:
    """Execute one declared change against the repository."""
    handler = OPERATIONS.get(change.op)
    if handler is None:
        raise OperationError(f"непозната операция: {change.op}")
    return handler(ctx, dict(change.params))


def plan_change(ctx: OpContext, change: Change) -> OpResult:
    """Predict the outcome of ``change`` without touching the repository."""
    if is_read_only(change.op):
        return apply_change(ctx, change)
    if change.op not in OPERATIONS:
        raise OperationError(f"непозната операция: {change.op}")
    target = resolve_target(ctx.root, change.params.get("path"))
    rel = _relative(ctx.root, target)
    exists = target.exists()
    if change.op == "write_file_if_absent":
        detail = "файлът вече съществува" if exists else "файлът ще бъде създаден"
        return OpResult(change.op, PLANNED, rel, detail)
    if change.op == "write_file":
        if change.params.get("allow_overwrite") is not True:
            raise OperationError(
                "write_file изисква 'allow_overwrite': true — иначе ползвайте "
                "write_file_if_absent"
            )
        detail = "файлът ще бъде презаписан" if exists else "файлът ще бъде създаден"
        return OpResult(change.op, PLANNED, rel, detail)
    if change.op == "append_block_if_absent":
        marker = _require_str(change.params, "marker")
        if exists and marker in read_text(target):
            return OpResult(change.op, UNCHANGED, rel, "блокът вече е наличен")
        return OpResult(change.op, PLANNED, rel, "блокът ще бъде добавен")
    if change.op == "replace_once":
        search = _require_str(change.params, "search")
        if not exists:
            return OpResult(change.op, FAILED, rel, "файлът липсва")
        occurrences = read_text(target).count(search)
        if occurrences == 0:
            return OpResult(change.op, FAILED, rel, "търсеният фрагмент липсва")
        if occurrences > 1:
            return OpResult(
                change.op,
                FAILED,
                rel,
                f"фрагментът се среща {occurrences} пъти (нееднозначно)",
            )
        return OpResult(change.op, PLANNED, rel, "фрагментът ще бъде заменен")
    return OpResult(change.op, PLANNED, rel, "промяната ще бъде приложена")
