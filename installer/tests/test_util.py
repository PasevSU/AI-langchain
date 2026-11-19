"""Tests for the low-level helpers: hashing, atomic writes, subprocesses."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from pasevsu_installer.util import (
    CompletedCommand,
    atomic_write_text,
    dump_json,
    read_text,
    run_command,
    sha256_file,
    sha256_text,
    utc_now,
    utc_stamp,
)

from .support import temp_root


class StampTest(unittest.TestCase):
    """Timestamps are stable, sortable and UTC."""

    def test_utc_now_is_iso_z(self) -> None:
        value = utc_now()
        self.assertTrue(value.endswith("Z"), value)
        self.assertIn("T", value)
        self.assertEqual(len(value), 20)

    def test_utc_stamp_shape(self) -> None:
        stamp = utc_stamp()
        self.assertRegex(stamp, r"^\d{8}T\d{6}Z$")
        self.assertEqual(len(stamp), 16)


class HashTest(unittest.TestCase):
    """Text and file digests agree."""

    def test_sha256_text_matches_file(self) -> None:
        with temp_root() as root:
            path = root / "sample.txt"
            atomic_write_text(path, "здравей\n")
            self.assertEqual(sha256_text("здравей\n"), sha256_file(path))

    def test_sha256_text_is_stable(self) -> None:
        self.assertEqual(sha256_text("a"), sha256_text("a"))
        self.assertNotEqual(sha256_text("a"), sha256_text("b"))
        self.assertEqual(len(sha256_text("a")), 64)


class WriteTest(unittest.TestCase):
    """Writes are UTF-8, LF-preserving and create missing parents."""

    def test_atomic_write_keeps_lf_and_creates_parents(self) -> None:
        with temp_root() as root:
            target = root / "deep" / "dir" / "file.txt"
            atomic_write_text(target, "едно\nдве\n")
            raw = target.read_bytes()
            self.assertNotIn(b"\r", raw)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(read_text(target), "едно\nдве\n")

    def test_dump_json_is_utf8_and_newline_terminated(self) -> None:
        with temp_root() as root:
            text = dump_json({"име": "стойност"})
            self.assertTrue(text.endswith("\n"))
            target = root / "data.json"
            atomic_write_text(target, text)
            raw = target.read_bytes()
            self.assertIn("име".encode(), raw)
            self.assertTrue(raw.endswith(b"\n"))
            self.assertIn(b"\n  ", raw)

    def test_rewrite_is_idempotent(self) -> None:
        with temp_root() as root:
            target = root / "a.txt"
            atomic_write_text(target, "x\n")
            before = sha256_file(target)
            atomic_write_text(target, "x\n")
            self.assertEqual(before, sha256_file(target))


class CommandTest(unittest.TestCase):
    """Command execution never raises for ordinary failures."""

    def test_successful_command(self) -> None:
        result = run_command(
            [sys.executable, "-c", "print('ok')"], cwd=Path.cwd(), timeout=120
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("ok", result.stdout)
        self.assertGreaterEqual(result.duration_ms, 0)

    def test_missing_binary_returns_127(self) -> None:
        result = run_command(
            ["pasevsu-definitely-missing-binary"], cwd=Path.cwd(), timeout=60
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 127)
        self.assertTrue(result.stderr)

    def test_nonzero_exit_is_reported(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=Path.cwd(), timeout=120
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)

    def test_timeout_returns_124(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=Path.cwd(),
            timeout=1,
        )
        self.assertEqual(result.exit_code, 124)

    def test_tail_returns_last_lines(self) -> None:
        result = run_command(
            [sys.executable, "-c", "print(''.join(f'l{i}\\n' for i in range(50)))"],
            cwd=Path.cwd(),
            timeout=120,
        )
        tail = result.tail(5)
        self.assertIn("l49", tail)
        self.assertNotIn("l0\n", tail)

    def test_completed_command_serialises(self) -> None:
        record = CompletedCommand(
            argv=("py", "-V"), exit_code=0, stdout="ok\n", stderr="", duration_ms=5
        )
        payload = record.to_dict()
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["duration_ms"], 5)
        self.assertTrue(record.ok)


if __name__ == "__main__":
    unittest.main()
