import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "worker/xcode-root-helper.sh"
ASSIGNMENTS = {
    "APPLICATIONS": "/Applications",
    "QUARANTINE": "/var/root/morpheus-factory-xcode",
    "XCODE_SELECT": "/usr/bin/xcode-select",
    "SPCTL": "/usr/sbin/spctl",
    "CODESIGN": "/usr/bin/codesign",
    "RM": "/bin/rm",
    "MV": "/bin/mv",
    "CP": "/bin/cp",
    "CHOWN": "/usr/sbin/chown",
    "CHMOD": "/bin/chmod",
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
        # A valid signature alone is not enough: the fake only accepts Apple's Xcode requirement.
        executable(bin_dir / "codesign", f'echo "codesign $*" >> {self.log}; [[ "$*" == *"-R=identifier \\"com.apple.dt.Xcode\\" and anchor apple"* && "$(cat {self.signed})" == yes ]]\n')
        executable(bin_dir / "mv", f'echo "mv $*" >> {self.log}; /bin/mv "$@"\n')
        executable(bin_dir / "cp", f'echo "cp $*" >> {self.log}; /bin/cp "$@"\n')
        # Fakes for root-only steps: record, and make the tree read-only the way
        # a root:wheel go-w tree is to the worker user.
        executable(bin_dir / "chown", f'echo "chown $*" >> {self.log}\n')
        executable(bin_dir / "chmod", f'echo "chmod $*" >> {self.log}; /usr/bin/find "${{@: -1}}" -type f -exec /bin/chmod u-w {{}} +\n')
        self.quarantine = self.root / "var-root/morpheus-factory-xcode"
        self.quarantine.parent.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.addCleanup(lambda: subprocess.run(["/bin/chmod", "-R", "u+w", str(self.root)], check=False, capture_output=True))
        source = SCRIPT.read_text()
        replacements = {"APPLICATIONS": self.apps, "QUARANTINE": self.quarantine, "XCODE_SELECT": bin_dir / "xcode-select",
                        "SPCTL": bin_dir / "spctl", "CODESIGN": bin_dir / "codesign", "RM": bin_dir / "rm", "MV": bin_dir / "mv", "CP": bin_dir / "cp",
                        "CHOWN": bin_dir / "chown", "CHMOD": bin_dir / "chmod"}
        for name, original in ASSIGNMENTS.items():
            line = f"{name}={original}\n"
            self.assertEqual(source.count(line), 1, line)
            source = source.replace(line, f"{name}={replacements[name]}\n")
        self.helper = self.root / "helper.sh"
        self.helper.write_text(source)

    def app(self, name, parent=None):
        app = (parent or self.apps) / name
        executable(app / "Contents/Developer/usr/bin/xcodebuild", f'echo "xcodebuild $*" >> {self.log}\n')
        return app

    def expanded(self, version_line="Xcode 27.2"):
        """An expanded download whose xcodebuild tries to swap in a payload once it is running as root."""
        app = self.work / "xcode-27.2-abc/Xcode.app"
        evil = self.root / "evil-ran"
        executable(app / "Contents/Developer/usr/bin/xcodebuild", f'''echo "xcodebuild $0 $*" >> {self.log}
echo "home $HOME" >> {self.log}
if [[ "$1" == -version ]]; then printf '%s\\nBuild version 27B52\\n' "{version_line}"; exit 0; fi
# Race attempts: rewrite the original path and this binary.
mkdir -p "{app}/Contents/Developer/usr/bin" 2>/dev/null
printf '#!/bin/bash\\ntouch {evil}\\n' > "{app}/Contents/Developer/usr/bin/xcodebuild" 2>/dev/null && chmod +x "{app}/Contents/Developer/usr/bin/xcodebuild"
printf '#!/bin/bash\\ntouch {evil}\\n' > "$0" 2>/dev/null || echo "rewrite-refused" >> {self.log}
exit 0
''')
        return app, evil

    def call(self, *argv):
        result = subprocess.run(["/bin/bash", str(self.helper), *map(str, argv)], capture_output=True, text=True,
                                env={"PATH": "/nonexistent"})
        return result.returncode, result.stderr, self.log.read_text().splitlines()

    def test_real_script_uses_absolute_tools_and_fails_closed(self):
        source = SCRIPT.read_text()
        self.assertIn("set -euo pipefail", source)
        self.assertIn("STAT=/usr/bin/stat\n", source)
        self.assertIn("MKDIR=/bin/mkdir\n", source)
        self.assertNotIn("first-launch", source)
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
        for verb in ("select", "remove"):
            self.assertEqual(self.call(verb, self.apps / "Xcode_27.3.app")[:3:2], (1, []))
            self.assertEqual(self.call(verb, self.apps / "Xcode_28.0.app")[:3:2], (1, []))
        (self.apps / "Xcode_29.0.app").write_text("")
        self.assertEqual(self.call("select", self.apps / "Xcode_29.0.app")[:3:2], (1, []))

    def test_unsigned_bundle_never_runs(self):
        app = self.app("Xcode_27.2.app")
        self.signed.write_text("no")
        code, stderr, log = self.call("select", app)
        self.assertEqual(code, 1)
        self.assertIn("not an Apple-signed app", stderr)
        self.assertFalse(any(line.startswith(("xcode-select", "xcodebuild")) for line in log))

    def test_install_quarantines_before_verifying_or_executing(self):
        source, evil = self.expanded()
        code, stderr, log = self.call("install", source, "27.2")
        self.assertEqual(code, 0, stderr)
        quarantined = self.quarantine / "Xcode_27.2.app"
        binary = quarantined / "Contents/Developer/usr/bin/xcodebuild"
        self.assertEqual(log[:6], [f"cp -Rc {source} {quarantined}", f"chown -R root:wheel {quarantined}", f"chmod -R go-w {quarantined}",
                                   f"chmod -R -N {quarantined}",
                                   f'codesign --verify --deep --strict -R=identifier "com.apple.dt.Xcode" and anchor apple {quarantined}',
                                   f"spctl --assess --type execute {quarantined}"])
        executed = [line for line in log if line.startswith("xcodebuild ")]
        self.assertEqual(executed, [f"xcodebuild {binary} -version", f"xcodebuild {binary} -license accept",
                                    f"xcodebuild {binary} -runFirstLaunch"])
        self.assertIn("rewrite-refused", log)
        self.assertEqual(log[-1], f"mv -h {quarantined} {self.apps}/Xcode_27.2.app")
        self.assertFalse(evil.exists())
        self.assertTrue((self.apps / "Xcode_27.2.app/Contents/Developer/usr/bin/xcodebuild").is_file())
        # Root never deletes the user-controlled source; the controller's temporary directory does.
        self.assertTrue(source.is_dir())
        self.assertFalse(any(line.startswith(("rm ", "mv " + str(source))) for line in log))

    def test_open_handle_on_the_source_cannot_reach_the_installed_copy(self):
        source, _ = self.expanded()
        binary = source / "Contents/Developer/usr/bin/xcodebuild"
        with open(binary, "r+b") as handle:
            code, stderr, _ = self.call("install", source, "27.2")
            self.assertEqual(code, 0, stderr)
            handle.seek(0)
            handle.write(b"#!/bin/bash\ntouch /tmp/never\n")
            handle.flush()
        installed = self.apps / "Xcode_27.2.app/Contents/Developer/usr/bin/xcodebuild"
        self.assertNotIn(b"/tmp/never", installed.read_bytes())

    def test_install_refuses_a_clone_that_is_not_a_real_directory(self):
        source, _ = self.expanded()
        bin_dir = self.root / "bin"
        executable(bin_dir / "cp", f'echo "cp $*" >> {self.log}; /bin/ln -s {self.work} "${{@: -1}}"\n')
        code, stderr, log = self.call("install", source, "27.2")
        self.assertEqual(code, 1)
        self.assertIn("is not a directory", stderr)
        self.assertFalse(any(line.startswith(("chown", "codesign", "xcodebuild")) for line in log))

    def test_install_validates_arguments_before_moving(self):
        source, _ = self.expanded()
        (self.work / "Other.app").mkdir()
        (self.work / "link").symlink_to(self.apps)
        self.app("Xcode.app")
        cases = [
            ("install", source),
            ("install", source, "27.2", "extra"),
        ]
        for argv in cases:
            self.assertEqual(self.call(*argv)[0], 2, argv)
        refused = [
            (source, "latest"),
            (source, "27"),
            ("work/xcode-27.2-abc/Xcode.app", "27.2"),
            (self.work / "Other.app", "27.2"),
            (self.apps / "Xcode.app", "27.2"),
            (f"{self.work}/../Applications/Xcode.app", "27.2"),
            (self.work / "link/Xcode.app", "27.2"),
            (self.work / "missing/Xcode.app", "27.2"),
        ]
        for src, version in refused:
            code, stderr, log = self.call("install", src, version)
            self.assertEqual((code, log), (1, []), (src, version, stderr))
        self.assertTrue(source.is_dir())
        self.assertTrue((self.apps / "Xcode.app").is_dir())

    def test_install_refuses_existing_destinations(self):
        source, _ = self.expanded()
        self.app("Xcode_27.2.app")
        self.assertEqual(self.call("install", source, "27.2")[:3:2], (1, []))
        (self.apps / "Xcode_27.2.app/Contents/Developer/usr/bin/xcodebuild").unlink()
        subprocess.run(["/bin/rm", "-rf", str(self.apps / "Xcode_27.2.app")], check=True)
        (self.quarantine / "Xcode_27.2.app").mkdir(parents=True)
        code, stderr, log = self.call("install", source, "27.2")
        self.assertEqual((code, log), (1, []))
        self.assertIn("sudo rm -rf", stderr)

    def test_unsigned_or_wrong_version_stays_in_quarantine_and_never_runs(self):
        for setup in ("unsigned", "version"):
            with self.subTest(setup):
                self.setUp()
                source, evil = self.expanded("Xcode 27.1" if setup == "version" else "Xcode 27.2")
                if setup == "unsigned":
                    self.signed.write_text("no")
                code, stderr, log = self.call("install", source, "27.2")
                quarantined = self.quarantine / "Xcode_27.2.app"
                self.assertEqual(code, 1)
                self.assertIn(f"sudo rm -rf {quarantined}", stderr)
                self.assertTrue(quarantined.is_dir())
                self.assertFalse((self.apps / "Xcode_27.2.app").exists())
                self.assertFalse(any("-license" in line or "-runFirstLaunch" in line for line in log))
                if setup == "unsigned":
                    self.assertFalse(any(line.startswith("xcodebuild") for line in log))
                self.assertFalse(evil.exists())

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

    def test_root_runs_xcodebuild_with_a_clean_environment(self):
        app, _ = self.expanded()
        code, _, log = self.call("install", app, "27.2")
        self.assertEqual(code, 0)
        self.assertEqual([line for line in log if line.startswith("home ")], ["home /var/root"] * 3)

    def test_codesign_must_be_asked_for_apples_requirement(self):
        source = SCRIPT.read_text()
        self.assertIn("""REQUIREMENT='identifier "com.apple.dt.Xcode" and anchor apple'""", source)
        self.assertIn('--verify --deep --strict -R="$REQUIREMENT"', source)
        app, _ = self.expanded()
        # Drop the requirement from the fake's acceptance: the script must still pass it.
        code, _, log = self.call("install", app, "27.2")
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("codesign --verify --deep --strict -R=identifier") for line in log))

    def test_target_replaced_during_install_is_refused_after_the_move(self):
        app, evil = self.expanded()
        # Simulate the race: a directory appears at the target just before the final move.
        executable(self.root / "bin/mv", f'echo "mv $*" >> {self.log}; /bin/mkdir -p "${{@: -1}}"; /bin/mv "$@"\n')
        code, stderr, log = self.call("install", app, "27.2")
        self.assertEqual(code, 1)
        self.assertIn("was replaced during install", stderr)
        self.assertTrue((self.apps / "Xcode_27.2.app/Xcode_27.2.app/Contents").is_dir())
        self.assertFalse(evil.exists())


if __name__ == "__main__":
    unittest.main()
