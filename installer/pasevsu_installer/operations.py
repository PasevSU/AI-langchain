"""Deterministic file operations declared by agent tasks.

Every change an agent is allowed to make is expressed as one of these named
operations. The registry is closed: an unknown ``op`` is a registry error, so an
agent can never execute arbitrary code. All paths are resolved inside the
repository root — traversal and absolute targets are rejected.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import OperationError
from .model import Change
from .util import UI_ASSETS_DIR, dump_json, read_text, sha256_file, sha256_text

WRITE = "applied"
UNCHANGED = "unchanged"
FAILED = "failed"
PLANNED = "planned"

_READ_ONLY_OPS = frozenset(
    {"assert_file_exists", "assert_file_contains", "assert_block_present", "assert_version"}
)

#: Файловете, в които версията трябва да съвпада (подразбиране за операциите с версия).
VERSION_SITES: tuple[str, ...] = (
    "installer/VERSION",
    "installer/pyproject.toml",
    "installer/pasevsu_installer/__init__.py",
)

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PY_VERSION_RE = re.compile(r'(?m)^(__version__\s*=\s*)"[^"]*"')
_TOML_VERSION_RE = re.compile(r'(?m)^(version\s*=\s*)"[^"]*"')


@dataclass(frozen=True)
class OpResult:
    """Outcome of one operation."""

    op: str
    status: str
    path: str | None = None
    detail: str = ""
    sha256_before: str | None = None
    sha256_after: str | None = None
    #: Допълнителни файлове, които операцията е пипнала (напр. всички места за версия).
    extra_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the operation applied cleanly or was already satisfied."""
        return self.status in (WRITE, UNCHANGED, PLANNED)

    @property
    def paths(self) -> tuple[str, ...]:
        """Every repository path this result refers to."""
        return ((self.path,) if self.path else ()) + self.extra_paths

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        payload: dict[str, Any] = {"op": self.op, "status": self.status, "detail": self.detail}
        if self.path is not None:
            payload["path"] = self.path
        if self.extra_paths:
            payload["extra_paths"] = list(self.extra_paths)
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
# version sites
# --------------------------------------------------------------------------
def _version_sites(ctx: OpContext, params: dict[str, Any]) -> tuple[Path, ...]:
    """Return the files a version change must touch, in declaration order."""
    raw = params.get("sites")
    if raw is None:
        return tuple(resolve_target(ctx.root, item) for item in VERSION_SITES)
    if not isinstance(raw, list) or not raw:
        raise OperationError("операцията изисква 'sites' да е непразен списък от пътища")
    return tuple(resolve_target(ctx.root, item) for item in raw)


def _version_pattern(target: Path) -> re.Pattern[str] | None:
    """Return the regex carrying the version, or ``None`` for a bare VERSION file."""
    if target.name == "VERSION":
        return None
    if target.suffix == ".py":
        return _PY_VERSION_RE
    if target.suffix in {".toml", ".cfg"}:
        return _TOML_VERSION_RE
    raise OperationError(f"неподдържан файл за версия: {target.name}")


def _current_version(target: Path) -> str:
    """Read the version declared in one site, or raise ``OperationError``."""
    if not target.is_file():
        raise OperationError(f"липсва файл за версия: {target.name}")
    text = read_text(target)
    pattern = _version_pattern(target)
    if pattern is None:
        return text.strip()
    match = pattern.search(text)
    if match is None:
        raise OperationError(f"в {target.name} липсва ред за версия")
    return match.group(0).split('"', 2)[1]


def _next_body(target: Path, version: str) -> str:
    """Return the file body with the version replaced by ``version``."""
    if not target.is_file():
        raise OperationError(f"липсва файл за версия: {target.name}")
    pattern = _version_pattern(target)
    if pattern is None:
        return version + "\n"
    text = read_text(target)
    if pattern.search(text) is None:
        raise OperationError(f"в {target.name} липсва ред за версия")
    return pattern.sub(lambda match: f'{match.group(1)}"{version}"', text, count=1)


def _planned_version_changes(
    ctx: OpContext, params: dict[str, Any]
) -> tuple[tuple[Path, ...], tuple[str, ...], str]:
    """Validate a version request and list the sites that would actually change."""
    version = _require_str(params, "version")
    if not _VERSION_RE.match(version):
        raise OperationError(f"версията {version!r} не е от вида X.Y.Z")
    sites = _version_sites(ctx, params)
    pending: list[str] = []
    for target in sites:
        if not target.is_file():
            raise OperationError(f"липсва файл за версия: {target.name}")
        if read_text(target) != _next_body(target, version):
            pending.append(_relative(ctx.root, target))
    return sites, tuple(pending), version


def bump_version(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Write one version string to every declared version site at once."""
    sites, pending, version = _planned_version_changes(ctx, params)
    first = _relative(ctx.root, sites[0])
    if not pending:
        return OpResult("bump_version", UNCHANGED, first, f"версията вече е {version}")
    before = sha256_file(resolve_target(ctx.root, pending[0]))
    after = ""
    for index, rel in enumerate(pending):
        target = resolve_target(ctx.root, rel)
        body = _next_body(target, version)
        target.write_text(body, encoding="utf-8", newline="\n")
        if index == 0:
            after = sha256_text(body)
    skipped = len(sites) - len(pending)
    detail = f"версия {version} в {len(pending)} файла"
    if skipped:
        detail += f" ({skipped} без промяна)"
    return OpResult(
        "bump_version",
        WRITE,
        pending[0],
        detail,
        sha256_before=before,
        sha256_after=after,
        extra_paths=tuple(pending[1:]),
    )


# --------------------------------------------------------------------------
# bundled assets
# --------------------------------------------------------------------------
def _asset_source(params: dict[str, Any]) -> Path:
    """Return the bundled asset a ``copy_asset`` change refers to."""
    name = _require_str(params, "asset")
    if PurePosixPath(name.replace("\\", "/")).name != name:
        raise OperationError(f"{name!r} не е име на вграден актив (очаква се само име)")
    return UI_ASSETS_DIR / name


def copy_asset(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Copy a bundled package asset (GUI static files) into the repository."""
    target = resolve_target(ctx.root, params.get("path"))
    rel = _relative(ctx.root, target)
    source = _asset_source(params)
    if not source.is_file():
        return OpResult("copy_asset", FAILED, rel, f"вграденият актив липсва: {source.name}")
    body = _normalise_body(read_text(source))
    before = sha256_file(target) if target.exists() else None
    if before == sha256_text(body):
        return OpResult("copy_asset", UNCHANGED, rel, "файлът е идентичен")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="\n")
    return OpResult(
        "copy_asset",
        WRITE,
        rel,
        f"копиран от {source.name}",
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


def assert_version(ctx: OpContext, params: dict[str, Any]) -> OpResult:
    """Verify that every version site agrees, optionally with an expected value."""
    expected = params.get("version")
    if expected is not None and (not isinstance(expected, str) or not expected.strip()):
        raise OperationError("assert_version: 'version' трябва да е непразен низ")
    sites = _version_sites(ctx, params)
    found = [(_relative(ctx.root, target), _current_version(target)) for target in sites]
    first = found[0][0]
    wanted = expected.strip() if isinstance(expected, str) else found[0][1]
    if not _VERSION_RE.match(wanted):
        return OpResult("assert_version", FAILED, first, f"невалидна версия {wanted!r}")
    mismatched = [f"{rel}={value}" for rel, value in found if value != wanted]
    if not mismatched:
        return OpResult("assert_version", UNCHANGED, first, f"версията е {wanted} навсякъде")
    return OpResult(
        "assert_version",
        FAILED,
        first,
        f"очаква се {wanted}, а е: {', '.join(mismatched)}",
        extra_paths=tuple(rel for rel, _value in found[1:]),
    )


OPERATIONS: dict[str, Callable[[OpContext, dict[str, Any]], OpResult]] = {
    "write_file_if_absent": write_file_if_absent,
    "write_file": write_file,
    "append_block_if_absent": append_block_if_absent,
    "replace_once": replace_once,
    "emit_selection_manifest": emit_selection_manifest,
    "bump_version": bump_version,
    "copy_asset": copy_asset,
    "assert_file_exists": assert_file_exists,
    "assert_file_contains": assert_file_contains,
    "assert_block_present": assert_block_present,
    "assert_version": assert_version,
}

KNOWN_OPS = frozenset(OPERATIONS)


def describe(change: Change) -> str:
    """Return a one-line human description of a change (used by dry runs)."""
    if change.op in {"bump_version", "assert_version"}:
        return f"{change.op} → версия {change.params.get('version', '?')}"
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
    if change.op == "bump_version":
        sites, pending, version = _planned_version_changes(ctx, change.params)
        first = _relative(ctx.root, sites[0])
        if not pending:
            return OpResult("bump_version", UNCHANGED, first, f"версията вече е {version}")
        return OpResult(
            "bump_version",
            PLANNED,
            pending[0],
            f"версия {version} ще се запише в {len(pending)} файла: {', '.join(pending)}",
            extra_paths=tuple(pending[1:]),
        )
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
    if change.op == "copy_asset":
        source = _asset_source(change.params)
        if not source.is_file():
            return OpResult(change.op, FAILED, rel, f"вграденият актив липсва: {source.name}")
        body = _normalise_body(read_text(source))
        if exists and sha256_file(target) == sha256_text(body):
            return OpResult(change.op, UNCHANGED, rel, "файлът е идентичен")
        return OpResult(change.op, PLANNED, rel, "активът ще бъде копиран")
    return OpResult(change.op, PLANNED, rel, "промяната ще бъде приложена")
