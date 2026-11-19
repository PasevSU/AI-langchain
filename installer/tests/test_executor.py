"""Tests for the executor: applying tasks, stopping on failure, dry runs."""

from __future__ import annotations

import unittest

from pasevsu_installer.executor import (
    BROKEN,
    PARTIAL,
    PLANNED,
    SUCCESS,
    ExecutionLedger,
    ExecutionResult,
    TaskOutcome,
    execute,
    merge_state_tasks,
    summarise,
)
from pasevsu_installer.journal import Journal
from pasevsu_installer.operations import FAILED, UNCHANGED, WRITE, OpResult
from pasevsu_installer.selection import Selection

from .support import change, task, temp_root


def outcome(
    task_id: str,
    status: str = SUCCESS,
    *,
    changes: tuple[OpResult, ...] = (),
    verify: tuple[OpResult, ...] = (),
) -> TaskOutcome:
    """Build a :class:`TaskOutcome` without running the executor."""
    return TaskOutcome(
        id=task_id,
        agent="test-agent",
        title=task_id,
        status=status,
        changes=changes,
        verify=verify,
        fingerprint=f"fp-{task_id}",
    )


class ExecuteTest(unittest.TestCase):
    """A run applies the selection in order and stops at the first failure."""

    def test_applies_change_and_reports_success(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(
                    task(
                        "t1",
                        changes=(
                            change("write_file_if_absent", path="docs/NOTES.md", content="здравей\n"),
                        ),
                    ),
                ),
                fingerprint="fp-run",
            )
            result = execute(selection, root)
            self.assertTrue(result.ok)
            self.assertIsNone(result.stopped_at)
            self.assertEqual(result.fingerprint, "fp-run")
            self.assertEqual([item.id for item in result.outcomes], ["t1"])
            self.assertEqual(result.outcomes[0].status, SUCCESS)
            self.assertTrue(result.outcomes[0].wrote)
            self.assertEqual(result.changed_paths, ("docs/NOTES.md",))
            self.assertEqual((root / "docs" / "NOTES.md").read_text(encoding="utf-8"), "здравей\n")

    def test_second_run_reports_partial(self) -> None:
        with temp_root() as root:
            payload = change("write_file_if_absent", path="docs/NOTES.md", content="x\n")
            selection = Selection(tasks=(task("t1", changes=(payload,)),))
            execute(selection, root)
            again = execute(selection, root)
            self.assertTrue(again.ok)
            self.assertEqual(again.outcomes[0].status, PARTIAL)
            self.assertFalse(again.outcomes[0].wrote)
            self.assertEqual(again.applied, ())
            self.assertEqual(again.changed_paths, ())

    def test_stops_at_first_failing_task(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(
                    task(
                        "t1",
                        changes=(
                            change("write_file_if_absent", path="a.md", content="a\n"),
                        ),
                    ),
                    task(
                        "t2",
                        changes=(
                            change("replace_once", path="missing.md", search="x", replacement="y"),
                        ),
                    ),
                    task(
                        "t3",
                        changes=(
                            change("write_file_if_absent", path="c.md", content="c\n"),
                        ),
                    ),
                )
            )
            result = execute(selection, root)
            self.assertFalse(result.ok)
            self.assertEqual(result.stopped_at, "t2")
            self.assertEqual([item.id for item in result.outcomes], ["t1", "t2"])
            self.assertEqual([item.id for item in result.failed], ["t2"])
            self.assertEqual(result.outcomes[1].changes[0].status, FAILED)
            self.assertIn("липсва", result.outcomes[1].error)
            self.assertFalse((root / "c.md").exists())

    def test_operation_error_is_captured(self) -> None:
        with temp_root() as root:
            selection = Selection(tasks=(task("t1", changes=(change("nope", path="a.md"),)),))
            result = execute(selection, root)
            self.assertFalse(result.ok)
            self.assertEqual(result.outcomes[0].status, BROKEN)
            self.assertIn("непозната операция", result.outcomes[0].error)
            self.assertEqual(result.outcomes[0].changes[0].status, FAILED)

    def test_verify_failure_marks_task_broken(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(
                    task(
                        "t1",
                        changes=(change("write_file_if_absent", path="a.md", content="a\n"),),
                        verify=(change("assert_file_exists", path="missing.md"),),
                    ),
                )
            )
            result = execute(selection, root)
            self.assertFalse(result.ok)
            recorded = result.outcomes[0]
            self.assertEqual(recorded.status, BROKEN)
            self.assertTrue(recorded.error.startswith("проверка"))
            self.assertEqual(recorded.verify[0].status, FAILED)
            self.assertTrue((root / "a.md").exists())

    def test_dry_run_plans_without_writing(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(task("t1", changes=(change("write_file_if_absent", path="a.md", content="a\n"),)),)
            )
            result = execute(selection, root, dry_run=True)
            self.assertTrue(result.dry_run)
            self.assertTrue(result.ok)
            self.assertEqual(result.outcomes[0].status, PLANNED)
            self.assertFalse(result.outcomes[0].wrote)
            self.assertEqual(result.changed_paths, ())
            self.assertFalse((root / "a.md").exists())

    def test_dry_run_skips_verification(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(
                    task(
                        "t1",
                        changes=(change("write_file_if_absent", path="a.md", content="a\n"),),
                        verify=(change("assert_file_exists", path="missing.md"),),
                    ),
                )
            )
            result = execute(selection, root, dry_run=True)
            self.assertTrue(result.ok)
            self.assertEqual(result.outcomes[0].status, PLANNED)
            self.assertEqual(result.outcomes[0].verify, ())

    def test_journal_records_each_task(self) -> None:
        with temp_root() as root:
            journal = Journal(root / ".installer" / "journal.jsonl")
            selection = Selection(
                tasks=(
                    task("t1", changes=(change("write_file_if_absent", path="a.md", content="a\n"),)),
                    task("t2", changes=(change("write_file_if_absent", path="b.md", content="b\n"),)),
                )
            )
            execute(selection, root, journal=journal)
            events = journal.events()
            self.assertEqual(len(events), 2)
            self.assertEqual([event["event"] for event in events], ["task", "task"])
            self.assertEqual([event["task"] for event in events], ["t1", "t2"])
            self.assertEqual([event["status"] for event in events], [SUCCESS, SUCCESS])
            self.assertFalse(any(event["dry_run"] for event in events))

    def test_empty_selection_is_ok(self) -> None:
        with temp_root() as root:
            result = execute(Selection(), root)
            self.assertTrue(result.ok)
            self.assertEqual(result.outcomes, ())
            self.assertEqual(result.changed_paths, ())


class MergeStateTasksTest(unittest.TestCase):
    """State merging counts runs, keeps history and ignores dry runs."""

    def test_counts_runs_and_appends_history(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(task("t1", changes=(change("write_file_if_absent", path="a.md", content="a\n"),)),)
            )
            result = execute(selection, root)
            first = merge_state_tasks({}, result, dry_run=False)
            entry = first["t1"]
            self.assertEqual(entry["agent"], "test-agent")
            self.assertEqual(entry["status"], SUCCESS)
            self.assertEqual(entry["runs"], 1)
            self.assertEqual(entry["paths"], ["a.md"])
            self.assertEqual(len(entry["history"]), 1)

            second = merge_state_tasks(first, result, dry_run=False)
            self.assertEqual(second["t1"]["runs"], 2)
            self.assertEqual(len(second["t1"]["history"]), 2)

    def test_history_is_capped_at_twenty(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(task("t1", changes=(change("write_file_if_absent", path="a.md", content="a\n"),)),)
            )
            result = execute(selection, root)
            seeded = {"t1": {"runs": 25, "history": [{"status": SUCCESS} for _ in range(20)]}}
            merged = merge_state_tasks(seeded, result, dry_run=False)
            self.assertEqual(merged["t1"]["runs"], 26)
            self.assertEqual(len(merged["t1"]["history"]), 20)
            self.assertEqual(merged["t1"]["history"][-1]["status"], SUCCESS)

    def test_dry_run_leaves_state_untouched(self) -> None:
        with temp_root() as root:
            selection = Selection(
                tasks=(task("t1", changes=(change("write_file_if_absent", path="a.md", content="a\n"),)),)
            )
            result = execute(selection, root, dry_run=True)
            previous = {"t1": {"runs": 4, "history": [{"status": PARTIAL}]}}
            merged = merge_state_tasks(previous, result, dry_run=True)
            self.assertEqual(merged, previous)

    def test_ignores_non_mapping_state(self) -> None:
        result = ExecutionResult(outcomes=(outcome("t1"),), fingerprint="fp")
        merged = merge_state_tasks({"t1": "broken"}, result, dry_run=False)  # type: ignore[dict-item]
        self.assertEqual(merged["t1"]["runs"], 1)


class SummariseTest(unittest.TestCase):
    """The summary line stays honest about what actually changed."""

    def test_success_line(self) -> None:
        result = ExecutionResult(
            outcomes=(
                outcome("t1", changes=(OpResult("write_file", WRITE, "a.md", ""),)),
                outcome("t2", PARTIAL),
            )
        )
        self.assertEqual(summarise(result), "2 задачи изпълнени, 1 с реални промени")

    def test_failure_line_names_the_task(self) -> None:
        broken = outcome("t2", BROKEN, changes=(OpResult("write_file", FAILED, "b.md", "boom"),))
        result = ExecutionResult(outcomes=(outcome("t1", PARTIAL), broken), stopped_at="t2")
        self.assertEqual(summarise(result), "0/2 задачи с промени, спряно на t2")

    def test_dry_run_prefix(self) -> None:
        result = ExecutionResult(outcomes=(outcome("t1", PLANNED),), dry_run=True)
        self.assertTrue(summarise(result).startswith("пробен пуск (dry-run): "))


class TaskOutcomeTest(unittest.TestCase):
    """Outcome helpers used by reporting and state merging."""

    def test_paths_are_deduplicated_in_order(self) -> None:
        recorded = outcome(
            "t1",
            changes=(
                OpResult("write_file", WRITE, "a.md", ""),
                OpResult("write_file", UNCHANGED, "a.md", ""),
            ),
            verify=(OpResult("assert_file_exists", UNCHANGED, "b.md", ""),),
        )
        self.assertEqual(recorded.paths, ("a.md", "b.md"))
        self.assertTrue(recorded.wrote)
        self.assertTrue(recorded.ok)

    def test_to_dict_is_json_ready(self) -> None:
        payload = outcome("t1", changes=(OpResult("write_file", WRITE, "a.md", "ok"),)).to_dict()
        self.assertEqual(payload["id"], "t1")
        self.assertEqual(payload["status"], SUCCESS)
        self.assertEqual(payload["paths"], ["a.md"])
        self.assertEqual(payload["fingerprint"], "fp-t1")
        self.assertEqual(payload["error"], "")

    def test_failed_outcome_is_not_ok(self) -> None:
        self.assertFalse(outcome("t1", BROKEN).ok)


class ExecutionResultTest(unittest.TestCase):
    """Aggregate properties of a run."""

    def test_changed_paths_come_from_applied_tasks_only(self) -> None:
        result = ExecutionResult(
            outcomes=(
                outcome("t1", changes=(OpResult("write_file", WRITE, "b.md", ""),)),
                outcome("t2", PARTIAL, changes=(OpResult("write_file", UNCHANGED, "a.md", ""),)),
                outcome("t3", changes=(OpResult("write_file", WRITE, "a.md", ""),)),
            )
        )
        self.assertEqual(result.changed_paths, ("a.md", "b.md"))
        self.assertEqual([item.id for item in result.applied], ["t1", "t3"])

    def test_to_dict_lists_tasks(self) -> None:
        result = ExecutionResult(outcomes=(outcome("t1"),), fingerprint="fp")
        payload = result.to_dict()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["fingerprint"], "fp")
        self.assertEqual(payload["changed_paths"], [])
        self.assertEqual(payload["tasks"][0]["id"], "t1")


class ExecutionLedgerTest(unittest.TestCase):
    """The CLI accumulator only appends."""

    def test_add_collects_outcomes(self) -> None:
        ledger = ExecutionLedger()
        ledger.add(outcome("t1"))
        ledger.add(outcome("t2", PARTIAL))
        self.assertEqual([item.id for item in ledger.outcomes], ["t1", "t2"])


if __name__ == "__main__":
    unittest.main()
