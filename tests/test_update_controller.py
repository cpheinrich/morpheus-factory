import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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
        self.assertEqual(controller.pin_for_host(self.manifest(), "mini")[:2], ("27.2", "27B52"))
        self.assertEqual(controller.pin_for_host(self.manifest(xcode={"version": "27.2", "build": "27B52", "phase": "promote"}), "mini")[3], "promote")
        with self.assertRaisesRegex(controller.UpdateError, "exact numeric"):
            controller.pin_for_host(self.manifest(xcode={"version": "latest", "build": "27B52"}), "mini")
        with self.assertRaisesRegex(controller.UpdateError, "absolute path"):
            controller.pin_for_host(self.manifest(validation=[{"argv": ["sh", "-c", "true"]}]), "mini")
        with self.assertRaisesRegex(controller.UpdateError, "components must be named"):
            controller.pin_for_host(self.manifest(xcode={"version": "27.2", "build": "27B52", "components": ["../../metal"]}), "mini")

    def test_xcodes_gets_only_apple_credentials(self):
        with patch.dict(os.environ, {"ZOE_APPLE_ID": "user@example.test", "ZOE_APPLE_ID_PASSWORD": "secret", "ASC_ISSUER_ID": "must-not-pass"}):
            env = controller.xcodes_environment()
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
            self.assertEqual(controller.interactive_login(), "apple-authenticated")
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
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage", side_effect=lambda *a: calls.append("stage")), \
             patch.object(controller, "validate", side_effect=lambda *a: calls.append("validate")), \
             patch.object(controller, "activate", side_effect=lambda *a: calls.append("activate")):
            result = controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertEqual(result, "promoted")
        self.assertEqual(calls, ["stage", "validate", "activate"])

    def test_already_selected_pin_does_not_rebuild_each_hour(self):
        with patch.object(controller, "report", return_value={"selectedMatches": True}), \
             patch.object(controller, "stage") as stage, \
             patch.object(controller, "validate") as validate, \
             patch.object(controller, "activate") as activate:
            result = controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertEqual(result, "already-current")
        stage.assert_called_once()
        validate.assert_not_called()
        activate.assert_not_called()

    def test_failed_validation_does_not_switch(self):
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
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
            if argv == ["/usr/bin/sudo", "-n", "/usr/bin/xcode-select", "--switch", previous]:
                return ""
            raise AssertionError(argv)
        with patch.object(controller, "report", return_value={"selected": previous, "selectedMatches": False}), \
             patch.object(controller, "stage"), patch.object(controller, "validate"), \
             patch.object(controller, "activate", side_effect=controller.UpdateError("post-switch check failed")), \
             patch.object(controller, "run", side_effect=command) as run:
            with self.assertRaisesRegex(controller.UpdateError, "post-switch check failed"):
                controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        run.assert_any_call(["/usr/bin/sudo", "-n", "/usr/bin/xcode-select", "--switch", previous])

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

        with patch.dict(os.environ, {"ZOE_APPLE_ID": "user@example.test", "ZOE_APPLE_ID_PASSWORD": "secret", "FACTORY_VERIFICATION_CODE_COMMAND": "/usr/bin/true"}), \
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


if __name__ == "__main__":
    unittest.main()
