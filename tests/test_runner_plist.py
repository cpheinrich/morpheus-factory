import importlib.util
import plistlib
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "worker/runner_plist.py"
spec = importlib.util.spec_from_file_location("runner_plist", SCRIPT)
runner_plist = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_plist)

RUNNER_DIR = "/Users/worker/actions-runner-mbp"
LOG_DIR = "/Users/worker/Library/Logs"


class RenderTests(unittest.TestCase):
    def render(self, name="mbp", runner_dir=RUNNER_DIR, log_dir=LOG_DIR):
        return runner_plist.render(name, runner_dir, log_dir)

    def test_never_sets_session_create(self):
        self.assertNotIn("SessionCreate", self.render())

    def test_interactive_runsvc_agent(self):
        plist = self.render()
        self.assertEqual(plist["Label"], "com.morpheus-factory.runner.mbp")
        self.assertEqual(plist["ProcessType"], "Interactive")
        self.assertEqual(plist["ProgramArguments"], [f"{RUNNER_DIR}/runsvc.sh"])
        self.assertEqual(plist["WorkingDirectory"], RUNNER_DIR)
        self.assertIs(plist["RunAtLoad"], True)
        self.assertIs(plist["KeepAlive"], True)
        self.assertEqual(
            plist["StandardOutPath"],
            f"{LOG_DIR}/com.morpheus-factory.runner.mbp/stdout.log",
        )
        self.assertEqual(
            plist["StandardErrorPath"],
            f"{LOG_DIR}/com.morpheus-factory.runner.mbp/stderr.log",
        )
        self.assertEqual(plist["EnvironmentVariables"], {"PATH": runner_plist.RUNNER_PATH, "ACTIONS_RUNNER_SVC": "1"})

    def test_requires_absolute_paths(self):
        with self.assertRaises(ValueError):
            self.render(runner_dir="actions-runner-mbp")
        with self.assertRaises(ValueError):
            self.render(log_dir="Library/Logs")
        with self.assertRaises(ValueError):
            self.render(runner_dir="/Users/worker/../root")

    def test_name_length_boundary(self):
        self.assertEqual(self.render(name="a" * 64)["Label"], "com.morpheus-factory.runner." + "a" * 64)
        with self.assertRaises(ValueError):
            self.render(name="a" * 65)

    def test_name_shape_refusals(self):
        for name in ("a..b", ".hidden", "-dash", "", "has space", "slash/name", "evo"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.render(name=name)
        self.assertEqual(self.render(name="Mac_mini-2.ci")["Label"], "com.morpheus-factory.runner.Mac_mini-2.ci")

    def test_round_trips_through_plistlib(self):
        plist = self.render()
        self.assertEqual(plistlib.loads(plistlib.dumps(plist)), plist)

    def test_cli_refusal_is_exit_code_not_traceback(self):
        self.assertEqual(runner_plist.main(["--name", "evo", "--runner-dir", RUNNER_DIR, "--log-dir", LOG_DIR]), 2)


if __name__ == "__main__":
    unittest.main()
