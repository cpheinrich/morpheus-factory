import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "worker/update_controller.py"
spec = importlib.util.spec_from_file_location("update_controller", SCRIPT)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class UpdateControllerTests(unittest.TestCase):
    def manifest(self, xcode=None, validation=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "manifest.json"
        path.write_text(json.dumps({"schemaVersion": 1, "hosts": {"mini": {
            "xcode": xcode or {"version": "27.2", "build": "27B52", "phase": "stage"},
            "validation": validation or [{"argv": ["/usr/bin/true"]}],
        }}}))
        return path

    def test_pin_requires_exact_identity_and_absolute_validation(self):
        pin = controller.pin_for_host(self.manifest(), "mini")
        self.assertEqual((pin.version, pin.build, pin.phase), ("27.2", "27B52", "stage"))
        self.assertEqual(controller.pin_for_host(self.manifest(xcode={"version": "27.2", "build": "27B52", "phase": "promote"}), "mini").phase, "promote")
        # An absent phase means promote; a hold is always explicit.
        self.assertEqual(controller.pin_for_host(self.manifest(xcode={"version": "27.2", "build": "27B52"}), "mini").phase, "promote")
        with self.assertRaisesRegex(controller.UpdateError, "exact numeric"):
            controller.pin_for_host(self.manifest(xcode={"version": "latest", "build": "27B52"}), "mini")
        with self.assertRaisesRegex(controller.UpdateError, "absolute path"):
            controller.pin_for_host(self.manifest(validation=[{"argv": ["sh", "-c", "true"]}]), "mini")
        with self.assertRaisesRegex(controller.UpdateError, "components must be named"):
            controller.pin_for_host(self.manifest(xcode={"version": "27.2", "build": "27B52", "components": ["../../metal"]}), "mini")

    def test_xcodes_gets_only_apple_credentials(self):
        zoe = {"idVar": "ZOE_APPLE_ID", "passwordVar": "ZOE_APPLE_ID_PASSWORD"}
        with patch.dict(os.environ, {"ZOE_APPLE_ID": "user@example.test", "ZOE_APPLE_ID_PASSWORD": "secret", "ASC_ISSUER_ID": "must-not-pass"}):
            env = controller.xcodes_environment(zoe)
        self.assertEqual(env["XCODES_USERNAME"], "user@example.test")
        self.assertEqual(env["XCODES_PASSWORD"], "secret")
        self.assertNotIn("ASC_ISSUER_ID", env)
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", env)

    def test_ordinary_children_do_not_inherit_any_credentials(self):
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""

        with patch.dict(os.environ, {"ZOE_APPLE_ID_PASSWORD": "secret", "ASC_ISSUER_ID": "another-secret"}), \
             patch.object(controller.subprocess, "run", return_value=Result()) as child:
            self.assertEqual(controller.run(["/usr/bin/true"]), "ok")
        passed = child.call_args.kwargs["env"]
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", passed)
        self.assertNotIn("ASC_ISSUER_ID", passed)

    def test_interactive_login_keeps_other_credentials_out_of_xcodes(self):
        class Result:
            returncode = 0

        with patch.dict(os.environ, {"ZOE_APPLE_ID": "user@example.test", "ZOE_APPLE_ID_PASSWORD": "secret", "ASC_ISSUER_ID": "other-secret"}), \
             patch.object(controller.shutil, "which", return_value="/opt/homebrew/bin/xcodes"), \
             patch.object(controller.subprocess, "run", return_value=Result()) as child:
            self.assertEqual(controller.interactive_login({"idVar": "ZOE_APPLE_ID", "passwordVar": "ZOE_APPLE_ID_PASSWORD"}), "apple-authenticated")
        self.assertNotIn("ASC_ISSUER_ID", child.call_args.kwargs["env"])

    def test_stage_phase_never_validates_or_switches(self):
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer"}), \
             patch.object(controller, "stage", return_value="staged") as stage, \
             patch.object(controller, "validate") as validate, \
             patch.object(controller, "activate") as activate:
            result = controller.reconcile("27.2", "27B52", [], "stage", Path("/tmp/state"))
        self.assertEqual(result, "staged")
        stage.assert_called_once()
        validate.assert_not_called()
        activate.assert_not_called()

    def test_already_installed_xcode_still_repairs_declared_components(self):
        app = Path("/Applications/Xcode_27.2.app")
        with patch.object(controller, "matching_app", return_value=app), \
             patch.object(controller, "ensure_components") as ensure:
            result = controller.stage("27.2", "27B52", Path("/tmp/state"), components=["MetalToolchain"])
        self.assertEqual(result, "already-installed")
        ensure.assert_called_once_with(app, ["MetalToolchain"])

    def test_promotion_validates_before_switch(self):
        calls = []
        with patch.object(controller, "host_is_busy", return_value=False), patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage", side_effect=lambda *a: calls.append("stage")), \
             patch.object(controller, "validate", side_effect=lambda *a: calls.append("validate")), \
             patch.object(controller, "activate", side_effect=lambda *a: calls.append("activate")):
            result = controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertEqual(result, "promoted")
        self.assertEqual(calls, ["stage", "validate", "activate"])

    def test_already_selected_pin_does_not_rebuild_each_hour(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        with patch.object(controller, "report", return_value={"selectedMatches": True}), \
             patch.object(controller, "stage") as stage, \
             patch.object(controller, "validate") as validate, \
             patch.object(controller, "activate") as activate:
            result = controller.reconcile("27.2", "27B52", [], "promote", state)
            first = controller.promotion(state, "27.2", "27B52")
            controller.reconcile("27.2", "27B52", [], "promote", state)
        self.assertEqual(result, "already-current")
        self.assertEqual(stage.call_count, 2)
        validate.assert_not_called()
        activate.assert_not_called()
        # A host selected before promotion records existed starts its window when
        # first seen, with no known previous app; later ticks keep that time.
        self.assertIsNone(first["previous"])
        self.assertEqual(controller.promotion(state, "27.2", "27B52"), first)

    def test_failed_validation_does_not_switch(self):
        with patch.object(controller, "host_is_busy", return_value=False), patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage"), \
             patch.object(controller, "validate", side_effect=controller.UpdateError("failed")), \
             patch.object(controller, "activate") as activate:
            with self.assertRaises(controller.UpdateError):
                controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        activate.assert_not_called()

    def test_failed_post_switch_verification_restores_previous_selection(self):
        previous = "/Applications/Xcode.app/Contents/Developer"
        selected = "/Applications/Xcode_27.2.app/Contents/Developer"
        def command(argv, **kwargs):
            if argv == ["/usr/bin/xcode-select", "-p"]:
                return selected
            if argv == ["/usr/bin/sudo", "-n", controller.HELPER_PATH, "select", "/Applications/Xcode.app"]:
                return ""
            raise AssertionError(argv)
        with patch.object(controller, "report", return_value={"selected": previous, "selectedMatches": False}), \
             patch.object(controller, "stage"), patch.object(controller, "validate"), patch.object(controller, "host_is_busy", return_value=False), \
             patch.object(controller, "activate", side_effect=controller.UpdateError("post-switch check failed")), \
             patch.object(controller, "run", side_effect=command) as run:
            with self.assertRaisesRegex(controller.UpdateError, "post-switch check failed"):
                controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertIn(["/usr/bin/sudo", "-n", controller.HELPER_PATH, "select", "/Applications/Xcode.app"], [c.args[0] for c in run.call_args_list])

    def test_rollback_refuses_a_previous_selection_outside_an_app(self):
        def command(argv, **kwargs):
            if argv == ["/usr/bin/xcode-select", "-p"]:
                return "/Applications/Xcode_27.2.app/Contents/Developer"
            raise AssertionError(argv)
        with patch.object(controller, "report", return_value={"selected": "/Library/Developer/CommandLineTools", "selectedMatches": False}), \
             patch.object(controller, "stage"), patch.object(controller, "validate"), patch.object(controller, "host_is_busy", return_value=False), \
             patch.object(controller, "activate", side_effect=controller.UpdateError("post-switch check failed")), \
             patch.object(controller, "run", side_effect=command):
            with self.assertRaisesRegex(controller.UpdateError, "cannot restore /Library/Developer/CommandLineTools"):
                controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))

    def test_busy_host_defers_before_the_validation_build(self):
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage"), patch.object(controller, "validate") as validate, \
             patch.object(controller, "activate") as activate, \
             patch.object(controller, "run", return_value="/sbin/launchd\n/Users/chris/actions-runner/bin/Runner.Worker\n"):
            result = controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertEqual(result, "deferred-busy")
        validate.assert_not_called()
        activate.assert_not_called()

    def test_stage_installs_through_the_helper_and_never_moves_itself(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        applications = root / "Applications"
        applications.mkdir()
        commands = []

        def command(argv, **kwargs):
            commands.append(argv)
            if argv[0] == "/usr/bin/xip":
                (kwargs["cwd"] / "Xcode.app").mkdir()
            if argv[:4] == ["/usr/bin/sudo", "-n", controller.HELPER_PATH, "install"]:
                self.assertTrue(Path(argv[4]).is_dir())
                (applications / "Xcode_27.2.app").mkdir()
            return ""

        def download(argv, verification, apple):
            (Path(argv[-1]) / "Xcode_27.2.xip").write_text("")

        identity = lambda app: ("27.2", "27B52") if app.exists() else None
        with patch.object(controller, "APPLICATIONS", applications), patch.object(controller, "matching_app", return_value=None), \
             patch.object(controller, "installed_build", side_effect=identity), patch.object(controller, "run", side_effect=command), \
             patch.object(controller, "run_xcodes", side_effect=download), patch.object(controller, "ensure_components") as components, \
             patch.object(controller.shutil, "which", return_value="/opt/homebrew/bin/xcodes"), \
             patch.object(controller.shutil, "disk_usage", return_value=SimpleNamespace(free=80 * 1024**3)):
            self.assertEqual(controller.stage("27.2", "27B52", root / "state", components=["MetalToolchain"]), "staged")
        helper = [argv for argv in commands if argv[0] == "/usr/bin/sudo"]
        self.assertEqual(len(helper), 1)
        self.assertEqual(helper[0][3:], ["install", helper[0][4], "27.2"])
        self.assertEqual(Path(helper[0][4]).name, "Xcode.app")
        self.assertTrue(Path(helper[0][4]).is_relative_to(root / "state"))
        self.assertFalse(any(argv[0] == "/bin/mv" for argv in commands))
        components.assert_called_once_with(applications / "Xcode_27.2.app", ["MetalToolchain"])
        # The helper clones and leaves the source; the temporary directory removes it.
        self.assertFalse(Path(helper[0][4]).exists())

    def test_stage_refuses_when_helper_install_leaves_wrong_identity(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)

        def command(argv, **kwargs):
            if argv[0] == "/usr/bin/xip":
                (kwargs["cwd"] / "Xcode.app").mkdir()
            return ""

        def identity(app):
            return ("27.2", "27B52") if app.name == "Xcode.app" else None

        with patch.object(controller, "APPLICATIONS", root), patch.object(controller, "matching_app", return_value=None), \
             patch.object(controller, "installed_build", side_effect=identity), patch.object(controller, "run", side_effect=command), \
             patch.object(controller, "run_xcodes", side_effect=lambda argv, *a: (Path(argv[-1]) / "x.xip").write_text("")), \
             patch.object(controller, "ensure_components") as components, \
             patch.object(controller.shutil, "which", return_value="/opt/homebrew/bin/xcodes"), \
             patch.object(controller.shutil, "disk_usage", return_value=SimpleNamespace(free=80 * 1024**3)):
            with self.assertRaisesRegex(controller.UpdateError, "did not retain the approved identity"):
                controller.stage("27.2", "27B52", root / "state")
        components.assert_not_called()

    def test_only_helper_invocations_use_sudo(self):
        lines = [line for line in SCRIPT.read_text().splitlines() if '"/usr/bin/sudo"' in line]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("HELPER_PATH" in line for line in lines))

    def test_ci_image_requires_exact_guest_proof_and_caches_only_unchanged_image(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        ci = {"image": "ci-xcode-27.2", "label": "xcode-27.2", "repository": "darwin-health/evo",
              "tart": "/usr/bin/true", "builder": "/usr/bin/true", "verifier": "/usr/bin/false", "activator": "/usr/bin/printf"}
        identity = {"disk.img": {"inode": 1}}
        with patch.object(controller, "stage") as stage, patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "image_is_sealed", return_value=True), \
             patch.object(controller, "image_identity", side_effect=lambda image: identity.copy()), \
             patch.object(controller, "run", return_value="") as run:
            self.assertEqual(controller.stage_ci_image("27.2", "27B52", state, None, [], ci), "verified-and-activated")
            self.assertTrue(any(call.args[0][0] == ci["verifier"] for call in run.call_args_list))
            receipt = state / "ci-images/ci-xcode-27.2.json"
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            run.reset_mock()
            self.assertEqual(controller.stage_ci_image("27.2", "27B52", state, None, [], ci), "already-verified")
            stage.assert_not_called()
            self.assertFalse(any(call.args[0][0] == ci["verifier"] for call in run.call_args_list))
            identity["disk.img"] = {"inode": 2}
            run.reset_mock()
            self.assertEqual(controller.stage_ci_image("27.2", "27B52", state, None, [], ci), "verified-and-activated")
            self.assertTrue(any(call.args[0][0] == ci["verifier"] for call in run.call_args_list))

    def test_ci_tools_receive_github_auth_without_apple_or_sms_secrets(self):
        with patch.dict(os.environ, {
            "GH_TOKEN": "github-secret",
            "FACTORY_GH_TOKEN": "github-secret",
            "GIT_ASKPASS": "/tmp/askpass",
            "ZOE_APPLE_ID_PASSWORD": "apple-secret",
            "FACTORY_VERIFICATION_CODE_TOKEN": "sms-secret",
        }):
            passed = controller.ci_environment()
        self.assertEqual(passed["GH_TOKEN"], "github-secret")
        self.assertEqual(passed["GIT_ASKPASS"], "/tmp/askpass")
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", passed)
        self.assertNotIn("FACTORY_VERIFICATION_CODE_TOKEN", passed)

    def test_provider_receives_scoped_environment_and_no_code_is_logged(self):
        class Result:
            returncode = 0
            stdout = '{"code":"123456"}'
            stderr = ""

        verification = {"purpose": "apple-developer", "account": "zoe-apple-developer"}
        with patch.dict(os.environ, {
            "FACTORY_VERIFICATION_CODE_COMMAND": "/usr/bin/true",
            "FACTORY_VERIFICATION_CODE_URL": "https://broker.example.test",
            "FACTORY_VERIFICATION_CODE_TOKEN": "broker-secret",
            "ZOE_APPLE_ID_PASSWORD": "apple-secret",
            "ASC_ISSUER_ID": "other-secret",
        }), patch.object(controller.subprocess, "run", return_value=Result()) as child:
            self.assertEqual(controller.request_verification_code(verification, 100.0, 120, 6), "123456")
        sent = json.loads(child.call_args.kwargs["input"])
        self.assertEqual(sent, {"purpose": "apple-developer", "account": "zoe-apple-developer", "challengeStartedAt": 100.0, "timeoutSeconds": 120})
        passed = child.call_args.kwargs["env"]
        self.assertEqual(passed["FACTORY_VERIFICATION_CODE_TOKEN"], "broker-secret")
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", passed)
        self.assertNotIn("ASC_ISSUER_ID", passed)

    def test_late_code_remains_eligible_after_sms_resend(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        script = Path(temporary.name) / "fake_xcodes.py"
        sends = Path(temporary.name) / "sends"
        script.write_text('''
import pathlib, sys
print('Two-factor authentication is enabled for this account.')
print('Enter "sms" without quotes to exit this prompt and choose a phone number to send an SMS security code to.')
print('Enter the 6 digit code from one of your trusted devices:', end='', flush=True)
assert sys.stdin.readline().strip() == 'sms'
print('Trusted phone numbers:')
print('1: +1 (masked)')
print('Select a trusted phone number to receive a code via SMS:', end='', flush=True)
assert sys.stdin.readline().strip() == '1'
pathlib.Path(sys.argv[1]).open('a').write('sent\\n')
print('Enter the 6 digit code sent to +1 (masked):', end='', flush=True)
assert sys.stdin.readline().strip() == '123456'
print('authenticated')
''')
        times = []

        def provider(_verification, started_at, _timeout, _length):
            times.append(started_at)
            if len(times) == 1:
                raise controller.CodeTimeout("no SMS yet")
            return "123456"

        with patch.dict(os.environ, {"APPLE_ID": "user@example.test", "APPLE_ID_PASSWORD": "secret", "FACTORY_VERIFICATION_CODE_COMMAND": "/usr/bin/true"}), \
             patch.object(controller, "request_verification_code", side_effect=provider):
            result = controller.run_xcodes([sys.executable, str(script), str(sends)], {"purpose": "apple-developer", "account": "zoe-apple-developer"})
        self.assertEqual(result, "xcodes-complete")
        self.assertEqual(sends.read_text().splitlines(), ["sent", "sent"])
        self.assertEqual(times[0], times[1])

    def test_three_missing_sms_codes_stop_without_unbounded_resends(self):
        start = 100.0
        with patch.dict(os.environ, {"FACTORY_VERIFICATION_CODE_COMMAND": "/usr/bin/true"}), \
             patch.object(controller, "_xcodes_interactive_attempt", side_effect=[controller.CodeTimeout("missing", start)] * 3) as attempts:
            with self.assertRaisesRegex(controller.UpdateError, "after three SMS sends"):
                controller.run_xcodes(["xcodes", "list"], {"purpose": "apple-developer", "account": "zoe-apple-developer"})
        self.assertEqual(attempts.call_count, 3)
        self.assertEqual(attempts.call_args_list[1].args[-1], start)



FINGERPRINT_A = "A" * 40
FINGERPRINT_B = "B" * 40
FINGERPRINT_C = "C" * 40
UDID = "00008120-00122CC80233401E"


def device_record(udid, tunnel, transport, name="Zoe\u2019s iPhone", pairing="paired"):
    """The shape `devicectl list devices --json-output` writes, as seen on the Pro."""
    return {"hardwareProperties": {"udid": udid}, "identifier": "68087738-05DB-5D1F-A350-1E576FBCC306",
            "connectionProperties": {"tunnelState": tunnel, "pairingState": pairing, "transportType": transport},
            "deviceProperties": {"name": name}}


class XcodesErrorTests(unittest.TestCase):
    APPLE_503 = "Error: NetworkError.non200StatusCode(statusCode: 503, data: <secret-ish body>)"

    def test_run_names_an_apple_503_without_echoing_output(self):
        result = SimpleNamespace(returncode=1, stdout="", stderr=self.APPLE_503)
        with patch.object(controller.subprocess, "run", return_value=result):
            with self.assertRaises(controller.UpdateError) as caught:
                controller.run(["xcodes", "download", "27.2"], env={})
        self.assertEqual(str(caught.exception), "xcodes: apple-unavailable: Apple returned an HTTP error; retry later")

    def test_interactive_attempt_categorises_its_captured_output(self):
        # A real child on a real pty, standing in for xcodes; nothing is sent to Apple.
        script = f"import sys; print({self.APPLE_503!r}); sys.exit(1)"
        with patch.object(controller, "xcodes_environment", return_value={"PATH": os.environ.get("PATH", "")}):
            with self.assertRaises(controller.UpdateError) as caught:
                controller._xcodes_interactive_attempt([sys.executable, "-c", script], {"purpose": "p", "account": "a"}, 1, None)
        self.assertEqual(str(caught.exception), "xcodes: apple-unavailable: Apple returned an HTTP error; retry later")
        self.assertNotIn("secret-ish", str(caught.exception))


FAKE_XCODES = r"""
import sys, termios, time
def hidden_readline(prompt):
    # readpassphrase(3): print the prompt, then TCSAFLUSH the terminal (dropping queued input), then read.
    sys.stdout.write(prompt); sys.stdout.flush()
    time.sleep(0.3)
    attrs = termios.tcgetattr(0); attrs[3] &= ~termios.ECHO
    termios.tcsetattr(0, termios.TCSAFLUSH, attrs)
    line = sys.stdin.readline().strip()
    attrs[3] |= termios.ECHO; termios.tcsetattr(0, termios.TCSANOW, attrs)
    return line
print("Two-factor authentication is enabled for this account.\n")
print('Enter "sms" without quotes to exit this prompt and choose a phone number to send an SMS security code to.')
if hidden_readline("Enter the 6 digit code from one of your trusted devices:") != "sms": sys.exit(3)
print("Select a trusted phone number to receive a code via SMS:"); print("1: +1 (•••) •••-••12"); sys.stdout.flush()
if sys.stdin.readline().strip() != "1": sys.exit(4)
if hidden_readline("Enter the 6 digit code sent to +1 (•••) •••-••12:") != "123456": sys.exit(5)
print("downloaded"); sys.exit(0)
"""


class HiddenPromptTests(unittest.TestCase):
    """xcodes reads 2FA answers with readpassphrase, whose TCSAFLUSH drops input queued before it runs."""

    def test_full_sms_flow_against_a_readpassphrase_like_child(self):
        with patch.object(controller, "xcodes_environment", return_value={"PATH": os.environ.get("PATH", "")}), \
             patch.object(controller, "request_verification_code", return_value="123456"), \
             patch.dict(os.environ, {"FACTORY_VERIFICATION_CODE_COMMAND": "/usr/bin/true"}):
            result, started = controller._xcodes_interactive_attempt([sys.executable, "-c", FAKE_XCODES], {"purpose": "p", "account": "a"}, 5, None)
        self.assertEqual(result, "xcodes-complete")
        self.assertIsNotNone(started)

    def test_immediate_write_is_lost_but_waiting_for_echo_off_is_not(self):
        import pty as _pty, termios as _termios
        child = r"""
import sys, termios, time
sys.stdout.write("prompt:"); sys.stdout.flush(); time.sleep(0.3)
a = termios.tcgetattr(0); a[3] &= ~termios.ECHO; termios.tcsetattr(0, termios.TCSAFLUSH, a)
sys.stdout.write("got=" + sys.stdin.readline().strip() + "\n"); sys.stdout.flush()
"""
        def run(answer):
            master, slave = _pty.openpty()
            process = controller.subprocess.Popen([sys.executable, "-c", child], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            out = b""
            deadline = controller.time.monotonic() + 5
            answered = False
            while controller.time.monotonic() < deadline:
                ready, _, _ = controller.select.select([master], [], [], 0.1)
                if ready:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break
                    out += chunk
                    if b"prompt:" in out and not answered:
                        answer(master)
                        answered = True
                if b"got=" in out or (process.poll() is not None and not ready):
                    break
            process.kill(); process.wait(); os.close(master)
            return out
        lost = run(lambda master: os.write(master, b"sms\n"))
        self.assertNotIn(b"got=sms", lost, "the immediate write should have been flushed by TCSAFLUSH")
        kept = run(lambda master: controller._answer_hidden_prompt(master, b"sms\n"))
        self.assertIn(b"got=sms", kept)


class ManifestFieldTests(unittest.TestCase):
    def host(self, **fields):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "manifest.json"
        entry = {"xcode": {"version": "27.2", "build": "27B52"}, "validation": [{"argv": ["/usr/bin/true"]}]}
        xcode = fields.pop("xcode", {})
        entry["xcode"].update(xcode)
        entry.update(fields)
        path.write_text(json.dumps({"schemaVersion": 1, "hosts": {"mini": entry}}))
        return controller.pin_for_host(path, "mini")

    def test_defaults_are_generic(self):
        pin = self.host()
        self.assertEqual(pin.apple, {"idVar": "APPLE_ID", "passwordVar": "APPLE_ID_PASSWORD"})
        self.assertEqual(pin.role, "phone-gate")
        self.assertEqual(pin.retain, 1)
        self.assertEqual(pin.paths["consumer"], str(Path.home() / "code/evo"))
        self.assertEqual(pin.paths["credentialsProject"], pin.paths["consumer"])
        self.assertNotIn("hostSetup", pin.paths)
        self.assertIsNone(pin.runner)

    def test_retain_boundary(self):
        self.assertEqual(self.host(xcode={"retain": 1}).retain, 1)
        for bad in (0, -1, "2", True, 1.5):
            with self.assertRaisesRegex(controller.UpdateError, "retain"):
                self.host(xcode={"retain": bad})

    def test_identifier_fields_are_validated(self):
        self.assertEqual(self.host(device={"udid": UDID}).device_udid, UDID)
        for bad in ("00008120-00122CC80233401", "00008120-00122CC80233401EF", "0000812000122CC80233401E", "0000812G-00122CC80233401E"):
            with self.assertRaisesRegex(controller.UpdateError, "udid"):
                self.host(device={"udid": bad})
        self.assertEqual(self.host(signing={"identity": "Apple Development", "team": "TAML3DQJTR"}).signing["team"], "TAML3DQJTR")
        for bad in ("TAML3DQJT", "TAML3DQJTRX", "taml3dqjtr"):
            with self.assertRaisesRegex(controller.UpdateError, "team"):
                self.host(signing={"identity": "Apple Development", "team": bad})
        self.assertEqual(self.host(apple={"idVar": "ZOE_APPLE_ID"}).apple, {"idVar": "ZOE_APPLE_ID", "passwordVar": "APPLE_ID_PASSWORD"})
        for bad in ({"idVar": "zoe_apple_id"}, {"idVar": "1APPLE"}, {"other": "X"}):
            with self.assertRaisesRegex(controller.UpdateError, "apple"):
                self.host(apple=bad)
        with self.assertRaisesRegex(controller.UpdateError, "absolute"):
            self.host(paths={"consumer": "code/evo"})
        with self.assertRaisesRegex(controller.UpdateError, "paths accepts"):
            self.host(paths={"evo": "/x"})
        with self.assertRaisesRegex(controller.UpdateError, "runner.label"):
            self.host(runner={"label": "bad/label"})
        with self.assertRaisesRegex(controller.UpdateError, "runner.dir"):
            self.host(runner={"label": "a.b", "dir": "runner"})
        with self.assertRaisesRegex(controller.UpdateError, "requiredEnv"):
            self.host(runner={"label": "a.b", "requiredEnv": ["lower"]})
        with self.assertRaisesRegex(controller.UpdateError, "organization"):
            self.host(runner={"label": "a.b", "organization": "-bad"})
        with self.assertRaisesRegex(controller.UpdateError, "ci-image host requires"):
            self.host(role="ci-image")

    def test_repository_manifest_entries(self):
        manifest = SCRIPT.parents[0] / "updates.json"
        zoe = controller.pin_for_host(manifest, "zoe-mac-mini")
        self.assertEqual((zoe.role, zoe.phase, zoe.apple["idVar"]), ("ci-image", "promote", "ZOE_APPLE_ID"))
        self.assertEqual(zoe.paths["hostSetup"], "/Users/zoe/code/agent-host-setup")
        pro = controller.pin_for_host(manifest, "christophers-macbook-pro")
        self.assertEqual((pro.role, pro.phase, pro.version, pro.build), ("phone-gate", "promote", "27.2", "27B52"))
        self.assertEqual(pro.runner, {"label": "actions.runner.darwin-health.evo-cd-mbp-device",
                                      "dir": "/Users/chrisheinrich/actions-runner-evo-cd-mbp-device",
                                      "requiredEnv": ["MORPHEUS_CREDENTIALS_REPO"], "organization": "darwin-health"})
        self.assertEqual(pro.checks[0]["argv"][-2:], ["--device-gate", UDID])
        air = controller.pin_for_host(manifest, "christophers-macbook-air")
        self.assertEqual((air.apple, air.runner, air.verification, air.phase),
                         ({"idVar": "APPLE_ID", "passwordVar": "APPLE_ID_PASSWORD"}, None, None, "stage"))
        self.assertEqual(air.device_udid, "00008150-0015741E3AEA401C")

    def test_xcodes_names_the_configured_variables_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(controller.UpdateError, "MY_ID and MY_PASSWORD are missing"):
                controller.xcodes_environment({"idVar": "MY_ID", "passwordVar": "MY_PASSWORD"})
        with patch.dict(os.environ, {"APPLE_ID": "u@example.test", "APPLE_ID_PASSWORD": "p", "ZOE_APPLE_ID": "other"}, clear=True):
            env = controller.xcodes_environment()
        self.assertEqual((env["XCODES_USERNAME"], env["XCODES_PASSWORD"]), ("u@example.test", "p"))


class FakeHost:
    """A healthy phone-gate host; tests flip one fact at a time."""

    def __init__(self, test):
        temporary = tempfile.TemporaryDirectory()
        test.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        for name in ("factory", "evo", "runner"):
            (self.root / name / ".git").mkdir(parents=True)
        self.label = "actions.runner.org.device"
        self.plist = self.home / "Library/LaunchAgents" / f"{self.label}.plist"
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(controller.plistlib.dumps({"Label": self.label, "ProgramArguments": ["/bin/true"]}))
        (self.root / "runner/.env").write_text("LANG=en_US.UTF-8\nMORPHEUS_CREDENTIALS_REPO=/x/.credentials\n")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"pro": {
            "role": "phone-gate",
            "xcode": {"version": "27.2", "build": "27B52", "phase": "stage"},
            "paths": {"factory": str(self.root / "factory"), "consumer": str(self.root / "evo")},
            "apple": {"idVar": "ZOE_APPLE_ID", "passwordVar": "ZOE_APPLE_ID_PASSWORD"},
            "verification": {"purpose": "apple-developer", "account": "zoe-apple-developer"},
            "runner": {"label": self.label, "dir": str(self.root / "runner"), "requiredEnv": ["MORPHEUS_CREDENTIALS_REPO"], "organization": "darwin-health"},
            "signing": {"identity": "Apple Development", "team": "TAML3DQJTR"},
            "device": {"udid": UDID},
            "validation": [{"argv": [sys.executable, "-c", "pass"]}],
        }}}))
        self.manifest = manifest
        self.pin = controller.pin_for_host(manifest, "pro")
        self.free = 70 * 1024**3
        self.installed = None
        self.sudo_refused = False
        self.helper = self.root / "morpheus-factory-xcode"
        self.helper.write_bytes(controller.HELPER_SOURCE.read_bytes())
        self.helper_stat = SimpleNamespace(st_uid=0, st_mode=0o100755)
        self.ancestor_stats = {}
        self.identities = [(FINGERPRINT_A, "Apple Development: Chris (266U7M6NPQ)", "TAML3DQJTR"),
                           (FINGERPRINT_B, "Apple Development: chris@example.test (BB58HPM92M)", "D495224G8R")]
        # The gate phone (UDID in lowercase, as devicectl may print it) is wired
        # and connected; another paired phone is on the network but disconnected.
        self.devicectl = {"result": {"devices": [
            device_record(UDID.lower(), "connected", "wired"),
            device_record("00008150-0015741E3AEA401C", "disconnected", "localNetwork", name="Other iPhone")]}}
        self.gh = (0, "")
        self.env = {"ZOE_APPLE_ID": "user@example.test", "ZOE_APPLE_ID_PASSWORD": "secret",
                    "FACTORY_VERIFICATION_CODE_COMMAND": sys.executable,
                    "FACTORY_VERIFICATION_CODE_URL": "https://broker.example.test", "USER": "chris"}
        self.commands = []

    def run(self, argv, **kwargs):
        self.commands.append(argv)
        if argv[:3] == ["/usr/bin/sudo", "-n", "-l"]:
            if self.sudo_refused:
                raise controller.UpdateError("sudo exited 1")
            return argv[3]
        if argv[:2] == ["/usr/bin/security", "find-identity"]:
            lines = [f'  {index}) {fingerprint} "{name}"' for index, (fingerprint, name, _) in enumerate(self.identities, 1)]
            return "\n".join(lines + [f"     {len(lines)} valid identities found"])
        if argv[:2] == ["/usr/bin/security", "find-certificate"]:
            return "\n".join(f"SHA-1 hash: {fingerprint}\n-----BEGIN CERTIFICATE-----\n{team}\n-----END CERTIFICATE-----"
                             for fingerprint, _, team in self.identities)
        if argv[:3] == ["/usr/bin/xcrun", "devicectl", "list"]:
            assert argv[3:5] == ["devices", "--json-output"], argv
            Path(argv[5]).write_text(self.devicectl if isinstance(self.devicectl, str) else json.dumps(self.devicectl))
            return "noise on stdout, not JSON"
        raise AssertionError(argv)

    def status(self, path=None):
        if path is not None:
            return self.ancestor_stats.get(path, SimpleNamespace(st_uid=0, st_mode=0o040755))
        if self.helper_stat is None:
            raise FileNotFoundError(str(self.helper))
        return self.helper_stat

    def gh_run(self, argv, **kwargs):
        self.commands.append(argv)
        code, stderr = self.gh
        return SimpleNamespace(returncode=code, stdout="secret-body", stderr=stderr)

    @contextmanager
    def patched(self):
        team = lambda pem: pem.split("\n")[1]
        which = lambda name: f"/opt/homebrew/bin/{name}"
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(controller.Path, "home", return_value=self.home), \
             patch.object(controller.shutil, "which", side_effect=which), \
             patch.object(controller.shutil, "disk_usage", return_value=SimpleNamespace(free=self.free)), \
             patch.object(controller, "matching_app", return_value=self.installed), \
             patch.object(controller, "certificate_team", side_effect=team), \
             patch.object(controller, "HELPER_PATH", str(self.helper)), \
             patch.object(controller, "helper_status", side_effect=self.status), \
             patch.object(controller.subprocess, "run", side_effect=self.gh_run), \
             patch.object(controller, "run", side_effect=self.run):
            yield

    def checks(self):
        with self.patched():
            return {check["name"]: check for check in controller.host_checks(self.pin)}


class CheckTests(unittest.TestCase):
    ALL = ["xcodes", "disk", "sudo", "apple-credentials", "verification-provider", "paths", "runner", "runner-env",
           "runner-groups", "signing", "device", "validation"]

    def failing(self, host):
        return sorted(name for name, check in host.checks().items() if not check["ok"])

    def test_healthy_host_passes_every_check(self):
        host = FakeHost(self)
        checks = host.checks()
        self.assertEqual(list(checks), self.ALL)
        self.assertEqual([name for name, check in checks.items() if not check["ok"]], [])
        self.assertIn(FINGERPRINT_A, checks["signing"]["detail"])
        self.assertTrue(checks["device"]["detail"].endswith("tunnel connected"))
        sudo = [argv[3:] for argv in host.commands if argv[:3] == ["/usr/bin/sudo", "-n", "-l"]]
        self.assertEqual(sudo, [[str(host.helper)]])

    def test_disk_boundary_and_installed_app(self):
        host = FakeHost(self)
        host.free = 70 * 1024**3 - 1
        self.assertEqual(self.failing(host), ["disk"])
        host.installed = Path("/Applications/Xcode_27.2.app")
        self.assertEqual(host.checks()["disk"], {"name": "disk", "ok": True, "blocking": True, "detail": "pinned Xcode is already installed"})

    def test_refused_sudo_prints_the_install_commands(self):
        host = FakeHost(self)
        host.sudo_refused = True
        checks = host.checks()
        self.assertEqual(self.failing(host), ["sudo"])
        detail = checks["sudo"]["detail"]
        self.assertIn(f"sudo -n does not allow {host.helper}", detail)
        self.assertIn(f"sudo install -d -o root -g wheel -m 755 {host.helper.parent}; sudo install -o root -g wheel -m 755 {controller.HELPER_SOURCE} {host.helper}", detail)
        self.assertIn(f"sudo visudo -f /etc/sudoers.d/morpheus-factory: chris ALL=(root) NOPASSWD: {host.helper}", detail)

    def test_helper_must_be_root_owned_unwritable_and_current(self):
        host = FakeHost(self)
        host.helper_stat = SimpleNamespace(st_uid=501, st_mode=0o100755)
        self.assertIn("is not owned by root", host.checks()["sudo"]["detail"])
        for mode in (0o100775, 0o100757):
            host.helper_stat = SimpleNamespace(st_uid=0, st_mode=mode)
            self.assertEqual(self.failing(host), ["sudo"])
            self.assertIn("group- or other-writable", host.checks()["sudo"]["detail"])
        host.helper_stat = SimpleNamespace(st_uid=0, st_mode=0o100755)
        host.helper.write_bytes(controller.HELPER_SOURCE.read_bytes() + b"# changed\n")
        self.assertEqual(self.failing(host), ["sudo"])
        self.assertIn("differs from xcode-root-helper.sh; reinstall it", host.checks()["sudo"]["detail"])
        host.helper.write_bytes(controller.HELPER_SOURCE.read_bytes())
        self.assertEqual(self.failing(host), [])
        # Ancestors are checked on the resolved path (a temp dir under /var resolves to /private/var).
        parent = str(host.helper.parent.resolve())
        for stat in (SimpleNamespace(st_uid=501, st_mode=0o040755), SimpleNamespace(st_uid=0, st_mode=0o040775)):
            host.ancestor_stats = {parent: stat}
            self.assertEqual(self.failing(host), ["sudo"])
            self.assertIn(f"{parent} is not root-owned and closed to group/other writes", host.checks()["sudo"]["detail"])
        host.ancestor_stats = {"/": SimpleNamespace(st_uid=0, st_mode=0o041777)}
        self.assertIn("/ is not root-owned", host.checks()["sudo"]["detail"])
        host.ancestor_stats = {}
        host.helper.unlink()
        host.helper_stat = None
        self.assertEqual(self.failing(host), ["sudo"])
        self.assertIn(f"{host.helper} is not installed", host.checks()["sudo"]["detail"])

    def test_missing_credentials_print_names_never_values(self):
        host = FakeHost(self)
        host.env["ZOE_APPLE_ID_PASSWORD"] = ""
        checks = host.checks()
        self.assertEqual(self.failing(host), ["apple-credentials"])
        self.assertEqual(checks["apple-credentials"]["detail"], "missing or empty: ZOE_APPLE_ID_PASSWORD")
        self.assertNotIn("user@example.test", json.dumps(checks))

    def test_provider_requires_https(self):
        host = FakeHost(self)
        host.env["FACTORY_VERIFICATION_CODE_URL"] = "http://broker.example.test"
        self.assertEqual(self.failing(host), ["verification-provider"])

    def test_two_identities_for_one_team_are_ambiguous(self):
        host = FakeHost(self)
        host.identities.append((FINGERPRINT_C, "Apple Development: Chris (266U7M6NPQ)", "TAML3DQJTR"))
        detail = host.checks()["signing"]["detail"]
        self.assertEqual(self.failing(host), ["signing"])
        self.assertIn(f"{FINGERPRINT_A}, {FINGERPRINT_C}", detail)

    def test_no_identity_for_team_fails(self):
        host = FakeHost(self)
        host.identities = [host.identities[1]]
        self.assertEqual(self.failing(host), ["signing"])
        self.assertEqual(host.checks()["signing"]["detail"], 'no valid "Apple Development" identity for team TAML3DQJTR')

    def test_device_must_be_paired_and_connected(self):
        host = FakeHost(self)
        self.assertEqual(host.checks()["device"]["detail"], f"{UDID} (Zoe\u2019s iPhone, wired): pairing paired, tunnel connected")
        devices = host.devicectl["result"]["devices"]
        # An idle wired phone's tunnel is down until needed: still a pass.
        devices[0]["connectionProperties"].update(tunnelState="disconnected")
        self.assertEqual(self.failing(host), [])
        self.assertIn("wired): pairing paired, tunnel disconnected", host.checks()["device"]["detail"])
        devices[0]["connectionProperties"].update(tunnelState="disconnected", transportType="localNetwork")
        self.assertEqual(self.failing(host), ["device"])
        self.assertIn("paired but not connected", host.checks()["device"]["detail"])
        self.assertIn("localNetwork", host.checks()["device"]["detail"])
        devices[0]["connectionProperties"].update(tunnelState="connected", transportType="localNetwork")
        self.assertEqual(self.failing(host), [])
        devices[0]["connectionProperties"].update(tunnelState="connected", pairingState="unpaired", transportType="wired")
        self.assertEqual(self.failing(host), ["device"])
        del devices[0]
        self.assertEqual(host.checks()["device"]["detail"], f"{UDID} is not paired to this Mac; pair and connect the iPhone")

    def test_unreadable_device_json_is_not_a_pass(self):
        host = FakeHost(self)
        for broken in ("not json", {"result": {}}, {"result": {"devices": "x"}}):
            host.devicectl = broken
            check = host.checks()["device"]
            self.assertFalse(check["ok"])
            self.assertTrue(check["detail"].startswith("could not be determined"), check["detail"])

    def test_device_command_error_is_not_a_pass(self):
        host = FakeHost(self)
        original = host.run

        def broken(argv, **kwargs):
            if argv[:2] == ["/usr/bin/xcrun", "devicectl"]:
                raise controller.UpdateError("xcrun exited 1")
            return original(argv, **kwargs)

        host.run = broken
        checks = host.checks()
        self.assertEqual(checks["device"], {"name": "device", "ok": False, "blocking": False, "detail": "could not be determined: xcrun exited 1"})

    def test_session_create_and_missing_plist_fail_with_fix(self):
        host = FakeHost(self)
        host.plist.write_bytes(controller.plistlib.dumps({"Label": host.label, "SessionCreate": True}))
        detail = host.checks()["runner"]["detail"]
        self.assertEqual(self.failing(host), ["runner"])
        self.assertIn(f"plutil -remove SessionCreate {host.plist}", detail)
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} {host.plist}", detail)
        host.plist.write_bytes(controller.plistlib.dumps({"Label": host.label, "SessionCreate": False}))
        self.assertEqual(self.failing(host), [])
        host.plist.unlink()
        self.assertEqual(host.checks()["runner"], {"name": "runner", "ok": False, "blocking": False, "detail": f"runner LaunchAgent not installed: {host.plist}"})

    def test_runner_env_must_define_required_names(self):
        host = FakeHost(self)
        (host.root / "runner/.env").write_text("LANG=en_US.UTF-8\n# MORPHEUS_CREDENTIALS_REPO=/x\n")
        detail = host.checks()["runner-env"]["detail"]
        self.assertEqual(self.failing(host), ["runner-env"])
        self.assertIn("does not define MORPHEUS_CREDENTIALS_REPO", detail)
        self.assertIn("stock runner installer rerun drops them", detail)
        self.assertNotIn("/x", detail.replace(str(host.root), ""))
        (host.root / "runner/.env").unlink()
        self.assertEqual(self.failing(host), ["runner-env"])

    def test_runner_groups_forbidden_and_error_both_fail(self):
        host = FakeHost(self)
        host.gh = (1, "gh: Must have admin rights to Repository. (HTTP 403)")
        detail = host.checks()["runner-groups"]["detail"]
        self.assertEqual(self.failing(host), ["runner-groups"])
        self.assertIn("admin:org", detail)
        self.assertNotIn("secret-body", detail)
        host.gh = (1, "error connecting to api.github.com")
        self.assertEqual(host.checks()["runner-groups"]["detail"], "could not be determined: gh api exited 1")
        self.assertTrue(any(argv[1:] == ["api", "orgs/darwin-health/actions/runner-groups"] for argv in host.commands))

    def test_missing_validation_executable_fails(self):
        host = FakeHost(self)
        host.pin.checks[0]["argv"][0] = str(host.root / "missing")
        self.assertEqual(self.failing(host), ["validation"])

    def test_check_command_exits_nonzero_but_prints_json(self):
        host = FakeHost(self)
        host.devicectl["result"]["devices"][0]["connectionProperties"].update(tunnelState="disconnected", transportType="localNetwork")
        output = io.StringIO()
        argv = ["update_controller.py", "check", "--host", "pro", "--manifest", str(host.manifest), "--json"]
        with patch.object(sys, "argv", argv), redirect_stdout(output), host.patched():
            self.assertEqual(controller.main(), 1)
        document = json.loads(output.getvalue())
        self.assertFalse(document["ok"])
        self.assertEqual([check["name"] for check in document["checks"] if not check["ok"]], ["device"])

    def test_signing_team_comes_from_real_shaped_certificate_subjects(self):
        subjects = {
            "PEM-A": ("subject= \n    userId                    = X35TT7TP7E\n"
                      "    commonName                = Apple Development: Christopher Heinrich (266U7M6NPQ)\n"
                      "    organizationalUnitName    = TAML3DQJTR\n    organizationName          = Darwin Life Inc.\n"
                      "    countryName               = US\n"),
            "PEM-B": ("subject= \n    userId                    = X35TT7TP7E\n"
                      "    commonName                = Apple Development: cpheinrich@gmail.com (BB58HPM92M)\n"
                      "    organizationalUnitName    = D495224G8R\n    organizationName          = Christopher Heinrich\n"
                      "    countryName               = US\n"),
        }
        listing = (f'  1) {FINGERPRINT_B} "Apple Development: cpheinrich@gmail.com (BB58HPM92M)"\n'
                   f'  2) {FINGERPRINT_A} "Apple Development: Christopher Heinrich (266U7M6NPQ)"\n'
                   f'  3) {FINGERPRINT_C} "iPhone Distribution: Christopher Heinrich (D495224G8R)"\n     3 valid identities found')
        certificates = (f"SHA-1 hash: {FINGERPRINT_B}\n-----BEGIN CERTIFICATE-----\nPEM-B\n-----END CERTIFICATE-----\n"
                        f"SHA-1 hash: {FINGERPRINT_A}\n-----BEGIN CERTIFICATE-----\nPEM-A\n-----END CERTIFICATE-----\n")
        pin = SimpleNamespace(signing={"identity": "Apple Development", "team": "TAML3DQJTR"})

        def security(argv, **kwargs):
            if argv[1] == "find-identity":
                return listing
            if argv[1] == "find-certificate":
                return certificates
            raise AssertionError(argv)

        def openssl(argv, **kwargs):
            self.assertEqual(argv[:3], ["/usr/bin/openssl", "x509", "-noout"])
            return SimpleNamespace(returncode=0, stdout=subjects[kwargs["input"].split("\n")[1]])

        with patch.object(controller, "run", side_effect=security), patch.object(controller.subprocess, "run", side_effect=openssl):
            self.assertEqual(controller.check_signing(pin),
                             (True, f"Apple Development: Christopher Heinrich (266U7M6NPQ) {FINGERPRINT_A}"))
            pin.signing["team"] = "266U7M6NPQ"  # the name suffix is not the team
            self.assertEqual(controller.check_signing(pin), (False, 'no valid "Apple Development" identity for team 266U7M6NPQ'))
            pin.signing["team"] = "D495224G8R"
            self.assertEqual(controller.check_signing(pin)[0], True)
        with patch.object(controller, "run", side_effect=controller.UpdateError("security exited 1")):
            self.assertEqual(controller.check_signing(pin), (False, "could not read identities"))
        with patch.object(controller, "run", side_effect=security), \
             patch.object(controller.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")):
            self.assertEqual(controller.check_signing(pin), (False, "could not read identities"))

    def test_certificate_team_reads_organizational_unit(self):
        subject = "subject= \n    commonName                = Apple Development: Chris (266U7M6NPQ)\n    organizationalUnitName    = TAML3DQJTR\n"
        with patch.object(controller.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=subject)):
            self.assertEqual(controller.certificate_team("pem"), "TAML3DQJTR")
        with patch.object(controller.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=subject)):
            self.assertIsNone(controller.certificate_team("pem"))


class PreflightTests(unittest.TestCase):
    def test_stage_refuses_before_download_when_preflight_fails(self):
        host = FakeHost(self)
        host.sudo_refused = True
        state = host.root / "state"
        argv = ["update_controller.py", "stage", "--host", "pro", "--manifest", str(host.manifest), "--state-dir", str(state)]
        with patch.object(sys, "argv", argv), host.patched(), \
             patch.object(controller, "run_xcodes") as download, patch.object(controller, "stage") as stage:
            with self.assertRaisesRegex(controller.UpdateError, r"^preflight: sudo$"):
                controller.main()
        download.assert_not_called()
        stage.assert_not_called()

    def test_stage_proceeds_when_preflight_passes(self):
        host = FakeHost(self)
        argv = ["update_controller.py", "stage", "--host", "pro", "--manifest", str(host.manifest), "--state-dir", str(host.root / "state")]
        with patch.object(sys, "argv", argv), host.patched(), redirect_stdout(io.StringIO()), \
             patch.object(controller, "stage", return_value="staged") as stage:
            self.assertEqual(controller.main(), 0)
        stage.assert_called_once()
        self.assertEqual(stage.call_args.args[-1], {"idVar": "ZOE_APPLE_ID", "passwordVar": "ZOE_APPLE_ID_PASSWORD"})

    def test_preflight_covers_only_install_facts(self):
        host = FakeHost(self)
        host.devicectl["result"]["devices"][0]["connectionProperties"].update(tunnelState="disconnected", transportType="localNetwork")
        with host.patched():
            controller.preflight(host.pin)
        self.assertEqual(controller.PREFLIGHT, ("xcodes", "disk", "sudo", "apple-credentials", "verification-provider", "paths"))

    def test_reconcile_prunes_after_promotion_and_reports_removed(self):
        host = FakeHost(self)
        output = io.StringIO()
        argv = ["update_controller.py", "reconcile", "--host", "pro", "--manifest", str(host.manifest), "--state-dir", str(host.root / "state")]
        with patch.object(sys, "argv", argv), redirect_stdout(output), patch.object(controller, "preflight"), \
             patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "reconcile", return_value="promoted"), \
             patch.object(controller, "prune", return_value=["/Applications/Xcode_26.3.app"]) as prune:
            self.assertEqual(controller.main(), 0)
        # retain defaults to 1, but the promotion opens the migration window.
        prune.assert_called_once_with("27.2", "27B52", 1, ["/Applications/Xcode.app/Contents/Developer", None], window=True)
        self.assertEqual(json.loads(output.getvalue())["removed"], ["/Applications/Xcode_26.3.app"])

    def test_staged_hold_does_not_prune(self):
        host = FakeHost(self)
        argv = ["update_controller.py", "reconcile", "--host", "pro", "--manifest", str(host.manifest), "--state-dir", str(host.root / "state")]
        with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()), patch.object(controller, "preflight"), \
             patch.object(controller, "report", return_value={"selected": "/x", "selectedMatches": False}), \
             patch.object(controller, "reconcile", return_value="staged"), patch.object(controller, "prune") as prune:
            controller.main()
        prune.assert_not_called()


class ActivateTests(unittest.TestCase):
    def activate(self, processes):
        commands = []
        app = Path("/Applications/Xcode_27.2.app")

        def command(argv, **kwargs):
            commands.append(argv)
            return processes if argv[0] == "/bin/ps" else ""

        with patch.object(controller, "matching_app", return_value=app), patch.object(controller, "run", side_effect=command), \
             patch.object(controller, "report", return_value={"selectedMatches": True}):
            result = controller.activate("27.2", "27B52")
        return result, commands

    def test_live_runner_job_defers_activation(self):
        for busy in ("/Users/chris/actions-runner/bin/Runner.Worker", "/Applications/Xcode.app/Contents/Developer/usr/bin/xcodebuild",
                     "swift-frontend"):
            with self.assertRaisesRegex(controller.UpdateError, "deferred"):
                self.activate(f"/sbin/launchd\n{busy}\n")

    def test_idle_runner_listener_does_not_defer(self):
        result, commands = self.activate("/sbin/launchd\n/Users/chris/actions-runner/bin/Runner.Listener\n")
        self.assertEqual(result, "activated")
        self.assertIn(["/usr/bin/sudo", "-n", controller.HELPER_PATH, "select", "/Applications/Xcode_27.2.app"], commands)


class PruneTests(unittest.TestCase):
    def prune(self, versions, retain, extra=(), unreadable=(), selected="27.2", previous=None, symlink=None, busy=False, window=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        applications = Path(temporary.name).resolve()
        for version in versions:
            (applications / f"Xcode_{version}.app").mkdir()
        for name in extra:
            (applications / name).mkdir()
        if symlink:
            (applications / "Xcode.app").symlink_to(applications / f"Xcode_{symlink}.app")
        commands = []

        def identity(app):
            if app.name in unreadable or not app.name.startswith("Xcode"):
                return None
            return ("x", "y")

        def command(argv, **kwargs):
            commands.append(argv)
            if argv[0] == "/bin/ps":
                return "/sbin/launchd\n" + ("/opt/runner/bin/Runner.Worker\n" if busy else "/opt/runner/bin/Runner.Listener\n")
            return ""

        current = {"selectedMatches": True, "selected": str(applications / f"Xcode_{selected}.app/Contents/Developer")}
        before = None if previous is None else str(applications / f"Xcode_{previous}.app/Contents/Developer")
        with patch.object(controller, "APPLICATIONS", applications), patch.object(controller, "installed_build", side_effect=identity), \
             patch.object(controller, "report", return_value=current), patch.object(controller, "run", side_effect=command):
            removed = controller.prune("27.2", "27B52", retain, before, window=window)
        helper = [argv for argv in commands if argv[0] != "/bin/ps"]
        names = [Path(path).name for path in removed]
        self.assertEqual(helper, [["/usr/bin/sudo", "-n", controller.HELPER_PATH, "remove", path] for path in removed])
        return names

    def test_retain_two_with_exactly_two_removes_nothing(self):
        self.assertEqual(self.prune(["27.2", "27.0"], 2), [])

    def test_retain_two_with_three_removes_the_oldest(self):
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 2), ["Xcode_26.3.app"])

    def test_versions_compare_numerically(self):
        self.assertEqual(self.prune(["27.2", "26.9", "26.10"], 2), ["Xcode_26.9.app"])

    def test_retain_one_keeps_only_selected_and_never_plain_xcode(self):
        self.assertEqual(sorted(self.prune(["27.2", "27.0", "26.3"], 1, extra=["Xcode.app", "Xcode_beta.app"])),
                         ["Xcode_26.3.app", "Xcode_27.0.app"])

    def test_previously_selected_app_is_kept_over_a_newer_leftover(self):
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 2, previous="26.3"), ["Xcode_27.0.app"])

    def test_previous_selection_counts_toward_retain(self):
        self.assertEqual(sorted(self.prune(["27.2", "27.1", "27.0", "26.3"], 3, previous="26.3")), ["Xcode_27.0.app"])

    def test_xcode_symlink_target_is_protected(self):
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 2, symlink="26.3"), ["Xcode_27.0.app"])
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 1, symlink="26.3"), ["Xcode_27.0.app"])

    def test_busy_host_prunes_nothing(self):
        with patch.object(controller, "host_is_busy", return_value=True), patch.object(controller, "root_helper") as helper:
            self.assertIsNone(controller.prune("27.2", "27B52", 1))
        helper.assert_not_called()

    def test_unreadable_bundle_is_never_removed(self):
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 2, unreadable=["Xcode_26.3.app"]), [])

    def test_prune_refuses_when_pin_is_not_selected(self):
        with patch.object(controller, "report", return_value={"selectedMatches": False, "selected": "/x"}), \
             patch.object(controller, "host_is_busy", return_value=False), patch.object(controller, "root_helper") as helper:
            with self.assertRaisesRegex(controller.UpdateError, "pruning refused"):
                controller.prune("27.2", "27B52", 2)
        helper.assert_not_called()


    def test_window_keeps_the_previous_app_whatever_retain_says(self):
        # retain 1 would leave only the selected app; until delivery validates
        # the pin, the newest other bundle stays as the rollback.
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 1, window=True), ["Xcode_26.3.app"])
        self.assertEqual(self.prune(["27.2", "27.0", "26.3"], 1, window=True, previous="26.3"), ["Xcode_27.0.app"])
        self.assertEqual(sorted(self.prune(["27.2", "27.0", "26.3"], 1)), ["Xcode_26.3.app", "Xcode_27.0.app"])


class RuntimeTests(unittest.TestCase):
    def ci_host(self, **ci):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "manifest.json"
        block = {"repository": "darwin-health/evo", "image": "ci-xcode-27.2", "label": "xcode-27.2",
                 "builder": "/usr/bin/true", "verifier": "/usr/bin/true", "activator": "/usr/bin/true", **ci}
        path.write_text(json.dumps({"schemaVersion": 1, "hosts": {"mini": {
            "xcode": {"version": "27.2", "build": "27B52"}, "ci": block, "validation": [{"argv": ["/usr/bin/true"]}]}}}))
        return controller.pin_for_host(path, "mini")

    def test_runtimes_must_be_unique_exact_versions(self):
        self.assertEqual(self.ci_host(runtimes=["26.5", "27.2"]).ci["runtimes"], ["26.5", "27.2"])
        self.assertEqual(self.ci_host(runtimes=["26.5.1"]).ci["runtimes"], ["26.5.1"])
        self.assertNotIn("runtimes", self.ci_host().ci)
        for bad in ([], ["26"], ["26.5", "26.5"], ["latest"], "26.5", [26.5], ["26.5 "], ["1.2.3.4"]):
            with self.assertRaisesRegex(controller.UpdateError, "ci.runtimes"):
                self.ci_host(runtimes=bad)
        with self.assertRaisesRegex(controller.UpdateError, "imagePruner"):
            self.ci_host(imagePruner="ci-vm/prune-images.mjs")

    def test_repository_mini_lists_both_runtimes(self):
        zoe = controller.pin_for_host(SCRIPT.parents[0] / "updates.json", "zoe-mac-mini")
        self.assertEqual(zoe.ci["runtimes"], ["26.5", "27.2"])
        self.assertEqual(zoe.retain, 1)

    def stage(self, state, runtimes, sealed=(False, True)):
        ci = {"image": "ci-xcode-27.2", "label": "xcode-27.2", "repository": "darwin-health/evo", "tart": "/t",
              "builder": "/b/build-image.sh", "verifier": "/b/verify-image.sh", "activator": "/b/activate-pool.mjs"}
        if runtimes is not None:
            ci["runtimes"] = runtimes
        with patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "image_is_sealed", side_effect=list(sealed)), \
             patch.object(controller, "image_identity", return_value={"disk.img": {"inode": 1}}), \
             patch.object(controller, "run", return_value="") as run:
            result = controller.stage_ci_image("27.2", "27B52", state, None, [], ci)
        return result, [call.args[0] for call in run.call_args_list]

    def test_builder_and_verifier_receive_each_runtime(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        result, commands = self.stage(Path(temporary.name), ["26.5", "27.2"])
        self.assertEqual(result, "verified-and-activated")
        self.assertEqual(commands[0], ["/b/build-image.sh", "--xcode", "/Applications/Xcode_27.2.app", "--name", "ci-xcode-27.2",
                                       "--runtime", "26.5", "--runtime", "27.2"])
        self.assertEqual(commands[1], ["/b/verify-image.sh", "--name", "ci-xcode-27.2", "--version", "27.2", "--build", "27B52",
                                       "--runtime", "26.5", "--runtime", "27.2"])
        self.assertEqual(commands[2][0], "/b/activate-pool.mjs")
        receipt = json.loads((Path(temporary.name) / "ci-images/ci-xcode-27.2.json").read_text())
        self.assertEqual(receipt["runtimes"], ["26.5", "27.2"])

    def test_failed_verification_says_how_to_rebuild_a_stale_image(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        ci = {"image": "ci-xcode-27.2", "label": "xcode-27.2", "repository": "darwin-health/evo", "tart": "/t", "runtimes": ["26.5"],
              "builder": "/b/build-image.sh", "verifier": "/b/verify-image.sh", "activator": "/b/activate-pool.mjs"}

        def command(argv, **kwargs):
            if argv[0] == "/b/verify-image.sh":
                raise controller.UpdateError("verify-image.sh exited 1")
            return ""

        with patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "image_is_sealed", return_value=True), \
             patch.object(controller, "image_identity", return_value={"disk.img": {"inode": 1}}), \
             patch.object(controller, "run", side_effect=command) as run:
            with self.assertRaises(controller.UpdateError) as caught:
                controller.stage_ci_image("27.2", "27B52", Path(temporary.name), None, [], ci)
        message = str(caught.exception)
        self.assertIn("tart rename ci-xcode-27.2 ci-xcode-27.2-stale", message)
        self.assertIn("tart delete ci-xcode-27.2", message)
        # Sealed: never rebuilt automatically, and nothing activated.
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ["/b/verify-image.sh"])

    def test_no_runtimes_passes_no_runtime_flags(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        _, commands = self.stage(Path(temporary.name), None)
        self.assertEqual(commands[0][-2:], ["--name", "ci-xcode-27.2"])
        self.assertEqual(commands[1][-2:], ["--build", "27B52"])

    def test_changed_runtimes_force_reverification(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name)
        self.stage(state, ["27.2"])
        result, commands = self.stage(state, ["27.2"], sealed=(True, True))
        self.assertEqual(result, "already-verified")
        self.assertFalse(any(argv[0] == "/b/verify-image.sh" for argv in commands))
        result, commands = self.stage(state, ["26.5", "27.2"], sealed=(True, True))
        self.assertEqual(result, "verified-and-activated")
        self.assertIn(["/b/verify-image.sh", "--name", "ci-xcode-27.2", "--version", "27.2", "--build", "27B52",
                       "--runtime", "26.5", "--runtime", "27.2"], commands)

    def runtimes_check(self, listing):
        pin = SimpleNamespace(ci={"runtimes": ["26.5", "27.2"]})
        with patch.object(controller, "run", return_value=json.dumps(listing)) as run:
            result = controller.check_runtimes(pin)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/xcrun", "simctl", "runtime", "list", "-j"])
        return result

    def test_runtimes_check_requires_each_listed_runtime_ready(self):
        ios = "com.apple.platform.iphonesimulator"
        both = {"A": {"version": "26.5", "platformIdentifier": ios, "state": "Ready"},
                "B": {"version": "27.2", "platformIdentifier": ios, "state": "Ready"}}
        self.assertEqual(self.runtimes_check(both)[0], True)
        ok, detail = self.runtimes_check({"A": both["A"], "W": {"version": "27.2", "platformIdentifier": "com.apple.platform.watchsimulator"}})
        self.assertFalse(ok)
        self.assertIn("missing on the host: 27.2", detail)
        self.assertIn("xcodebuild -downloadPlatform iOS", detail)
        ok, _ = self.runtimes_check({"A": both["A"], "B": {**both["B"], "state": "Deleting"}})
        self.assertFalse(ok)

    def test_runtimes_check_applies_to_ci_hosts_with_runtimes_and_never_passes_on_error(self):
        pin = self.ci_host(runtimes=["27.2"])
        with patch.object(controller, "run", side_effect=controller.UpdateError("xcrun exited 1")):
            checks = {check["name"]: check for check in controller.host_checks(pin, only=("runtimes",))}
        self.assertEqual(checks["runtimes"], {"name": "runtimes", "ok": False, "blocking": True, "detail": "could not be determined: xcrun exited 1"})
        self.assertEqual(controller.host_checks(self.ci_host(), only=("runtimes",)), [])


class PromotionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"

    def test_activate_writes_a_private_promotion_record(self):
        def command(argv, **kwargs):
            if argv[0] == "/bin/ps":
                return "/sbin/launchd\n"
            return "/Applications/Xcode_26.3.app/Contents/Developer" if argv[0] == "/usr/bin/xcode-select" else ""

        with patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "run", side_effect=command), patch.object(controller, "report", return_value={"selectedMatches": True}):
            self.assertEqual(controller.activate("27.2", "27B52", self.state), "activated")
        path = self.state / "promoted.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        record = json.loads(path.read_text())
        self.assertEqual((record["version"], record["build"], record["previous"]),
                         ("27.2", "27B52", "/Applications/Xcode_26.3.app/Contents/Developer"))
        self.assertRegex(record["promotedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$")
        self.assertEqual(list(self.state.iterdir()), [path])  # no temporary left behind

    def test_reactivating_the_selected_pin_keeps_the_promotion_record(self):
        selected = "/Applications/Xcode_27.2.app/Contents/Developer"
        controller.record_promotion(self.state, "27.2", "27B52", "/Applications/Xcode_26.3.app/Contents/Developer")
        original = controller.promotion(self.state, "27.2", "27B52")

        def command(argv, **kwargs):
            return "/sbin/launchd\n" if argv[0] == "/bin/ps" else (selected if argv[0] == "/usr/bin/xcode-select" else "")

        with patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "run", side_effect=command), patch.object(controller, "report", return_value={"selectedMatches": True}), \
             patch.object(controller, "now_iso", return_value="2099-01-01T00:00:00+00:00"):
            controller.activate("27.2", "27B52", self.state)
            self.assertEqual(controller.promotion(self.state, "27.2", "27B52"), original)
            # With no record, a re-activation records the window without naming the selected app as previous.
            (self.state / "promoted.json").unlink()
            controller.activate("27.2", "27B52", self.state)
        record = controller.promotion(self.state, "27.2", "27B52")
        self.assertEqual((record["previous"], record["promotedAt"]), (None, "2099-01-01T00:00:00+00:00"))

    def test_failed_activation_writes_no_record(self):
        with patch.object(controller, "matching_app", return_value=Path("/Applications/Xcode_27.2.app")), \
             patch.object(controller, "run", return_value=""), patch.object(controller, "report", return_value={"selectedMatches": False}):
            with self.assertRaises(controller.UpdateError):
                controller.activate("27.2", "27B52", self.state)
        self.assertFalse((self.state / "promoted.json").exists())

    def status(self):
        manifest = UpdateControllerTests.manifest(self)
        output = io.StringIO()
        argv = ["update_controller.py", "status", "--host", "mini", "--manifest", str(manifest), "--state-dir", str(self.state)]
        with patch.object(sys, "argv", argv), redirect_stdout(output), \
             patch.object(controller, "report", return_value={"selectedMatches": True, "installedMatches": True}):
            controller.main()
        return json.loads(output.getvalue())

    def test_status_reports_promoted_at_for_this_pin_only(self):
        self.assertIsNone(self.status()["promotedAt"])
        controller.record_promotion(self.state, "27.1", "27A1", None)
        self.assertIsNone(self.status()["promotedAt"])
        controller.record_promotion(self.state, "27.2", "27B52", "/x")
        status = self.status()
        self.assertEqual(status["promotedAt"], controller.promotion(self.state, "27.2", "27B52")["promotedAt"])
        self.assertIsNone(status["retiredAt"])


class RetireTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.pruner = self.root / "prune-images.mjs"
        self.pruner.write_text("#!/bin/sh\n")
        self.pruner.chmod(0o755)
        self.pin = SimpleNamespace(version="27.2", build="27B52", retain=1, role="ci-image",
                                   ci={"imagePruner": str(self.pruner)}, paths={"hostSetup": "/unused"})

    def retire(self, busy=False, selected="/Applications/Xcode_27.2.app/Contents/Developer", previous="/Applications/Xcode_26.3.app/Contents/Developer",
               record=True):
        if record:
            controller.record_promotion(self.state, "27.2", "27B52", previous)
        commands = []
        with patch.object(controller, "host_is_busy", return_value=busy), \
             patch.object(controller, "report", return_value={"selectedMatches": True, "selected": selected}), \
             patch.object(controller, "prune", return_value=["/Applications/Xcode_26.3.app"]) as prune, \
             patch.object(controller, "run", side_effect=lambda argv, **kwargs: commands.append((argv, kwargs)) or ""):
            result = controller.retire(self.pin, self.state)
        return result, prune, commands

    def test_retire_prunes_to_retain_and_runs_the_image_pruner_with_the_small_environment(self):
        (result, removed), prune, commands = self.retire()
        self.assertEqual((result, removed), ("retired", ["/Applications/Xcode_26.3.app"]))
        # No migration-window protection: the previous app is not passed in.
        prune.assert_called_once_with("27.2", "27B52", 1)
        self.assertEqual([argv for argv, _ in commands], [[str(self.pruner), "--apply"]])
        self.assertNotIn("env", commands[0][1])  # run's default allowlist, no GitHub auth
        self.assertIsNotNone(controller.promotion(self.state, "27.2", "27B52")["retiredAt"])
        self.assertFalse(controller.in_window(self.state, "27.2", "27B52"))

    def test_host_turning_busy_during_retire_defers_without_images_or_stamp(self):
        # Busy at prune time though idle at the first check: a job started between them.
        controller.record_promotion(self.state, "27.2", "27B52", "/Applications/Xcode_26.3.app/Contents/Developer")
        with patch.object(controller, "host_is_busy", side_effect=[False, True]), \
             patch.object(controller, "report", return_value={"selectedMatches": True, "selected": "/Applications/Xcode_27.2.app/Contents/Developer"}), \
             patch.object(controller, "root_helper") as helper, patch.object(controller, "run") as run:
            self.assertEqual(controller.retire(self.pin, self.state), ("deferred-busy", []))
        helper.assert_not_called()
        run.assert_not_called()
        self.assertTrue(controller.in_window(self.state, "27.2", "27B52"))

    def test_busy_host_retires_nothing(self):
        (result, removed), prune, commands = self.retire(busy=True)
        self.assertEqual((result, removed), ("deferred-busy", []))
        prune.assert_not_called()
        self.assertEqual(commands, [])
        self.assertTrue(controller.in_window(self.state, "27.2", "27B52"))

    def test_retire_refuses_when_the_previous_app_is_selected(self):
        with self.assertRaisesRegex(controller.UpdateError, "previous Xcode is the selected one"):
            self.retire(previous="/Applications/Xcode_27.2.app/Contents/Developer")

    def test_retire_refuses_without_a_promotion_record(self):
        with self.assertRaisesRegex(controller.UpdateError, "no promotion record"):
            self.retire(record=False)

    def test_phone_gate_retire_runs_no_image_pruner(self):
        self.pin.role, self.pin.ci = "phone-gate", None
        (result, _), _, commands = self.retire()
        self.assertEqual((result, commands), ("retired", []))

    def test_default_pruner_lives_in_host_setup(self):
        pin = SimpleNamespace(ci={}, paths={"hostSetup": "/Users/zoe/code/agent-host-setup"})
        self.assertEqual(controller.image_pruner(pin), "/Users/zoe/code/agent-host-setup/ci-vm/prune-images.mjs")


if __name__ == "__main__":
    unittest.main()
