"""Publisher: turns a verified migration into a release.

Publishing is a git operation with three gates: a Conventional Commit whose
scope is mandatory, a dedicated branch (``master``/``main`` is refused unless the
configuration explicitly allows it), and an annotated tag carrying the installer
version. Pushing is opt-in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import gitops
from .config import Config
from .errors import PublishError
from .journal import Journal
from .util import atomic_write_text, dump_json, utc_now

PUBLISHED = "published"
SKIPPED = "skipped"
FAILED = "failed"

PROTECTED_BRANCHES = frozenset({"master", "main"})
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class Publication:
    """Outcome of the publish step."""

    status: str
    reason: str = ""
    branch: str | None = None
    commit: str | None = None
    tag: str | None = None
    pushed: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        """Whether the step did not fail."""
        return self.status != FAILED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "status": self.status,
            "reason": self.reason,
            "branch": self.branch,
            "commit": self.commit,
            "tag": self.tag,
            "pushed": list(self.pushed),
            "files": list(self.files),
            "message": self.message,
        }


def commit_message(config: Config, *, version: str, summary: str, body: str = "") -> str:
    """Build a Conventional Commit message with a mandatory scope."""
    headline = summary.strip().splitlines()[0] if summary.strip() else "миграция"
    trailing = "" if f"({config.commit_scope})" in headline else f" ({version})"
    message = f"feat({config.commit_scope}): {headline}{trailing}"
    sections = [message, "", body.strip()] if body.strip() else [message]
    sections.extend(["", f"Installer-Version: {version}", f"Installer-Run: {utc_now()}"])
    return "\n".join(sections).rstrip("\n") + "\n"


def publish(
    config: Config,
    *,
    version: str,
    paths: tuple[str, ...] | list[str],
    summary: str,
    body: str = "",
    journal: Journal | None = None,
    dry_run: bool = False,
) -> Publication:
    """Commit the changed paths, tag the release and push when enabled."""
    root = config.root
    if not config.publish_enabled:
        return Publication(SKIPPED, reason="публикуването е изключено в конфигурацията")
    if not gitops.is_repository(root):
        raise PublishError(f"няма git хранилище в {root}")
    if not _VERSION_RE.match(version):
        raise PublishError(f"невалидна версия за публикуване: {version}")

    files = _normalise_paths(root, paths)
    if not files:
        return Publication(SKIPPED, reason="няма променени файлове за публикуване")

    revision = gitops.revision(root)
    current = revision.get("branch") or ""
    target_branch = current
    if current in PROTECTED_BRANCHES or not current:
        if config.allow_master:
            target_branch = current or "master"
        else:
            target_branch = f"{config.commit_scope}/{version}"
            if not dry_run:
                _switch_branch(root, target_branch)

    message = commit_message(config, version=version, summary=summary, body=body)
    tag_name = f"{config.tag_prefix}{version}"

    if dry_run:
        return Publication(
            SKIPPED,
            reason="пробен пуск (dry-run) — нищо не е ангажирано",
            branch=target_branch,
            tag=tag_name,
            files=tuple(files),
            message=message,
        )

    if not _commit_allowed(message):
        raise PublishError("съобщението за commit не съдържа задължителен scope")

    staged = gitops.stage(root, files)
    if not staged.ok:
        raise PublishError(f"git add се провали: {_error_tail(staged)}")

    reason = ""
    if gitops.staged_paths(root):
        created = gitops.commit(root, message, files)
        if not created.ok:
            raise PublishError(f"git commit се провали: {_error_tail(created)}")
    else:
        reason = "нищо ново за ангажиране — публикуван е текущият HEAD"
    commit_sha = _head(root)

    if not gitops.tag_exists(root, tag_name):
        tagged = gitops.tag(root, tag_name, f"{config.name} {version}")
        if not tagged.ok:
            raise PublishError(f"git tag се провали: {_error_tail(tagged)}")

    pushed: list[str] = []
    if config.push:
        for refspec in (target_branch, tag_name):
            sent = _push_with_retry(root, refspec)
            if not sent.ok:
                raise PublishError(f"git push {refspec} се провали: {_error_tail(sent)}")
            pushed.append(refspec)

    publication = Publication(
        PUBLISHED,
        reason=reason,
        branch=target_branch,
        commit=commit_sha,
        tag=tag_name,
        pushed=tuple(pushed),
        files=tuple(files),
        message=message,
    )
    if journal is not None:
        journal.append("publish", **publication.to_dict())
    return publication


def write_release_notes(config: Config, *, version: str, body: str) -> Path:
    """Write ``installer/RELEASE-NOTES/<version>.md`` and return its path."""
    directory = config.release_notes_path
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{version}.md"
    header = f"# {config.name} {version}\n\n"
    atomic_write_text(target, header + body.strip() + "\n")
    return target


def write_publication_record(config: Config, publication: Publication) -> Path:
    """Persist the publication payload inside the installer state directory."""
    target = config.root / config.state_dir / "publish.json"
    atomic_write_text(target, dump_json(publication.to_dict()))
    return target


def _clean_path(raw: str) -> str:
    """Normalise one repository-relative path without eating a leading dot."""
    text = str(raw).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text


def _is_state_path(item: str) -> bool:
    """Return ``True`` when the path belongs to the installer state directory."""
    return item.split("/", 1)[0].rstrip("/") == ".installer"


def _normalise_paths(root: Path, paths: tuple[str, ...] | list[str]) -> list[str]:
    seen: list[str] = []
    for raw in paths:
        cleaned = _clean_path(raw)
        if not cleaned or cleaned in seen:
            continue
        seen.append(cleaned)
    inside = [item for item in seen if not item.startswith("..")]
    if len(inside) != len(seen):
        raise PublishError("опит за публикуване на път извън репото")
    if not inside:
        inside = gitops.changed_paths(root)
    return [item for item in inside if not _is_state_path(item)]


PUSH_ATTEMPTS = 3


def _push_with_retry(root: Path, refspec: str) -> Any:
    """Push ``refspec`` and retry the transient failures that DNS/network raise."""
    last: Any = None
    for _ in range(PUSH_ATTEMPTS):
        last = gitops.push(root, "origin", refspec)
        if last.ok:
            return last
        if not _transient(last):
            return last
    return last


def _transient(result: Any) -> bool:
    """Whether a push failure looks like a temporary network problem."""
    text = f"{result.stdout}\n{result.stderr}".lower()
    markers = (
        "getaddrinfo",
        "could not resolve host",
        "connection reset",
        "connection timed out",
        "temporary failure in name resolution",
        "remote end hung up",
        "rpc failed",
    )
    return any(marker in text for marker in markers)


def _switch_branch(root: Path, branch: str) -> None:
    current = gitops.revision(root).get("branch") or ""
    if current == branch:
        return
    if gitops.branch_exists(root, branch):
        switched = gitops.checkout(root, branch)
    else:
        switched = gitops.create_branch(root, branch)
    if not switched.ok:
        raise PublishError(f"неуспешно преминаване към клон {branch}: {_error_tail(switched)}")


def _commit_allowed(message: str) -> bool:
    return gitops.commit_message_scope(message) is not None


def _head(root: Path) -> str:
    result = gitops.git(root, "rev-parse", "HEAD")
    return result.stdout.strip() if result.ok else ""


def _error_tail(result: Any) -> str:
    text = (result.stderr or result.stdout or "").strip().splitlines()
    return text[-1] if text else f"exit {result.exit_code}"
