"""Tests for the persisted installer state and its lock."""

from __future__ import annotations

import json
import os
import unittest

from pasevsu_installer.errors import LockError, StateError
from pasevsu_installer.state import StateStore

from .support import temp_root


class FreshStateTest(unittest.TestCase):
    """A first run starts from a well-defined document."""

    def test_fresh_payload(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            self.assertTrue(store.is_first_run())
            payload = store.fresh("0.1.0")
            self.assertEqual(payload["version"], "0.1.0")
            self.assertEqual(payload["phase"], "new")
            self.assertEqual(payload["runs"], 0)
            self.assertEqual(payload["tasks"], {})

    def test_mark_persists_and_returns_payload(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            saved = store.mark(store.fresh("0.1.0"), "backup", plan_fingerprint="abc")
            self.assertEqual(saved["phase"], "backup")
            self.assertEqual(saved["plan_fingerprint"], "abc")
            reloaded = store.load()
            self.assertEqual(reloaded["phase"], "backup")
            self.assertTrue(store.is_first_run())
            store.mark(reloaded, "selection", first_run_started_at="2026-01-01T00:00:00Z")
            self.assertFalse(store.is_first_run())
            self.assertIn("created_at", reloaded)
            self.assertIn("updated_at", reloaded)

    def test_corrupted_state_is_reported(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            store.path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(StateError):
                store.load()

    def test_state_file_is_gitignored(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            text = (root / ".installer" / ".gitignore").read_text(encoding="utf-8")
            self.assertIn("journal.jsonl", text)
            self.assertIn("report.md", text)


class LockTest(unittest.TestCase):
    """Two concurrent runs can never mutate the repository."""

    def test_second_acquire_is_rejected(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            with store.lock("first"):
                with self.assertRaises(LockError):
                    with store.lock("second"):
                        pass

    def test_stale_lock_is_stolen(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            store.lock_path.write_text(
                json.dumps({"pid": 999999, "purpose": "old"}), encoding="utf-8"
            )
            os.utime(store.lock_path, (0, 0))
            with store.lock("fresh"):
                self.assertTrue(store.lock_path.exists())
            self.assertFalse(store.lock_path.exists())

    def test_lock_is_released_after_failure(self) -> None:
        with temp_root() as root:
            store = StateStore(root)
            store.ensure()
            with self.assertRaises(RuntimeError):
                with store.lock("boom"):
                    raise RuntimeError("boom")
            self.assertFalse(store.lock_path.exists())


if __name__ == "__main__":
    unittest.main()
