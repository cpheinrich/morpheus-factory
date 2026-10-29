"""Selection boundaries for pressure-based Morpheus worktree cleanup."""

import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


source = Path(__file__).resolve().parents[1] / "scripts/cleanup-old-worktrees.py"
spec = importlib.util.spec_from_file_location("worktree_cleanup", source)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class WorktreeSelectionTests(unittest.TestCase):
    def test_only_old_managed_task_worktrees_are_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / ".morpheus-worktrees" / "old"
            recent = root / ".morpheus-worktrees" / "recent"
            other = root / "unmanaged"
            for path in (old, recent, other):
                path.mkdir(parents=True)
            age = time.time() - 20 * cleanup.DAY
            for path in (old, other):
                os.utime(path, (age, age))
            records = "".join(
                f"worktree {path}\0HEAD {'a' * 40}\0branch refs/heads/ev-26-09-25-22.37.28-sample\0\0"
                for path in (old, recent, other)
            ).encode()
            with patch.object(cleanup.subprocess, "check_output", return_value=records):
                self.assertEqual(list(cleanup.candidates(root, 14)),
                                 [(old, "EV-26-09-25-22.37.28", "a" * 40)])

    def test_deletion_requires_exact_successful_morpheus_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".morpheus-worktrees" / "task"
            path.mkdir(parents=True)
            candidate = (path, "EV-26-09-25-22.37.28", "a" * 40)
            results = [Mock(returncode=0, stdout="", stderr=""),
                       Mock(returncode=0, stdout=f"Safe to remove: {root / 'other'} (branch)", stderr="")]
            with patch.object(cleanup, "repository_roots", return_value=[root]), \
                 patch.object(cleanup, "candidates", return_value=[candidate]), \
                 patch.object(cleanup.shutil, "which", return_value="morpheus"), \
                 patch.object(cleanup.subprocess, "run", side_effect=results) as run, \
                 patch.object(cleanup, "load_attempts", return_value={}), \
                 patch.object(cleanup, "save_attempts"), \
                 patch.object(cleanup, "free_gib", return_value=0), \
                 patch.object(cleanup, "HOME", root), \
                 patch.object(__import__("sys"), "argv", ["cleanup-old-worktrees", "--apply"]):
                self.assertEqual(cleanup.main(), 0)
                self.assertEqual(run.call_count, 2)

    def test_exact_proof_runs_morpheus_recheck(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".morpheus-worktrees" / "task"
            path.mkdir(parents=True)
            candidate = (path, "EV-26-09-25-22.37.28", "a" * 40)
            results = [Mock(returncode=0, stdout="", stderr=""),
                       Mock(returncode=0, stdout=f"Safe to remove: {path} (branch, PR #1)", stderr=""),
                       Mock(returncode=0, stdout=f"Removed: {path} (branch, PR #1)", stderr="")]
            with patch.object(cleanup, "repository_roots", return_value=[root]), \
                 patch.object(cleanup, "candidates", return_value=[candidate]), \
                 patch.object(cleanup.shutil, "which", return_value="morpheus"), \
                 patch.object(cleanup.subprocess, "run", side_effect=results) as run, \
                 patch.object(cleanup, "load_attempts", return_value={}), \
                 patch.object(cleanup, "save_attempts"), \
                 patch.object(cleanup, "free_gib", return_value=0), \
                 patch.object(__import__("sys"), "argv", ["cleanup-old-worktrees", "--apply"]):
                self.assertEqual(cleanup.main(), 0)
                self.assertEqual(run.call_count, 3)
                self.assertEqual(run.call_args.args[0], ["morpheus", "pm", "cleanup", candidate[1]])


if __name__ == "__main__":
    unittest.main()
