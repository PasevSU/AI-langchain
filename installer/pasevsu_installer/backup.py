"""Backup release: the mandatory gate that runs before any mutation.

Nothing is written to the repository until a backup has been created **and**
verified: an annotated git tag on the current commit plus a ZIP archive of the
work tree, both recorded with a tree digest so the state can be restored or
compared later.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gitops
from .errors import BackupError
from .util import atomic_write_text, dump_json, sha256_file, sha256_text, utc_now, utc_stamp

IGNORED_DIRS = frozenset({".git", ".installer", "__pycache__", ".pytest_cache", ".ruff_cache"})


@dataclass(frozen=True)
class Backup:
    """A restorable snapshot of the repository."""

    id: str
    created_at: str
    kind: str
    tree_sha256: str
    files: int
    archive: str
    archive_sha256: str
    manifest: str
    ref: str | None = None
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    version: str = ""

    @property
    def ok(self) -> bool:
        """Whether the snapshot is complete enough to allow mutations."""
        return bool(self.tree_sha256) and bool(self.archive_sha256)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "kind": self.kind,
            "ref": self.ref,
            "commit": self.commit,
            "branch": self.branch,
            "dirty": self.dirty,
            "version": self.version,
            "tree_sha256": self.tree_sha256,
            "files": self.files,
            "archive": self.archive,
            "archive_sha256": self.archive_sha256,
            "manifest": self.manifest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Backup:
        """Rebuild a backup record from persisted state."""
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            kind=str(data.get("kind", "archive")),
            tree_sha256=str(data.get("tree_sha256", "")),
            files=int(data.get("files", 0)),
            archive=str(data.get("archive", "")),
            archive_sha256=str(data.get("archive_sha256", "")),
            manifest=str(data.get("manifest", "")),
            ref=data.get("ref"),
            commit=data.get("commit"),
            branch=data.get("branch"),
            dirty=data.get("dirty"),
            version=str(data.get("version", "")),
        )


def create_backup(
    root: Path,
    backup_dir: Path,
    *,
    tag_prefix: str = "installer-backup",
    version: str = "0.0.0",
    stamp: str | None = None,
) -> Backup:
    """Create and verify a backup release of ``root``."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_id = stamp or utc_stamp()
    created_at = utc_now()

    if gitops.is_repository(root):
        rev = gitops.revision(root)
        digest = gitops.tree_digest(root)
        ref = _unique_tag(root, f"{tag_prefix}-{backup_id}")
        tagged = gitops.tag(
            root,
            ref,
            f"Installer backup {backup_id} (v{version}) "
            f"branch={rev.get('branch')} commit={rev.get('commit')} "
            f"tree={digest['sha256'][:16]}",
        )
        if not tagged.ok:
            raise BackupError(f"git tag се провали: {tagged.tail()}")
        kind = "git-tag"
        files = [str(path) for path in gitops.tracked_files(root)]
        files.extend(_untracked(root))
        commit = rev.get("commit")
        branch = rev.get("branch")
        dirty = rev.get("dirty")
        tree_sha = str(digest["sha256"])
        file_count = int(digest["files"])
    else:
        ref = None
        commit = None
        branch = None
        dirty = None
        files = [str(path) for path in _walk(root)]
        tree_sha = _walk_digest(root)
        file_count = len(files)
        kind = "archive"

    archive_path = backup_dir / f"{backup_id}.zip"
    _archive(root, archive_path, files)
    if not archive_path.exists() or archive_path.stat().st_size == 0:
        raise BackupError(f"архивът не е създаден: {archive_path}")

    manifest_path = backup_dir / f"{backup_id}.json"
    record = Backup(
        id=backup_id,
        created_at=created_at,
        kind=kind,
        tree_sha256=tree_sha,
        files=file_count,
        archive=str(archive_path),
        archive_sha256=sha256_file(archive_path),
        manifest=str(manifest_path),
        ref=ref,
        commit=commit,
        branch=branch,
        dirty=dirty,
        version=version,
    )
    atomic_write_text(manifest_path, dump_json(record.to_dict()))
    verify_backup(record)
    return record


def verify_backup(backup: Backup) -> None:
    """Raise :class:`BackupError` unless the snapshot is intact on disk."""
    if not backup.ok:
        raise BackupError(f"непълна резервна копия: {backup.id}")
    archive = Path(backup.archive)
    if not archive.exists():
        raise BackupError(f"липсва архив: {archive}")
    actual = sha256_file(archive)
    if actual != backup.archive_sha256:
        raise BackupError(
            f"контролната сума на архива не съвпада за {backup.id} "
            f"(очаквано {backup.archive_sha256[:12]}, налично {actual[:12]})"
        )
    if not Path(backup.manifest).exists():
        raise BackupError(f"липсва манифест: {backup.manifest}")


def _unique_tag(root: Path, name: str) -> str:
    candidate = name
    for attempt in range(2, 100):
        exists = gitops.git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{candidate}")
        if not exists.ok:
            return candidate
        candidate = f"{name}-{attempt}"
    raise BackupError(f"не може да се намери свободно име за таг близо до {name}")


def _untracked(root: Path) -> list[str]:
    result = gitops.git(root, "ls-files", "--others", "--exclude-standard")
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _walk(root: Path) -> list[Path]:
    collected: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts[:-1])
        if parts & IGNORED_DIRS:
            continue
        collected.append(path.relative_to(root))
    return collected


def _walk_digest(root: Path) -> str:
    lines = [
        f"{relative.as_posix()}\0{sha256_file(root / relative)}" for relative in _walk(root)
    ]
    return sha256_text("\n".join(lines))


def _archive(root: Path, target: Path, files: list[str]) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in files:
            source = root / relative
            if not source.is_file():
                continue
            archive.write(source, arcname=relative)
