"""Test runner: executes every configured suite and reports honestly.

A suite whose declared external tools are missing is never reported as passing.
It becomes a *measured skip* carrying the reason, so the report can distinguish
"all tests green" from "some tests could not run here".
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config, SuiteSpec
from .journal import Journal
from .util import run_command

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
TAIL_LINES = 40


@dataclass(frozen=True)
class SuiteOutcome:
    """Result of one configured test suite."""

    id: str
    status: str
    command: tuple[str, ...] = ()
    cwd: str = "."
    required: bool = True
    exit_code: int | None = None
    duration_ms: float = 0.0
    missing: tuple[str, ...] = ()
    skip_reason: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        """Whether the suite passed, or was allowed to be skipped."""
        return self.status == PASSED or (self.status == SKIPPED and not self.required)

    @property
    def unverified(self) -> bool:
        """Whether a required suite could not be executed."""
        return self.status == SKIPPED and self.required

    @property
    def blocking(self) -> bool:
        """Whether this suite prevents the run from being considered green.

        A suite declared ``required: false`` is advisory: it still runs and its
        result is reported, but a failure or a skip never blocks publishing.
        """
        return self.required and self.status != PASSED

    @property
    def advisory(self) -> bool:
        """Whether an optional suite ran and failed without blocking the run."""
        return not self.required and self.status == FAILED

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "id": self.id,
            "status": self.status,
            "command": list(self.command),
            "cwd": self.cwd,
            "required": self.required,
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 1),
            "missing": list(self.missing),
            "skip_reason": self.skip_reason,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
        }


@dataclass(frozen=True)
class TestReport:
    """Aggregate result of the configured test suites."""

    suites: tuple[SuiteOutcome, ...] = ()
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether every suite either passed or was an allowed skip."""
        return all(outcome.ok for outcome in self.suites)

    @property
    def green(self) -> bool:
        """Whether no required suite failed or could not run."""
        return all(not outcome.blocking for outcome in self.suites)

    @property
    def blocking(self) -> tuple[SuiteOutcome, ...]:
        """The suites that block the run (required, but not passing)."""
        return tuple(item for item in self.suites if item.blocking)

    @property
    def advisory(self) -> tuple[SuiteOutcome, ...]:
        """The optional suites that ran and failed."""
        return tuple(item for item in self.suites if item.advisory)

    @property
    def complete(self) -> bool:
        """Whether every suite actually ran (no skips at all)."""
        return all(outcome.status != SKIPPED for outcome in self.suites)

    @property
    def failed(self) -> tuple[SuiteOutcome, ...]:
        """The suites that ran and failed."""
        return tuple(item for item in self.suites if item.status == FAILED)

    @property
    def skipped(self) -> tuple[SuiteOutcome, ...]:
        """The suites that could not run."""
        return tuple(item for item in self.suites if item.status == SKIPPED)

    @property
    def unverified(self) -> tuple[SuiteOutcome, ...]:
        """The required suites that could not run."""
        return tuple(item for item in self.suites if item.unverified)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation."""
        return {
            "ok": self.ok,
            "complete": self.complete,
            "duration_ms": round(self.duration_ms, 1),
            "suites": [item.to_dict() for item in self.suites],
        }


def run_suites(
    config: Config, *, root: Path | None = None, journal: Journal | None = None
) -> TestReport:
    """Run every configured suite in declaration order."""
    base = root if root is not None else config.root
    outcomes: list[SuiteOutcome] = []
    started = time.monotonic()

    if not config.suites:
        return TestReport(suites=(), duration_ms=0.0)

    for suite in config.suites:
        outcome = _run_suite(suite, base)
        outcomes.append(outcome)
        if journal is not None:
            journal.append(
                "suite",
                suite=outcome.id,
                status=outcome.status,
                exit_code=outcome.exit_code,
                duration_ms=round(outcome.duration_ms, 1),
                missing=list(outcome.missing),
                skip_reason=outcome.skip_reason,
            )

    return TestReport(
        suites=tuple(outcomes),
        duration_ms=(time.monotonic() - started) * 1000.0,
    )


def _run_suite(suite: SuiteSpec, root: Path) -> SuiteOutcome:
    missing = tuple(tool for tool in suite.requires if shutil.which(tool) is None)
    if missing:
        reason = suite.skip_reason or f"липсва инструмент: {', '.join(missing)}"
        return SuiteOutcome(
            id=suite.id,
            status=SKIPPED,
            command=suite.command,
            cwd=suite.cwd,
            required=suite.required,
            missing=missing,
            skip_reason=reason,
        )

    cwd = (root / suite.cwd).resolve()
    if not cwd.is_dir():
        return SuiteOutcome(
            id=suite.id,
            status=SKIPPED,
            command=suite.command,
            cwd=suite.cwd,
            required=suite.required,
            skip_reason=f"работната папка липсва: {suite.cwd}",
        )

    command = list(suite.command)
    result = run_command(command, cwd=cwd, timeout=suite.timeout_s)
    status = PASSED if result.ok else FAILED
    return SuiteOutcome(
        id=suite.id,
        status=status,
        command=tuple(command),
        cwd=suite.cwd,
        required=suite.required,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        stdout_tail=_tail(result.stdout),
        stderr_tail=_tail(result.stderr),
    )


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    if not text:
        return ""
    trimmed = text.strip().splitlines()
    return "\n".join(trimmed[-lines:])


def summarise(report: TestReport) -> str:
    """Return a one-line Bulgarian summary of the test report."""
    total = len(report.suites)
    if total == 0:
        return "не са конфигурирани тестови пакети"
    passed = sum(1 for item in report.suites if item.status == PASSED)
    failed = len(report.failed)
    skipped = len(report.skipped)
    text = f"{passed}/{total} пакета минаха"
    if failed:
        names = ", ".join(item.id for item in report.failed)
        text += f", {failed} се провалиха ({names})"
    if skipped:
        names = ", ".join(item.id for item in report.skipped)
        text += f", {skipped} пропуснати ({names})"
    advisory = len(report.advisory)
    if advisory:
        names = ", ".join(item.id for item in report.advisory)
        text += f"; {advisory} незадължителни без успех ({names}) — не блокират"
    return text


def blockers(report: TestReport) -> tuple[str, ...]:
    """Return the ids of the suites that prevent publishing."""
    return tuple(item.id for item in report.blocking)


def advisories(report: TestReport) -> tuple[str, ...]:
    """Return the ids of the optional suites that ran and failed."""
    return tuple(item.id for item in report.advisory)


def required_unverified(report: TestReport) -> tuple[str, ...]:
    """Return the ids of required suites that could not run."""
    return tuple(item.id for item in report.unverified)
