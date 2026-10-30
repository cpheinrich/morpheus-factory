import importlib.util
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "worker/install_updates.py"
spec = importlib.util.spec_from_file_location("install_updates", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallUpdatesTests(unittest.TestCase):
    def test_host_github_account_reaches_plist_and_bootstrap_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "host-credentials"
            project.mkdir()
            commands = []
            bootstrap_attempts = 0

            def command(argv, **kwargs):
                nonlocal bootstrap_attempts
                commands.append(argv)
                if argv[:2] == ["launchctl", "bootstrap"]:
                    bootstrap_attempts += 1
                    return SimpleNamespace(returncode=0 if bootstrap_attempts == 3 else 5)
                return SimpleNamespace(returncode=0)

            argv = ["install_updates.py", "--host", "zoe-mac-mini", "--credentials-project", str(project), "--github-user", "zoeagent"]
            with patch.object(sys, "argv", argv), patch.object(installer.Path, "home", return_value=home), \
                 patch.object(installer.subprocess, "run", side_effect=command), patch.object(installer.time, "sleep") as sleep:
                installer.main()
            plist = plistlib.loads((home / "Library/LaunchAgents/com.morpheus-factory.host-updates.plist").read_bytes())
            args = plist["ProgramArguments"]
            self.assertEqual(args[args.index("--github-user") + 1], "zoeagent")
            self.assertEqual(args[args.index("--credentials-project") + 1], str(project.resolve()))
            self.assertEqual(bootstrap_attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertTrue(any(command[:2] == ["plutil", "-lint"] for command in commands))


if __name__ == "__main__":
    unittest.main()
