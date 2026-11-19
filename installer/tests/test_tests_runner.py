"""Tests for the suite gate: only *required* suites may block a run."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from pasevsu_installer.config import Config, SuiteSpec
from pasevsu_installer.tests_runner import (
    FAILED,
    PASSED,
    SKIPPED,
    SuiteOutcome,
    TestReport,
    advisories,
    blockers,
    required_unverified,
    run_suites,
    summarise,
)


def _outcome(suite_id: str, status: str, *, required: bool = True) -> SuiteOutcome:
    return SuiteOutcome(id=suite_id, status=status, required=required)


class SuiteOutcomeGateTest(unittest.TestCase):
    """The ``required`` flag is the only thing that decides blocking."""

    def test_required_failure_blocks(self) -> None:
        outcome = _outcome("a", FAILED, required=True)
        self.assertTrue(outcome.blocking)
        self.assertFalse(outcome.advisory)
        self.assertFalse(outcome.ok)

    def test_optional_failure_is_advisory_only(self) -> None:
        outcome = _outcome("a", FAILED, required=False)
        self.assertFalse(outcome.blocking)
        self.assertTrue(outcome.advisory)
        self.assertFalse(outcome.ok)

    def test_required_skip_blocks(self) -> None:
        outcome = _outcome("a", SKIPPED, required=True)
        self.assertTrue(outcome.blocking)
        self.assertTrue(outcome.unverified)

    def test_optional_skip_does_not_block(self) -> None:
        outcome = _outcome("a", SKIPPED, required=False)
        self.assertFalse(outcome.blocking)
        self.assertFalse(outcome.advisory)
        self.assertTrue(outcome.ok)

    def test_passing_suite_is_never_blocking(self) -> None:
        for required in (True, False):
            outcome = _outcome("a", PASSED, required=required)
            self.assertTrue(outcome.ok)
            self.assertFalse(outcome.blocking)
            self.assertFalse(outcome.advisory)


class TestReportGateTest(unittest.TestCase):
    """Aggregate behaviour of :class:`TestReport`."""

    def test_optional_failure_keeps_report_green(self) -> None:
        report = TestReport(
            suites=(
                _outcome("required-ok", PASSED),
                _outcome("optional-broken", FAILED, required=False),
            )
        )
        self.assertTrue(report.green)
        self.assertEqual(blockers(report), ())
        self.assertEqual(advisories(report), ("optional-broken",))
        self.assertFalse(report.ok)

    def test_required_failure_turns_report_red(self) -> None:
        report = TestReport(
            suites=(
                _outcome("required-broken", FAILED),
                _outcome("optional-ok", PASSED, required=False),
            )
        )
        self.assertFalse(report.green)
        self.assertEqual(blockers(report), ("required-broken",))
        self.assertEqual(advisories(report), ())

    def test_empty_report_is_green(self) -> None:
        self.assertTrue(TestReport().green)
        self.assertEqual(blockers(TestReport()), ())

    def test_summary_marks_optional_failure_as_non_blocking(self) -> None:
        report = TestReport(
            suites=(
                _outcome("compile", PASSED),
                _outcome("unit", FAILED, required=False),
            )
        )
        text = summarise(report)
        self.assertIn("1/2", text)
        self.assertIn("unit", text)
        self.assertIn("не блокират", text)

    def test_required_unverified_only_lists_required_skips(self) -> None:
        report = TestReport(
            suites=(
                _outcome("a", SKIPPED, required=True),
                _outcome("b", SKIPPED, required=False),
            )
        )
        self.assertEqual(required_unverified(report), ("a",))
        self.assertEqual(blockers(report), ("a",))


class RunSuitesGateTest(unittest.TestCase):
    """End-to-end gate behaviour with real subprocesses."""

    def _config(self, root: Path, *suites: SuiteSpec) -> Config:
        return Config(root=root, path=root / "installer.config.json", suites=suites)

    def test_optional_failing_suite_does_not_block_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(
                root,
                SuiteSpec(id="ok", command=(sys.executable, "-c", "pass"), required=True),
                SuiteSpec(
                    id="optional-broken",
                    command=(sys.executable, "-c", "raise SystemExit(3)"),
                    required=False,
                ),
            )
            report = run_suites(config)
        self.assertEqual([item.status for item in report.suites], [PASSED, FAILED])
        self.assertTrue(report.green)
        self.assertEqual(blockers(report), ())
        self.assertEqual(advisories(report), ("optional-broken",))

    def test_required_failing_suite_blocks_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(
                root,
                SuiteSpec(id="broken", command=(sys.executable, "-c", "raise SystemExit(5)")),
            )
            report = run_suites(config)
        self.assertFalse(report.green)
        self.assertEqual(blockers(report), ("broken",))

    def test_missing_tool_skips_required_and_optional_differently(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(
                root,
                SuiteSpec(id="req", command=("definitely-absent-tool",), requires=("definitely-absent-tool",)),
                SuiteSpec(
                    id="opt",
                    command=("definitely-absent-tool",),
                    requires=("definitely-absent-tool",),
                    required=False,
                ),
            )
            report = run_suites(config)
        self.assertEqual({item.status for item in report.suites}, {SKIPPED})
        self.assertEqual(blockers(report), ("req",))
        self.assertFalse(report.green)

    def test_missing_working_directory_is_skipped_not_blocking_when_optional(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            config = self._config(
                root,
                SuiteSpec(
                    id="optional-gone",
                    command=(sys.executable, "-c", "pass"),
                    cwd="does-not-exist",
                    required=False,
                ),
            )
            report = run_suites(config)
        self.assertEqual(report.suites[0].status, SKIPPED)
        self.assertTrue(report.green)


if __name__ == "__main__":
    unittest.main()
