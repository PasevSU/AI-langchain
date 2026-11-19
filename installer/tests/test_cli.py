"""Tests for :mod:`pasevsu_installer.cli`.

The CLI is the only layer that wires selection, backup, execution, tests and
publication together, so these tests drive it exactly the way a user does: a
temporary root, a configuration file, a declaration, and ``cli.run``. Every
scenario stays offline — publishing is disabled and the test suites are empty,
so no test needs a git remote or a toolchain.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from typing import Any

from pasevsu_installer import cli
from pasevsu_installer.journal import Journal
from pasevsu_installer.publisher import DIFF_NOTHING, diff_summary
from pasevsu_installer.state import LOCK_TTL_S, StateStore

from .support import dump_json, temp_root, write_text

CONFIG: dict[str, Any] = {
    "schema": "pasevsu.installer.config.v1",
    "name": "test-installer",
    "version_file": "installer/VERSION",
    "registry_dir": "installer/agents",
    "state_dir": ".installer",
    "release_notes_dir": "installer/RELEASE-NOTES",
    "commit_scope": "installer",
    "tag_prefix": "installer-v",
    "backup": {"tag_prefix": "installer-backup"},
    "publish": {"enabled": False, "push": False, "allow_master": False},
    "test_suites": [],
}

WRITER: dict[str, Any] = {
    "id": "write-note",
    "title": "Записва бележка",
    "declared": True,
    "risk": "low",
    "scope": ["docs/"],
    "changes": [
        {
            "op": "write_file_if_absent",
            "params": {"path": "docs/note.md", "content": "здравей\n"},
        }
    ],
    "verify": [{"op": "assert_file_exists", "params": {"path": "docs/note.md"}}],
}

# A declaration that re-declares the version already on disk: every operation is
# idempotent, so the run must end as a no-op instead of a release.
BUMP_ONLY: dict[str, Any] = {
    "id": "bump-version",
    "title": "Обявява версия",
    "declared": True,
    "risk": "low",
    "scope": ["installer/VERSION"],
    "changes": [{"op": "bump_version", "params": {"version": "0.1.0"}}],
    "verify": [{"op": "assert_version", "params": {"version": "0.1.0"}}],
}

# A guard has verification but no change: it can never write a file.
GUARD_ONLY: dict[str, Any] = {
    "id": "release-guard",
    "title": "Пазач на версията",
    "declared": True,
    "risk": "low",
    "scope": ["installer/VERSION"],
    "changes": [],
    "verify": [{"op": "assert_version", "params": {"version": "0.1.0"}}],
}


def project(root: Path, *, version: str = "0.1.0") -> None:
    """Lay out a minimal project the installer can run against.

    All three version sites have to exist: ``bump_version`` and
    ``assert_version`` refuse to work when one of them is missing.
    """
    write_text(root, "installer/VERSION", version + "\n")
    write_text(
        root,
        "installer/pyproject.toml",
        f'[project]\nname = "test-installer"\nversion = "{version}"\n',
    )
    write_text(
        root,
        "installer/pasevsu_installer/__init__.py",
        f'__version__ = "{version}"\n',
    )
    dump_json(root, "installer/installer.config.json", CONFIG)


def agent(root: Path, *tasks: dict[str, Any], agent_id: str = "test-agent") -> None:
    """Register one agent declaration holding ``tasks``."""
    dump_json(
        root,
        f"installer/agents/{agent_id}.json",
        {
            "schema": "pasevsu.installer.agent.v1",
            "id": agent_id,
            "title": agent_id,
            "status": "active",
            "tasks": list(tasks),
        },
    )


def run_cli(root: Path, *argv: str) -> tuple[int, str]:
    """Run the CLI against ``root`` and return its exit code and full output."""
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.run([*argv, "--root", str(root)])
    return code, out.getvalue() + err.getvalue()


def events_of(root: Path) -> list[dict[str, Any]]:
    """Read the run journal the installer writes into its state directory."""
    return Journal(StateStore(root).artifacts("journal.jsonl")).events()


class ParserTest(unittest.TestCase):
    """The parser must stay forgiving about the verb and strict about typos."""

    def test_default_verb_is_install(self) -> None:
        args = cli.build_parser().parse_args([])

        self.assertEqual(args.verb, "install")
        self.assertFalse(args.dry_run)
        self.assertFalse(args.status)

    def test_shortcut_flags_are_booleans(self) -> None:
        args = cli.build_parser().parse_args(["--status", "--force", "--no-tests"])

        self.assertTrue(args.status)
        self.assertTrue(args.force)
        self.assertTrue(args.no_tests)

    def test_unknown_verb_is_rejected(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            cli.build_parser().parse_args(["nope"])

        self.assertEqual(caught.exception.code, cli.EXIT_CONFIG)


class InstallTest(unittest.TestCase):
    """A full migration end-to-end, still without any network or git."""

    def test_first_run_applies_and_finishes(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)

            code, text = run_cli(root, "install")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("Готово: миграцията приключи.", text)
            self.assertTrue((root / "docs" / "note.md").is_file())
            state = StateStore(root).load()
            self.assertEqual(state["phase"], "done")
            self.assertEqual(state["runs"], 1)

    def test_completed_run_is_reported_without_force(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            code, text = run_cli(root, "install")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("вече е завършил успешно", text)
            self.assertEqual(StateStore(root).load()["runs"], 1)

    def test_dry_run_plans_without_touching_the_tree(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)

            code, text = run_cli(root, "install", "--dry-run")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("Пробен пуск: нищо не е променено.", text)
            self.assertFalse((root / "docs" / "note.md").exists())
            self.assertEqual(StateStore(root).load()["phase"], "planned")

    def test_fully_idempotent_declaration_ends_as_a_noop(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, BUMP_ONLY)

            code, text = run_cli(root, "install")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("нищо не е променено", text)
            state = StateStore(root).load()
            self.assertEqual(state["phase"], "noop")
            self.assertEqual(state["history"][-1]["phase"], "noop")
            self.assertFalse((root / "installer" / "RELEASE-NOTES").exists())

    def test_guard_only_declaration_ends_as_a_noop(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, GUARD_ONLY)

            code, text = run_cli(root, "install")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("без тестове и без публикуване", text)
            self.assertEqual(StateStore(root).load()["phase"], "noop")

    def test_force_bypasses_the_completed_guard(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            code, text = run_cli(root, "install", "--force")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertNotIn("вече е завършил успешно", text)
            # ``--force`` lifts the completed guard only; the applied-task skip
            # still applies, so nothing is written a second time.
            self.assertIn("Няма заявени промени за прилагане", text)
            self.assertEqual(StateStore(root).load()["runs"], 1)

    def test_missing_configuration_is_a_config_error(self) -> None:
        with temp_root() as root:
            code, text = run_cli(root, "install")

            self.assertEqual(code, cli.EXIT_CONFIG, msg=text)
            self.assertIn("конфигурация", text)

    def test_report_and_journal_are_written(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            store = StateStore(root)
            self.assertTrue(store.artifacts("report.md").is_file())
            self.assertTrue(store.artifacts("journal.jsonl").is_file())
            self.assertTrue(store.artifacts("selection.json").is_file())

    def test_every_journal_event_carries_run_and_phase(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            events = events_of(root)

            self.assertTrue(events)
            for event in events:
                self.assertIn("run", event, msg=event)
                self.assertIn("phase", event, msg=event)
            phases = {event["phase"] for event in events}
            self.assertIn("selection", phases)
            self.assertIn("apply", phases)
            # The journal records the phases a run actually passes through; the
            # final ``done`` phase carries no event of its own, so it is asserted
            # on the persisted state instead.
            self.assertEqual(StateStore(root).load()["phase"], "done")

    def test_history_records_the_run(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            state = StateStore(root).load()
            history = state["history"]

            self.assertEqual(len(history), 1)
            entry = history[0]
            self.assertEqual(entry["phase"], "done")
            self.assertEqual(entry["run"], events_of(root)[0]["run"])
            self.assertEqual(entry["applied"], 1)
            self.assertIn("docs/note.md", entry["paths"])


class StatusAndResetTest(unittest.TestCase):
    """``--status`` reads and ``--reset`` deletes — both must be careful."""

    def test_status_without_state_is_harmless(self) -> None:
        with temp_root() as root:
            project(root)

            code, text = run_cli(root, "--status")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("не е стартиран", text)

    def test_status_prints_the_history(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            code, text = run_cli(root, "--status")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertIn("история: 1", text)
            self.assertIn("done", text)

    def test_reset_needs_the_force_flag(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            code, text = run_cli(root, "--reset")

            self.assertEqual(code, cli.EXIT_CONFIG, msg=text)
            self.assertIn("--force", text)
            self.assertTrue(StateStore(root).path.exists())

    def test_reset_with_force_clears_the_state(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")

            code, text = run_cli(root, "--reset", "--force")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertFalse(StateStore(root).path.exists())

    def test_reset_refuses_a_live_foreign_lock(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")
            store = StateStore(root)
            holder = {"pid": 123456, "purpose": "migration", "at": "2026-01-01T00:00:00Z"}
            store.lock_path.write_text(json.dumps(holder), encoding="utf-8")

            code, text = run_cli(root, "--reset", "--force")

            self.assertEqual(code, cli.EXIT_LOCKED, msg=text)
            self.assertIn("ключалката се държи", text)
            self.assertTrue(store.path.exists())

    def test_reset_removes_a_stale_lock(self) -> None:
        with temp_root() as root:
            project(root)
            agent(root, WRITER)
            run_cli(root, "install")
            store = StateStore(root)
            store.lock_path.write_text(
                json.dumps({"pid": 999999, "purpose": "migration"}), encoding="utf-8"
            )
            old = time.time() - (LOCK_TTL_S * 2)
            os.utime(store.lock_path, (old, old))

            code, text = run_cli(root, "--reset", "--force")

            self.assertEqual(code, cli.EXIT_OK, msg=text)
            self.assertFalse(store.path.exists())
            self.assertFalse(store.lock_path.exists())


class DiffSummaryTest(unittest.TestCase):
    """The commit headline must describe the index, never the wish."""

    def test_empty_index_is_named(self) -> None:
        self.assertEqual(diff_summary({"files": 0}), DIFF_NOTHING)

    def test_counts_are_reported(self) -> None:
        self.assertEqual(
            diff_summary({"files": 2, "added": 7, "removed": 3}),
            "миграция: 2 файла, +7/-3 реда",
        )

    def test_dry_run_is_marked(self) -> None:
        self.assertEqual(
            diff_summary({"files": 1, "added": 1, "removed": 0}, dry_run=True),
            "пробен пуск — миграция: 1 файла, +1/-0 реда",
        )

    def test_unknown_counters_degrade_to_the_file_count(self) -> None:
        self.assertEqual(
            diff_summary({"files": 3, "added": None, "removed": None}),
            "миграция: 3 файла",
        )


if __name__ == "__main__":
    unittest.main()
