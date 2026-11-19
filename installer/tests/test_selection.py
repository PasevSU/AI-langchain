"""Tests for agent/task selection, ordering and exclusion reporting."""

from __future__ import annotations

import unittest

from pasevsu_installer.errors import RegistryError
from pasevsu_installer.selection import report, select

from .support import change, declaration, task


def _write(path: str = "out/one.txt", content: str = "one\n") -> tuple:
    return (change("write_file_if_absent", path=path, content=content),)


class SelectionTest(unittest.TestCase):
    """Only explicitly declared work is selected."""

    def test_selects_declared_task(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=_write()),))
        result = select((agent,))
        self.assertEqual(result.task_ids, ("t-one",))
        self.assertFalse(result.empty)

    def test_disabled_agent_is_excluded(self) -> None:
        agent = declaration(status="disabled", tasks=(task("t-one", changes=_write()),))
        result = select((agent,))
        self.assertTrue(result.empty)
        reason = result.exclusions[0].reason
        self.assertIn("изключен", reason)
        self.assertIn("disabled", reason)

    def test_undeclared_task_is_excluded(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=_write(), declared=False),))
        result = select((agent,))
        self.assertTrue(result.empty)
        self.assertIn("declared=false", result.exclusions[0].reason)

    def test_task_without_changes_is_excluded(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=()),))
        result = select((agent,))
        self.assertTrue(result.empty)
        self.assertIn("без нито една промяна", result.exclusions[0].reason)

    def test_agent_without_tasks_is_excluded(self) -> None:
        agent = declaration(tasks=())
        result = select((agent,))
        self.assertTrue(result.empty)
        self.assertIn("няма дефинирани задачи", result.exclusions[0].reason)

    def test_only_filter(self) -> None:
        one = declaration("agent-one", tasks=(task("t-one", changes=_write(), agent_id="agent-one"),))
        two = declaration("agent-two", tasks=(task("t-two", changes=_write("out/two.txt"), agent_id="agent-two"),))
        result = select((one, two), only=("agent-two",))
        self.assertEqual(result.task_ids, ("t-two",))
        self.assertIn("agent-one", [item.id for item in result.exclusions])

    def test_unknown_agent_in_only_is_an_error(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=_write()),))
        with self.assertRaises(RegistryError):
            select((agent,), only=("ghost",))

    def test_dependency_order(self) -> None:
        agent = declaration(
            tasks=(
                task("t-second", changes=_write("b.txt"), depends_on=("t-first",)),
                task("t-first", changes=_write("a.txt")),
            )
        )
        result = select((agent,))
        self.assertEqual(result.task_ids, ("t-first", "t-second"))

    def test_cycle_is_an_error(self) -> None:
        agent = declaration(
            tasks=(
                task("t-a", changes=_write("a.txt"), depends_on=("t-b",)),
                task("t-b", changes=_write("b.txt"), depends_on=("t-a",)),
            )
        )
        with self.assertRaises(RegistryError):
            select((agent,))

    def test_missing_dependency_is_an_error(self) -> None:
        agent = declaration(
            tasks=(task("t-a", changes=_write("a.txt"), depends_on=("t-missing",)),)
        )
        with self.assertRaises(RegistryError):
            select((agent,))

    def test_applied_task_with_same_fingerprint_is_skipped(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=_write()),))
        first = select((agent,))
        state = {
            "tasks": {
                "t-one": {
                    "status": "applied",
                    "fingerprint": first.tasks[0].fingerprint(),
                }
            }
        }
        second = select((agent,), state)
        self.assertTrue(second.empty)
        self.assertIn("вече приложена", second.exclusions[0].reason)

    def test_changed_content_regenerates_the_task(self) -> None:
        first = select((declaration(tasks=(task("t-one", changes=_write(content="one\n")),)),))
        state = {
            "tasks": {
                "t-one": {
                    "status": "applied",
                    "fingerprint": first.tasks[0].fingerprint(),
                }
            }
        }
        second = select(
            (declaration(tasks=(task("t-one", changes=_write(content="two\n")),)),), state
        )
        self.assertEqual(second.regenerated, ("t-one",))
        self.assertEqual(second.task_ids, ("t-one",))

    def test_fingerprint_is_stable(self) -> None:
        agent = declaration(tasks=(task("t-one", changes=_write()),))
        self.assertEqual(select((agent,)).fingerprint, select((agent,)).fingerprint)


class ReportTest(unittest.TestCase):
    """Exclusions are split by kind for the markdown report."""

    def test_splits_agent_and_task_exclusions(self) -> None:
        disabled = declaration("off", status="disabled", tasks=(task("t-off", changes=_write(), agent_id="off"),))
        undeclared = declaration(
            "on", tasks=(task("t-on", changes=_write(), declared=False, agent_id="on"),)
        )
        result = report(select((disabled, undeclared)))
        self.assertEqual([item.id for item in result.excluded_agents], ["off"])
        self.assertEqual([item.id for item in result.excluded_tasks], ["t-on"])
        payload = result.to_dict()
        self.assertIn("excluded_agents", payload)
        self.assertIn("excluded_tasks", payload)


if __name__ == "__main__":
    unittest.main()
