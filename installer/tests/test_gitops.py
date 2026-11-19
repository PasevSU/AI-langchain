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


if __name__ == "__main__":
    unittest.main()
