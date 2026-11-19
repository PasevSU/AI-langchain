"""Tests for :mod:`pasevsu_installer.gitops`."""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from pasevsu_installer import gitops
from pasevsu_installer.util import run_command

from .support import temp_root, write_text

GIT = shutil.which("git")


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


@unittest.skipIf(GIT is None, "git липсва в PATH")
class ChangedPathsTest(unittest.TestCase):
    """``git status --porcelain`` prefixes must never eat into the path."""

    def test_modified_file_keeps_its_first_character(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "README.md", "seed\n")
            run_command([GIT, "add", "README.md"], cwd=root, timeout=120)
            run_command([GIT, "commit", "-m", "chore: seed"], cwd=root, timeout=120)
            write_text(root, "README.md", "seed\nchanged\n")

            self.assertEqual(gitops.changed_paths(root), ["README.md"])

    def test_untracked_and_modified_paths_are_listed(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "AGENTS.md", "seed\n")
            run_command([GIT, "add", "AGENTS.md"], cwd=root, timeout=120)
            run_command([GIT, "commit", "-m", "chore: seed"], cwd=root, timeout=120)
            write_text(root, "AGENTS.md", "seed\nmore\n")
            write_text(root, "docs/new file.md", "new\n")

            self.assertEqual(
                gitops.changed_paths(root),
                ["AGENTS.md", "docs/new file.md"],
            )


@unittest.skipIf(GIT is None, "git липсва в PATH")
class BranchHelpersTest(unittest.TestCase):
    """Branch probing must use a boolean, never a raw command object."""

    def test_branch_exists_reflects_local_refs(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "seed.txt", "seed\n")
            run_command([GIT, "add", "seed.txt"], cwd=root, timeout=120)
            run_command([GIT, "commit", "-m", "chore: seed"], cwd=root, timeout=120)

            self.assertTrue(gitops.branch_exists(root, "master"))
            self.assertFalse(gitops.branch_exists(root, "installer/0.1.0"))


@unittest.skipIf(GIT is None, "git липсва в PATH")
class IndexHelpersTest(unittest.TestCase):
    """The index and tag probes must be booleans/lists, never command objects."""

    def test_staged_paths_reports_only_staged_work(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "seed.txt", "changed\n")
            self.assertEqual(gitops.staged_paths(root), [])

            self.assertTrue(gitops.stage(root, ["seed.txt"]).ok)
            self.assertEqual(gitops.staged_paths(root), ["seed.txt"])

    def test_tag_exists_follows_created_tags(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            self.assertFalse(gitops.tag_exists(root, "installer-v0.1.0"))

            self.assertTrue(gitops.tag(root, "installer-v0.1.0", "първи").ok)
            self.assertTrue(gitops.tag_exists(root, "installer-v0.1.0"))

    def test_staged_stat_counts_the_very_first_commit(self) -> None:
        with temp_root() as root:
            run_command([GIT, "init", "-b", "master"], cwd=root, timeout=120)
            write_text(root, "a.txt", "one\ntwo\n")
            self.assertTrue(gitops.stage(root, ["a.txt"]).ok)

            stat = gitops.staged_stat(root)

            self.assertEqual(stat["files"], 1)
            self.assertEqual(stat["added"], 2)
            self.assertEqual(stat["removed"], 0)

    def test_unstage_works_before_the_first_commit(self) -> None:
        with temp_root() as root:
            run_command([GIT, "init", "-b", "master"], cwd=root, timeout=120)
            write_text(root, "a.txt", "one\n")
            self.assertTrue(gitops.stage(root, ["a.txt"]).ok)
            self.assertEqual(gitops.staged_paths(root), ["a.txt"])

            gitops.unstage(root, ["a.txt"])

            self.assertEqual(gitops.staged_paths(root), [])

    def test_unstage_ignores_an_empty_path_list(self) -> None:
        with temp_root() as root:
            _init_repo(root)
            write_text(root, "seed.txt", "changed\n")
            self.assertTrue(gitops.stage(root, ["seed.txt"]).ok)

            gitops.unstage(root, [])

            self.assertEqual(gitops.staged_paths(root), ["seed.txt"])


if __name__ == "__main__":
    unittest.main()
