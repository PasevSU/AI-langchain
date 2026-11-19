"""Thin, explicit wrapper around the ``git`` CLI.

Only the operations the installer needs exist here, each returning the raw
:class:`~pasevsu_installer.util.CompletedCommand` so every git decision is
auditable. Line endings are pinned to LF and interactive prompts are disabled,
because the installer runs unattended on Windows checkouts with
``core.autocrlf=true``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import OperationError
from .util import CompletedCommand, read_text, run_command

LF_ENV = {
    "GIT_CONFIG_PARAMETERS": (
        "'core.autocrlf=false' 'core.eol=lf' 'core.safecrlf=false' "
        "'credential.interactive=false'"
    ),
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "GIT_EDITOR": ":",
    "GIT_ASKPASS": "",
}


def git(root: Path, *args: str, timeout: int = 300) -> CompletedCommand:
    """Run ``git <args>`` inside ``root`` with LF and non-interactive flags."""
    return run_command(
        ["git", "-C", str(root), *args], cwd=root, timeout=timeout, env=LF_ENV
    )


def is_repository(root: Path) -> bool:
    """Whether ``root`` is the top level of a git work tree."""
    result = git(root, "rev-parse", "--show-toplevel")
    if not result.ok:
        return False
    toplevel = result.stdout.strip().replace("\\", "/").lower()
    expected = str(root.resolve()).replace("\\", "/").lower()
    return toplevel == expected


def revision(root: Path) -> dict[str, Any]:
    """Return commit SHA, branch name and whether the tree is dirty."""
    head = git(root, "rev-parse", "HEAD")
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    status = git(root, "status", "--porcelain")
    return {
        "commit": head.stdout.strip() if head.ok else None,
        "branch": branch.stdout.strip() if branch.ok else None,
        "dirty": bool(status.stdout.strip()) if status.ok else None,
    }


def tree_digest(root: Path) -> dict[str, Any]:
    """Return a deterministic digest of every tracked blob in the work tree."""
    listing = git(root, "ls-files", "-s", "-z")
    if not listing.ok:
        raise OperationError(f"git ls-files се провали: {listing.tail()}")
    entries: list[str] = []
    files = 0
    for chunk in listing.stdout.split("\0"):
        if not chunk.strip():
            continue
        meta, _, path = chunk.partition("\t")
        parts = meta.split()
        if len(parts) < 3:  # noqa: PLR2004 - "<mode> <sha> <stage>"
            continue
        entries.append(f"{path}\0{parts[1]}")
        files += 1
    from .util import sha256_text

    return {"sha256": sha256_text("\n".join(sorted(entries))), "files": files}


def changed_paths(root: Path) -> list[str]:
    """List modified, added, deleted and untracked paths."""
    result = git(root, "status", "--porcelain", "-z", "--untracked-files=all")
    if not result.ok:
        return []
    chunks = result.stdout.split("\0")
    paths: list[str] = []
    skip_next = False
    for chunk in chunks:
        if skip_next:
            skip_next = False
            continue
        # Each entry is ``XY<space>path``; the path may itself start with a
        # space, so the status prefix is removed by offset, never by stripping.
        if len(chunk) <= 3:  # noqa: PLR2004
            continue
        status = chunk[:2]
        path = chunk[3:]
        if "R" in status or "C" in status:
            # Renames and copies carry the original path as the next field.
            skip_next = True
        if path and path not in paths:
            paths.append(path)
    return sorted(paths)


def create_branch(root: Path, name: str, *, force: bool = False) -> CompletedCommand:
    """Create and check out ``name`` from the current commit."""
    args = ["switch", "-c", name] if not force else ["switch", "-C", name]
    return git(root, *args)


def checkout(root: Path, ref: str) -> CompletedCommand:
    """Check out an existing ref."""
    return git(root, "switch", ref)


def branch_exists(root: Path, name: str) -> bool:
    """Whether a local branch called ``name`` exists."""
    result = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
    return result.ok


def stage(root: Path, paths: list[str]) -> CompletedCommand:
    """Stage the given paths (never ``-A``: only what the installer touched)."""
    if not paths:
        raise OperationError("няма пътища за добавяне в индекса")
    return git(root, "add", "--", *paths)


def commit(root: Path, message: str, paths: list[str]) -> CompletedCommand:
    """Stage ``paths`` and create a commit with ``message``."""
    staged = stage(root, paths)
    if not staged.ok:
        return staged
    return git(root, "commit", "-m", message)


def tag(root: Path, name: str, message: str) -> CompletedCommand:
    """Create an annotated tag."""
    return git(root, "tag", "-a", name, "-m", message)


def push(root: Path, remote: str, refspec: str) -> CompletedCommand:
    """Push a refspec to ``remote`` (used only when publishing is enabled)."""
    return git(root, "push", "--porcelain", remote, refspec)


def describe(root: Path, ref: str = "HEAD") -> str:
    """Return ``git describe`` output, or an empty string on failure."""
    result = git(root, "describe", "--tags", "--always", "--dirty", ref)
    return result.stdout.strip() if result.ok else ""


def commit_message_scope(message: str) -> str | None:
    """Extract the Conventional Commit scope, if any."""
    header = message.strip().splitlines()[0] if message.strip() else ""
    if "(" not in header or ")" not in header:
        return None
    return header.split("(", 1)[1].split(")", 1)[0].strip() or None


def read_file_at(root: Path, ref: str, path: str) -> str:
    """Read a file's content at ``ref`` (``git show``)."""
    result = git(root, "show", f"{ref}:{path}")
    return result.stdout if result.ok else ""


def tracked_files(root: Path) -> list[str]:
    """Return every tracked path, sorted."""
    result = git(root, "ls-files")
    if not result.ok:
        return []
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def current_file(root: Path, path: str) -> str:
    """Read a working-tree file as UTF-8 text."""
    return read_text(root / path)
