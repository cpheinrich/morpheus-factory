import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "worker/xcode-root-helper.sh"
ASSIGNMENTS = {
    "APPLICATIONS": "/Applications",
    "XCODE_SELECT": "/usr/bin/xcode-select",
    "SPCTL": "/usr/sbin/spctl",
    "RM": "/bin/rm",
}


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class XcodeRootHelperTests(unittest.TestCase):
    """Runs a copy whose root paths point at fakes; the real script is never run as root."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.apps = self.root / "Applications"
        self.apps.mkdir()
        self.log = self.root / "log"
        self.log.write_text("")
        self.selected = self.root / "selected"
        self.selected.write_text("/Library/Developer/CommandLineTools\n")
        self.signed = self.root / "signed"
        self.signed.write_text("yes")
        bin_dir = self.root / "bin"
        executable(bin_dir / "xcode-select", f'''echo "xcode-select $*" >> {self.log}
if [[ "$1" == -p ]]; then [[ -s {self.selected} ]] || exit 1; cat {self.selected}; fi
''')
        executable(bin_dir / "spctl", f'echo "spctl $*" >> {self.log}; [[ "$(cat {self.signed})" == yes ]]\n')
        executable(bin_dir / "rm", f'echo "rm $*" >> {self.log}\n')
        source = SCRIPT.read_text()
        replacements = {"APPLICATIONS": self.apps, "XCODE_SELECT": bin_dir / "xcode-select", "SPCTL": bin_dir / "spctl", "RM": bin_dir / "rm"}
        for name, original in ASSIGNMENTS.items():
            line = f"{name}={original}\n"
            self.assertEqual(source.count(line), 1, line)
            source = source.replace(line, f"{name}={replacements[name]}\n")
        self.helper = self.root / "helper.sh"
        self.helper.write_text(source)

    def app(self, name):
        app = self.apps / name
        executable(app / "Contents/Developer/usr/bin/xcodebuild", f'echo "xcodebuild $*" >> {self.log}\n')
        return app

    def call(self, *argv):
        result = subprocess.run(["/bin/bash", str(self.helper), *map(str, argv)], capture_output=True, text=True,
                                env={"PATH": "/nonexistent"})
        return result.returncode, result.stderr, self.log.read_text().splitlines()

    def test_real_script_uses_absolute_tools_and_fails_closed(self):
        source = SCRIPT.read_text()
        self.assertIn("set -euo pipefail", source)
        self.assertIn("STAT=/usr/bin/stat\n", source)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_argument_count_and_verb(self):
        app = self.app("Xcode_27.2.app")
        for argv in ((), ("select",), ("select", app, "extra"), ("switch", app)):
            self.assertEqual(self.call(*argv)[0], 2, argv)
        self.assertEqual(self.call()[2], [])

    def test_select_switches_a_signed_bundle(self):
        for name in ("Xcode.app", "Xcode_27.2.app", "Xcode_26.3.1.app"):
            app = self.app(name)
            code, _, log = self.call("select", app)
            self.assertEqual(code, 0)
            self.assertEqual(log[-2:], [f"spctl --assess --type execute {app}", f"xcode-select --switch {app}/Contents/Developer"])

    def test_paths_outside_the_patterns_are_refused_before_any_action(self):
        self.app("Xcode_27.2.app")
        (self.root / "elsewhere/Xcode_27.2.app").mkdir(parents=True)
        cases = [
            ("select", self.apps / "Xcode-beta.app"),
            ("select", self.apps / "Xcode_27.app"),
            ("select", f"{self.apps}/Xcode_27.2.app/"),
            ("select", f"{self.apps}/../elsewhere/Xcode_27.2.app"),
            ("select", self.root / "elsewhere/Xcode_27.2.app"),
            ("select", "Xcode_27.2.app"),
            ("first-launch", self.apps / "Xcode.app"),
            ("remove", self.apps / "Xcode.app"),
            ("remove", f"{self.apps}/Xcode_27.2.app -- /etc"),
        ]
        for verb, path in cases:
            code, stderr, log = self.call(verb, path)
            self.assertEqual((code, log), (1, []), (verb, path))
            self.assertTrue(stderr.startswith("morpheus-factory-xcode: "))

    def test_symlink_and_missing_bundles_are_refused(self):
        target = self.app("Xcode_27.2.app")
        (self.apps / "Xcode_27.3.app").symlink_to(target)
        for verb in ("select", "first-launch", "remove"):
            self.assertEqual(self.call(verb, self.apps / "Xcode_27.3.app")[:3:2], (1, []))
            self.assertEqual(self.call(verb, self.apps / "Xcode_28.0.app")[:3:2], (1, []))
        (self.apps / "Xcode_29.0.app").write_text("")
        self.assertEqual(self.call("select", self.apps / "Xcode_29.0.app")[:3:2], (1, []))

    def test_unsigned_bundle_never_runs(self):
        app = self.app("Xcode_27.2.app")
        self.signed.write_text("no")
        for verb in ("select", "first-launch"):
            code, stderr, log = self.call(verb, app)
            self.assertEqual(code, 1)
            self.assertIn("not an Apple-signed app", stderr)
            self.assertFalse(any(line.startswith(("xcode-select", "xcodebuild")) for line in log))

    def test_first_launch_accepts_license_then_runs_first_launch(self):
        app = self.app("Xcode_27.2.app")
        code, _, log = self.call("first-launch", app)
        self.assertEqual(code, 0)
        self.assertEqual(log, [f"spctl --assess --type execute {app}", "xcodebuild -license accept", "xcodebuild -runFirstLaunch"])

    def test_remove_deletes_an_unselected_bundle(self):
        app = self.app("Xcode_26.3.app")
        self.app("Xcode_27.2.app")
        self.selected.write_text(f"{self.apps}/Xcode_27.2.app/Contents/Developer\n")
        code, _, log = self.call("remove", app)
        self.assertEqual(code, 0)
        self.assertEqual(log[-1], f"rm -rf -- {app}")

    def test_remove_refuses_the_selected_bundle(self):
        app = self.app("Xcode_27.2.app")
        self.selected.write_text(f"{app}/Contents/Developer\n")
        code, stderr, log = self.call("remove", app)
        self.assertEqual(code, 1)
        self.assertIn("selected Xcode", stderr)
        self.assertFalse(any(line.startswith("rm") for line in log))

    def test_remove_refuses_the_xcode_symlink_target(self):
        app = self.app("Xcode_26.3.app")
        (self.apps / "Xcode.app").symlink_to(app)
        code, stderr, log = self.call("remove", app)
        self.assertEqual(code, 1)
        self.assertIn("target of", stderr)
        self.assertFalse(any(line.startswith("rm") for line in log))

    def test_remove_refuses_when_selection_is_unreadable(self):
        app = self.app("Xcode_26.3.app")
        self.selected.write_text("")
        code, stderr, log = self.call("remove", app)
        self.assertEqual(code, 1)
        self.assertIn("cannot read the selected Xcode", stderr)
        self.assertFalse(any(line.startswith("rm") for line in log))


if __name__ == "__main__":
    unittest.main()
