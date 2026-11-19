"""Tests for the closed set of file operations."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from pasevsu_installer.errors import OperationError
from pasevsu_installer.operations import (
    FAILED,
    PLANNED,
    UNCHANGED,
    WRITE,
    OpContext,
    apply_change,
    plan_change,
)

from .support import change, temp_root


class ResolveTargetTest(unittest.TestCase):
    """The repository root is a hard boundary."""

    def test_rejects_traversal(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(
                    OpContext(root),
                    change("write_file_if_absent", path="../escape.md", content="x\n"),
                )

    def test_rejects_absolute_path(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(
                    OpContext(root),
                    change("write_file_if_absent", path="C:/Windows/x.md", content="x\n"),
                )

    def test_rejects_empty_path(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(OpContext(root), change("assert_file_exists", path=""))


class WriteFileIfAbsentTest(unittest.TestCase):
    """Creating a file never overwrites it."""

    def test_creates_then_reports_unchanged(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            payload = change(
                "write_file_if_absent", path="docs/NOTES.md", content="здравей\n"
            )
            first = apply_change(ctx, payload)
            self.assertEqual(first.status, WRITE)
            self.assertEqual((root / "docs" / "NOTES.md").read_text(encoding="utf-8"), "здравей\n")
            second = apply_change(ctx, payload)
            self.assertEqual(second.status, UNCHANGED)
            self.assertEqual(second.detail, "файлът вече съществува")

    def test_dry_run_creates_nothing(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            result = plan_change(
                ctx, change("write_file_if_absent", path="a/b.md", content="x\n")
            )
            self.assertEqual(result.status, PLANNED)
            self.assertFalse((root / "a").exists())


class WriteFileTest(unittest.TestCase):
    """Overwriting requires an explicit opt-in."""

    def test_requires_allow_overwrite(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(
                    OpContext(root), change("write_file", path="x.md", content="x\n")
                )

    def test_identical_content_is_unchanged(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            payload = change(
                "write_file", path="x.md", content="x\n", allow_overwrite=True
            )
            self.assertEqual(apply_change(ctx, payload).status, WRITE)
            again = apply_change(ctx, payload)
            self.assertEqual(again.status, UNCHANGED)
            self.assertEqual(again.detail, "съдържанието е идентично")

    def test_overwrites_when_content_differs(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            apply_change(
                ctx, change("write_file", path="x.md", content="old\n", allow_overwrite=True)
            )
            result = apply_change(
                ctx, change("write_file", path="x.md", content="new\n", allow_overwrite=True)
            )
            self.assertEqual(result.status, WRITE)
            self.assertEqual((root / "x.md").read_text(encoding="utf-8"), "new\n")


class AppendBlockIfAbsentTest(unittest.TestCase):
    """Blocks are idempotent thanks to their marker."""

    def test_appends_once(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            payload = change(
                "append_block_if_absent",
                path="README.md",
                marker="<!-- mark -->",
                content="<!-- mark -->\nтекст\n",
            )
            self.assertEqual(apply_change(ctx, payload).status, WRITE)
            again = apply_change(ctx, payload)
            self.assertEqual(again.status, UNCHANGED)
            self.assertEqual(again.detail, "блокът вече е наличен")
            self.assertEqual(
                (root / "README.md").read_text(encoding="utf-8").count("<!-- mark -->"), 1
            )

    def test_marker_must_be_inside_content(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(
                    OpContext(root),
                    change(
                        "append_block_if_absent",
                        path="README.md",
                        marker="<!-- mark -->",
                        content="без маркер\n",
                    ),
                )


class ReplaceOnceTest(unittest.TestCase):
    """``replace_once`` refuses anything but a single unambiguous match."""

    def test_replaces_single_match(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            apply_change(
                ctx, change("write_file_if_absent", path="f.txt", content="alpha beta\n")
            )
            result = apply_change(
                ctx, change("replace_once", path="f.txt", search="beta", replacement="gamma")
            )
            self.assertEqual(result.status, WRITE)
            self.assertEqual((root / "f.txt").read_text(encoding="utf-8"), "alpha gamma\n")

    def test_missing_fragment_fails(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            apply_change(ctx, change("write_file_if_absent", path="f.txt", content="alpha\n"))
            result = apply_change(
                ctx, change("replace_once", path="f.txt", search="zzz", replacement="y")
            )
            self.assertEqual(result.status, FAILED)
            self.assertEqual(result.detail, "търсеният фрагмент липсва")

    def test_ambiguous_fragment_fails(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root)
            apply_change(
                ctx, change("write_file_if_absent", path="f.txt", content="a\na\n")
            )
            result = apply_change(
                ctx, change("replace_once", path="f.txt", search="a", replacement="b")
            )
            self.assertEqual(result.status, FAILED)
            self.assertIn("нееднозначно", result.detail)

    def test_missing_file_fails(self) -> None:
        with temp_root() as root:
            result = apply_change(
                OpContext(root), change("replace_once", path="nope.txt", search="a", replacement="b")
            )
            self.assertEqual(result.status, FAILED)
            self.assertEqual(result.detail, "файлът липсва")


class SelectionManifestTest(unittest.TestCase):
    """The manifest records exactly what the run decided."""

    def test_writes_selection_json(self) -> None:
        with temp_root() as root:
            ctx = OpContext(root, {"agents": ["a"], "tasks": ["t"]})
            payload = change("emit_selection_manifest", path=".installer/manifest.json")
            self.assertEqual(apply_change(ctx, payload).status, WRITE)
            data = json.loads((root / ".installer" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(data["agents"], ["a"])
            self.assertEqual(apply_change(ctx, payload).status, UNCHANGED)


class AssertionsTest(unittest.TestCase):
    """Read-only assertions report failures instead of raising."""

    def test_assert_file_exists_on_missing_file(self) -> None:
        with temp_root() as root:
            result = apply_change(
                OpContext(root), change("assert_file_exists", path="ghost.md")
            )
            self.assertEqual(result.status, FAILED)

    def test_assert_file_contains(self) -> None:
        with temp_root() as root:
            (Path(root) / "f.txt").write_text("hello world\n", encoding="utf-8")
            good = apply_change(
                OpContext(root), change("assert_file_contains", path="f.txt", text="hello")
            )
            self.assertTrue(good.ok)
            bad = apply_change(
                OpContext(root), change("assert_file_contains", path="f.txt", text="bye")
            )
            self.assertEqual(bad.status, FAILED)


class UnknownOpTest(unittest.TestCase):
    """The operation set is closed."""

    def test_unknown_op_is_rejected(self) -> None:
        with temp_root() as root:
            with self.assertRaises(OperationError):
                apply_change(OpContext(root), change("rm_rf", path="."))


if __name__ == "__main__":
    unittest.main()
