import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "worker/validate_evo_main.py"
spec = importlib.util.spec_from_file_location("validate_evo_main", SCRIPT)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class ValidateEvoMainTests(unittest.TestCase):
    def test_dirty_primary_checkout_is_untouched_and_scratch_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "evo"
            repo.mkdir()
            developer = root / "Xcode.app/Contents/Developer"
            (developer / "usr/bin").mkdir(parents=True)
            (developer / "usr/bin/xcodebuild").write_text("")
            script = repo / "apps/ios/scripts/dev.sh"
            script.parent.mkdir(parents=True)
            script.write_text('#!/bin/sh\ntest "$(cat marker)" = main\n')
            script.chmod(0o755)
            marker = repo / "apps/ios/marker"
            marker.write_text("main\n")

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            git("config", "user.email", "test@example.invalid")
            git("config", "user.name", "Test")
            git("add", "apps/ios")
            git("commit", "-qm", "base")
            git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
            expected = git("rev-parse", "HEAD")
            marker.write_text("dirty\n")
            with patch.dict(os.environ, {"DEVELOPER_DIR": str(developer)}):
                self.assertEqual(validator.validate(repo), expected)
            self.assertEqual(marker.read_text(), "dirty\n")
            self.assertEqual(git("worktree", "list", "--porcelain").count("worktree "), 1)

            script.write_text("#!/bin/sh\nexit 7\n")
            git("add", "apps/ios/scripts/dev.sh")
            git("commit", "-qm", "failing main")
            git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
            with patch.dict(os.environ, {"DEVELOPER_DIR": str(developer)}):
                with self.assertRaises(subprocess.CalledProcessError):
                    validator.validate(repo)
            self.assertEqual(marker.read_text(), "dirty\n")
            self.assertEqual(git("worktree", "list", "--porcelain").count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
