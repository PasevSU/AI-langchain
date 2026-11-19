"""Tests for the mandatory backup release gate."""

from __future__ import annotations

import shutil
import unittest
import zipfile
from pathlib import Path

from pasevsu_installer.backup import Backup, create_backup, verify_backup
from pasevsu_installer.errors import BackupError
from pasevsu_installer.util import run_command, sha256_file

from .support import temp_root, write_text

GIT = shutil.which("git")


def _populate(root: Path) -> None:
    write_text(root, "README.md", "# тест\n")
    write_text(root, "pkg/module.py", "VALUE = 1\n")
    write_text(root, ".installer/state.json", "{}\n")


class ArchiveBackupTest(unittest.TestCase):
    """Without a git repository the backup is a verified ZIP archive."""

    def test_archive_mode_records_both_artefacts(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000000Z")
            self.assertEqual(record.kind, "archive")
            self.assertEqual(record.id, "20260101T000000Z")
            self.assertTrue(record.ok)
            self.assertEqual(Path(record.archive).name, "20260101T000000Z.zip")
            self.assertEqual(Path(record.manifest).name, "20260101T000000Z.json")
            self.assertTrue(Path(record.archive).exists())
            self.assertTrue(Path(record.manifest).exists())
            self.assertEqual(record.archive_sha256, sha256_file(Path(record.archive)))
            self.assertIsNone(record.ref)
            self.assertGreaterEqual(record.files, 2)

    def test_ignored_directories_are_left_out(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000001Z")
            with zipfile.ZipFile(record.archive) as archive:
                names = archive.namelist()
        joined = " ".join(names)
        self.assertIn("README.md", joined)
        self.assertNotIn(".installer", joined)

    def test_second_backup_uses_supplied_stamp(self) -> None:
        with temp_root() as root:
            _populate(root)
            first = create_backup(root, root / "backups", stamp="20260101T000002Z")
            second = create_backup(root, root / "backups", stamp="20260101T000003Z")
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.created_at, "")


class VerifyTest(unittest.TestCase):
    """A damaged snapshot must fail verification, never pass silently."""

    def test_round_trip_passes(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000004Z")
            verify_backup(record)

    def test_missing_digest_fails(self) -> None:
        with temp_root() as root:
            record = create_backup(root, root / "backups", stamp="20260101T000005Z")
            broken = Backup.from_dict({**record.to_dict(), "tree_sha256": ""})
            self.assertFalse(broken.ok)
            with self.assertRaises(BackupError):
                verify_backup(broken)

    def test_tampered_archive_fails(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000006Z")
            Path(record.archive).write_bytes(b"not a zip")
            with self.assertRaises(BackupError):
                verify_backup(record)

    def test_missing_archive_fails(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000007Z")
            Path(record.archive).unlink()
            with self.assertRaises(BackupError):
                verify_backup(record)

    def test_missing_manifest_fails(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000008Z")
            Path(record.manifest).unlink()
            with self.assertRaises(BackupError):
                verify_backup(record)


class RecordTest(unittest.TestCase):
    """Backup records serialise and rebuild without loss."""

    def test_from_dict_defaults(self) -> None:
        record = Backup.from_dict({})
        self.assertEqual(record.kind, "archive")
        self.assertEqual(record.files, 0)
        self.assertEqual(record.version, "")
        self.assertFalse(record.ok)

    def test_to_dict_is_complete(self) -> None:
        with temp_root() as root:
            _populate(root)
            record = create_backup(root, root / "backups", stamp="20260101T000009Z")
        payload = record.to_dict()
        for key in ("id", "created_at", "kind", "tree_sha256", "files", "archive"):
            self.assertIn(key, payload)
        self.assertEqual(Backup.from_dict(payload), record)


@unittest.skipIf(GIT is None, "git липсва в PATH")
class GitTagBackupTest(unittest.TestCase):
    """Inside a repository the backup is an annotated tag plus a ZIP."""

    def test_tag_is_created_and_recorded(self) -> None:
        with temp_root() as root:
            _populate(root)
            run_command([GIT, "init"], cwd=root, timeout=120)
            run_command(
                [GIT, "config", "user.email", "installer@example.invalid"],
                cwd=root,
                timeout=60,
            )
            run_command(
                [GIT, "config", "user.name", "Installer Test"], cwd=root, timeout=60
            )
            run_command([GIT, "add", "."], cwd=root, timeout=120)
            run_command(
                [GIT, "commit", "-m", "chore(test): начален коммит"],
                cwd=root,
                timeout=180,
            )
            record = create_backup(root, root / "backups", stamp="20260101T000010Z")
            tags = run_command([GIT, "tag", "-l"], cwd=root, timeout=120)
            archived = Path(record.archive).exists()
        self.assertEqual(record.kind, "git-tag")
        self.assertIsNotNone(record.ref)
        self.assertTrue(str(record.ref).startswith("installer-backup-"))
        self.assertIn(str(record.ref), tags.stdout)
        self.assertIsNotNone(record.commit)
        self.assertTrue(archived)
        self.assertTrue(record.ok)


if __name__ == "__main__":
    unittest.main()
