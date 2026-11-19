"""Shared low-level helpers: time, hashing, atomic writes, subprocess calls.

The module is deliberately dependency-free (standard library only) so the
installer agent can run inside a bare checkout with no ``uv`` environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any

DEFAULT_TIMEOUT_S = 900


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 (``Z`` suffix) form."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    """Return a filesystem- and tag-safe UTC timestamp."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def sha256_text(text: str) -> str:
    """Return the SHA-256 hex digest of ``text`` encoded as UTF-8."""
    return sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_json(data: Any) -> str:
    """Serialise ``data`` as pretty, newline-terminated JSON."""
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically using an LF-normalised temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed via context manager
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with handle:
            handle.write(text)
        Path(handle.name).replace(path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def read_text(path: Path) -> str:
    """Read a UTF-8 text file with universal newlines disabled."""
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class CompletedCommand:
    """Result of a finished subprocess call."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def ok(self) -> bool:
        """Whether the command exited with status ``0``."""
        return self.exit_code == 0

    def tail(self, lines: int = 30) -> str:
        """Return the last ``lines`` of merged stdout/stderr."""
        merged = "\n".join(part for part in (self.stdout, self.stderr) if part.strip())
        return "\n".join(merged.splitlines()[-lines:])

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary (without the full stdout)."""
        return {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 1),
        }


def run_command(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> CompletedCommand:
    """Run ``argv`` in ``cwd`` and capture its output.

    ``env`` is **merged** into the inherited environment instead of replacing
    it. Dropping ``SystemRoot``/``windir`` on Windows makes the CRT unable to
    create its resolver helper thread, so ``git push`` dies with
    ``getaddrinfo() thread failed to start`` even though the network is fine.

    A timeout is reported as exit code ``124`` instead of raising so callers can
    treat it as an ordinary failed gate.
    """
    started = monotonic()
    child_env = {**os.environ, **env} if env else None
    try:
        completed = subprocess.run(  # noqa: S603 - argv is always an explicit list
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=child_env,
        )
        return CompletedCommand(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_ms=(monotonic() - started) * 1000,
        )
    except subprocess.TimeoutExpired as exc:
        return CompletedCommand(
            argv=tuple(argv),
            exit_code=124,
            stdout=(exc.stdout or b"").decode("utf-8", "replace")
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or ""),
            stderr=f"timeout after {timeout}s",
            duration_ms=(monotonic() - started) * 1000,
        )
    except FileNotFoundError as exc:
        return CompletedCommand(
            argv=tuple(argv),
            exit_code=127,
            stdout="",
            stderr=str(exc),
            duration_ms=(monotonic() - started) * 1000,
        )
