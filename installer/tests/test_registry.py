"""Tests for declaration loading and validation."""

from __future__ import annotations

import unittest

from pasevsu_installer.errors import RegistryError
from pasevsu_installer.model import AGENT_SCHEMA
from pasevsu_installer.registry import load_declaration, load_declarations

from .support import AGENTS_DIR, dump_json, temp_root


def _agent(agent_id: str = "sample", **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": AGENT_SCHEMA,
        "id": agent_id,
        "title": "Sample",
        "status": "active",
        "tasks": [
            {
                "id": "sample-task",
                "title": "Sample task",
                "declared": True,
                "changes": [
                    {
                        "op": "write_file_if_absent",
                        "params": {"path": "f.txt", "content": "x\n"},
                    }
                ],
                "verify": [{"op": "assert_file_exists", "params": {"path": "f.txt"}}],
            }
        ],
    }
    payload.update(overrides)
    return payload


class RealRegistryTest(unittest.TestCase):
    """The declarations shipped in ``installer/agents`` must load cleanly."""

    def test_shipped_agents_load(self) -> None:
        declarations = load_declarations(AGENTS_DIR)
        self.assertGreaterEqual(len(declarations), 2)
        by_id = {item.id: item for item in declarations}
        self.assertIn("installer-bootstrap", by_id)
        self.assertIn("installer-docs", by_id)

    def test_every_shipped_task_is_declared_and_verified(self) -> None:
        for declaration in load_declarations(AGENTS_DIR):
            for task in declaration.tasks:
                self.assertTrue(task.declared, msg=f"{task.id} не е declared")
                self.assertTrue(task.verify, msg=f"{task.id} няма verify стъпка")
                if not task.changes:
                    self.assertTrue(
                        task.scope, msg=f"{task.id}: пазач без обявен scope"
                    )

    def test_disabled_agent_is_kept_visible(self) -> None:
        statuses = {item.id: item.status for item in load_declarations(AGENTS_DIR)}
        self.assertEqual(statuses.get("repo-hygiene"), "disabled")


class ValidationTest(unittest.TestCase):
    """Invalid declarations are rejected with an actionable message."""

    def test_missing_directory(self) -> None:
        with temp_root() as root, self.assertRaises(RegistryError):
            load_declarations(root / "absent")

    def test_empty_directory(self) -> None:
        with temp_root() as root:
            (root / "agents").mkdir()
            with self.assertRaises(RegistryError):
                load_declarations(root / "agents")

    def test_unknown_schema(self) -> None:
        with temp_root() as root:
            path = dump_json(root, "agents/a.json", _agent(schema="other.schema.v1"))
            with self.assertRaises(RegistryError):
                load_declaration(path)

    def test_invalid_identifier(self) -> None:
        with temp_root() as root:
            path = dump_json(root, "agents/a.json", _agent("Bad_ID"))
            with self.assertRaises(RegistryError):
                load_declaration(path)

    def test_unknown_operation(self) -> None:
        with temp_root() as root:
            payload = _agent()
            payload["tasks"][0]["changes"][0]["op"] = "shell_exec"  # type: ignore[index]
            path = dump_json(root, "agents/a.json", payload)
            with self.assertRaises(RegistryError):
                load_declaration(path)

    def test_mutating_op_in_verify_is_rejected(self) -> None:
        with temp_root() as root:
            payload = _agent()
            payload["tasks"][0]["verify"] = [
                {"op": "write_file_if_absent", "params": {"path": "f.txt", "content": "x\n"}}
            ]  # type: ignore[index]
            path = dump_json(root, "agents/a.json", payload)
            with self.assertRaises(RegistryError):
                load_declaration(path)

    def test_read_only_op_in_changes_is_rejected(self) -> None:
        with temp_root() as root:
            payload = _agent()
            payload["tasks"][0]["changes"] = [
                {"op": "assert_file_exists", "params": {"path": "f.txt"}}
            ]  # type: ignore[index]
            path = dump_json(root, "agents/a.json", payload)
            with self.assertRaises(RegistryError):
                load_declaration(path)

    def test_duplicate_agent_ids_across_files(self) -> None:
        with temp_root() as root:
            dump_json(root, "agents/a.json", _agent("dup"))
            dump_json(root, "agents/b.json", _agent("dup"))
            with self.assertRaises(RegistryError):
                load_declarations(root / "agents")

    def test_duplicate_task_ids_across_files(self) -> None:
        with temp_root() as root:
            dump_json(root, "agents/a.json", _agent("one"))
            dump_json(root, "agents/b.json", _agent("two"))
            with self.assertRaises(RegistryError):
                load_declarations(root / "agents")

    def test_bad_status(self) -> None:
        with temp_root() as root:
            path = dump_json(root, "agents/a.json", _agent(status="paused"))
            with self.assertRaises(RegistryError):
                load_declaration(path)


if __name__ == "__main__":
    unittest.main()
