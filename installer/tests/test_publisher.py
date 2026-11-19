"""Tests for the publisher: commit messages, release notes, publish gates."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from pasevsu_installer import gitops
from pasevsu_installer.config import Config
from pasevsu_installer.errors import PublishError
from pasevsu_installer.publisher import (
    PUBLISHED,
    SKIPPED,
    Publication,
    _switch_branch,
    commit_message,
    publish,
    write_publication_record,
    write_release_notes,
)
from pasevsu_installer.util import read_text, run_command

from .support import temp_root, write_text

GIT = shutil.which("git")


def _config(root: Path, **overrides: object) -> Config:
    base: dict[str, object] = {"root": root, "path": root / "installer.config.json"}
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _init_repo(root: Path) -> None:
    """Create a git repository on ``master`` with a repository-local identity."""
    run_command([GIT, "init", "-b", "master"], cwd=root, timeout=120)
    for key, value in (
        ("user.name", "Installer Test"),
        ("user.email", "installer@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        run_command([GIT, "config", key, value], cwd=root, timeout=120)
    write_text(root, "seed.txt", "seed\n")
    run_command([GIT, "add", "seed.txt"], cwd=root, timeout=120)
    run_command([GIT, "commit", "-m", "chore: seed"], cwd=root, timeout=120)


class CommitMessageTest(unittest.TestCase):
    """Commit headlines are Conventional Commits with a mandatory scope."""

    def test_headline_carries_scope_and_version(self) -> None:
        with temp_root() as root:
            message = commit_message(
                _config(root), version="0.1.0", summary="добавя миграция"
            )
        first = message.splitlines()[0]
        self.assertEqual(first, "feat(installer): добавя миграция (0.1.0)")

    def test_trailers_are_present(self) -> None:
        with temp_root() as root:
            message = commit_message(_config(root), version="1.2.3", summary="нещо")
        self.assertIn("Installer-Version: 1.2.3", message)
        self.assertIn("Installer-Run: ", message)
        self.assertTrue(message.endswith("\n"))
        self.assertNotIn("\r", message)

    def test_empty_summary_falls_back(self) -> None:
        with temp_root() as root:
            message = commit_message(_config(root), version="0.1.0", summary="   ")
        self.assertIn("feat(installer):", message.splitlines()[0])

    def test_body_is_kept_when_given(self) -> None:
        with temp_root() as root:
            message = commit_message(
                _config(root), version="0.1.0", summary="нещо", body="подробности"
            )
        self.assertIn("подробности", message)

    def test_scope_comes_from_configuration(self) -> None:
        with temp_root() as root:
            message = commit_message(
                _config(root, commit_scope="ci"), version="0.1.0", summary="нещо"
            )
        self.assertTrue(message.startswith("feat(ci): "), message)


class PublishGateTest(unittest.TestCase):
    """Publication refuses to run unless every gate passes."""

    def test_disabled_publish_is_skipped(self) -> None:
        with temp_root() as root:
            outcome = publish(
                _config(root, publish_enabled=False),
                version="0.1.0",
                paths=("a.txt",),
                summary="нещо",
            )
        self.assertEqual(outcome.status, SKIPPED)
        self.assertTrue(outcome.ok)
        self.assertIn("изключено", outcome.reason)

    def test_missing_repository_raises(self) -> None:
        with temp_root() as root:
            write_text(root, "a.txt", "x\n")
            with self.assertRaises(PublishError):
                publish(
                    _config(root),
                    version="0.1.0",
                    paths=("a.txt",),
                    summary="нещо",
                )

    def test_empty_paths_fall_back_to_git_status(self) -> None:
        if GIT is None:  # pragma: no cover - environment without git
            self.skipTest("git липсва в PATH")
        with temp_root() as root:
            run_command([GIT, "init"], cwd=root, timeout=120)
            outcome = publish(
                _config(root),
                version="0.1.0",
                paths=(),
                summary="нещо",
                dry_run=True,
            )
        self.assertEqual(outcome.status, SKIPPED)
        self.assertIn("променени файлове", outcome.reason)

    @unittest.skipIf(GIT is None, "git липсва в PATH")
    def test_dry_run_never_commits(self) -> None:
        with temp_root() as root:
            run_command([GIT, "init"], cwd=root, timeout=120)
            write_text(root, "a.txt", "x\n")
            outcome = publish(
                _config(root),
                version="0.1.0",
                paths=("a.txt",),
                summary="нещо",
                dry_run=True,
            )
            log = run_command([GIT, "log", "--oneline"], cwd=root, timeout=120)
        self.assertEqual(outcome.status, SKIPPED)
        self.assertIn("dry-run", outcome.reason)
        self.assertEqual(outcome.tag, "installer-v0.1.0")
        self.assertEqual(outcome.branch, "installer/0.1.0")
        self.assertIn("a.txt", outcome.files)
        self.assertEqual(log.stdout.strip(), "")


@unittest.skipIf(GIT is None, "git липсва в PATH")
class ProtectedBranchTest(unittest.TestCase):
    """Publishing from a protected branch moves the work to a release branch."""

    def test_publish_switches_to_release_branch(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "a.txt", "x\n")
            outcome = publish(
                _config(root, push=False),
                version="0.1.0",
                paths=("a.txt",),
                summary="добавя инсталатор",
            )
            branch = run_command(
                [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=120
            )
            tags = run_command(
                [GIT, "tag", "--list", "installer-v*"], cwd=root, timeout=120
            )
        self.assertEqual(outcome.status, PUBLISHED, outcome.message)
        self.assertEqual(outcome.branch, "installer/0.1.0")
        self.assertEqual(branch.stdout.strip(), "installer/0.1.0")
        self.assertEqual(tags.stdout.strip(), "installer-v0.1.0")
        self.assertTrue(outcome.commit)

    def test_new_version_gets_its_own_branch(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "a.txt", "x\n")
            config = _config(root, push=False)
            first = publish(
                config, version="0.1.0", paths=("a.txt",), summary="първи пуск"
            )
            write_text(root, "b.txt", "y\n")
            second = publish(
                config, version="0.1.1", paths=("b.txt",), summary="втори пуск"
            )
            branch = run_command(
                [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=120
            )
        self.assertEqual(first.status, PUBLISHED, first.message)
        self.assertEqual(second.status, PUBLISHED, second.message)
        self.assertEqual(first.branch, "installer/0.1.0")
        self.assertEqual(second.branch, "installer/0.1.1")
        self.assertEqual(branch.stdout.strip(), "installer/0.1.1")

    def test_working_branch_is_left_alone(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            run_command([GIT, "switch", "-c", "feature/notes"], cwd=root, timeout=120)
            write_text(root, "a.txt", "x\n")
            outcome = publish(
                _config(root, push=False),
                version="0.1.0",
                paths=("a.txt",),
                summary="добавя инсталатор",
            )
            branch = run_command(
                [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=120
            )
        self.assertEqual(outcome.status, PUBLISHED, outcome.message)
        self.assertEqual(outcome.branch, "feature/notes")
        self.assertEqual(branch.stdout.strip(), "feature/notes")

    def test_existing_branch_is_checked_out(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            run_command([GIT, "switch", "-c", "installer/0.1.0"], cwd=root, timeout=120)
            run_command([GIT, "switch", "master"], cwd=root, timeout=120)
            self.assertTrue(gitops.branch_exists(root, "installer/0.1.0"))
            _switch_branch(root, "installer/0.1.0")
            current = run_command(
                [GIT, "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=120
            )
        self.assertEqual(current.stdout.strip(), "installer/0.1.0")


@unittest.skipIf(GIT is None, "git липсва в PATH")
class RepublishTest(unittest.TestCase):
    """A second run with nothing new must still succeed instead of crashing."""

    def test_second_publish_reuses_the_existing_release(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "a.txt", "x\n")
            config = _config(root, push=False)
            first = publish(
                config, version="0.1.0", paths=("a.txt",), summary="добавя инсталатор"
            )
            second = publish(
                config, version="0.1.0", paths=("a.txt",), summary="добавя инсталатор"
            )
            log = run_command([GIT, "rev-list", "--count", "HEAD"], cwd=root, timeout=120)
            tags = run_command(
                [GIT, "tag", "--list", "installer-v*"], cwd=root, timeout=120
            )
        self.assertEqual(first.status, PUBLISHED, first.message)
        self.assertEqual(second.status, PUBLISHED, second.message)
        self.assertEqual(log.stdout.strip(), "2")
        self.assertEqual(tags.stdout.strip(), "installer-v0.1.0")
        self.assertIn("нищо ново", second.reason)
        self.assertEqual(second.commit, first.commit)


class PathNormalisationTest(unittest.TestCase):
    """Publish paths are cleaned without eating a leading dot directory."""

    def test_relative_prefix_is_stripped(self) -> None:
        if GIT is None:  # pragma: no cover - environment without git
            self.skipTest("git липсва в PATH")
        with temp_root() as root:
            run_command([GIT, "init"], cwd=root, timeout=120)
            outcome = publish(
                _config(root),
                version="0.1.0",
                paths=("./installer/a.py", "installer\\b.py"),
                summary="нещо",
                dry_run=True,
            )
        self.assertEqual(outcome.files, ("installer/a.py", "installer/b.py"))

    def test_state_directory_is_never_published(self) -> None:
        if GIT is None:  # pragma: no cover - environment without git
            self.skipTest("git липсва в PATH")
        with temp_root() as root:
            run_command([GIT, "init"], cwd=root, timeout=120)
            outcome = publish(
                _config(root),
                version="0.1.0",
                paths=(".installer/selection-manifest.json", "installer/a.py"),
                summary="нещо",
                dry_run=True,
            )
        self.assertEqual(outcome.files, ("installer/a.py",))


class ReleaseNotesTest(unittest.TestCase):
    """Release notes land inside the configured directory."""

    def test_notes_file_is_written(self) -> None:
        with temp_root() as root:
            config = _config(root, name="pasevsu-installer-agent")
            path = write_release_notes(config, version="0.1.0", body="Първи пуск.")
            text = read_text(path)
        self.assertEqual(path.parent.name, "RELEASE-NOTES")
        self.assertEqual(path.name, "0.1.0.md")
        self.assertIn("pasevsu-installer-agent 0.1.0", text)
        self.assertIn("Първи пуск.", text)

    def test_body_is_stripped(self) -> None:
        with temp_root() as root:
            path = write_release_notes(_config(root), version="9.9.9", body="\n\nтяло\n\n")
            text = read_text(path)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\n\n\n", text)


class PublicationRecordTest(unittest.TestCase):
    """The publication payload is persisted for later inspection."""

    def test_record_is_json(self) -> None:
        with temp_root() as root:
            config = _config(root, state_dir=".installer")
            publication = Publication(
                PUBLISHED, branch="installer/0.1.0", commit="abc", tag="installer-v0.1.0"
            )
            path = write_publication_record(config, publication)
            text = read_text(path)
        self.assertEqual(path.name, "publish.json")
        self.assertIn('"status": "published"', text)
        self.assertIn("installer-v0.1.0", text)

    def test_ok_flag(self) -> None:
        self.assertTrue(Publication(PUBLISHED).ok)
        self.assertTrue(Publication(SKIPPED).ok)
        self.assertFalse(Publication("failed").ok)


if __name__ == "__main__":
    unittest.main()
