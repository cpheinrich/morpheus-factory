import importlib.util
import json
import os
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
        with self.assertRaisesRegex(controller.UpdateError, "exact numeric"):
            controller.pin_for_host(self.manifest(xcode={"version": "latest", "build": "27B52"}), "mini")
        with self.assertRaisesRegex(controller.UpdateError, "absolute path"):
            controller.pin_for_host(self.manifest(validation=[{"argv": ["sh", "-c", "true"]}]), "mini")

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

    def test_promotion_validates_before_switch(self):
        calls = []
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage", side_effect=lambda *a: calls.append("stage")), \
             patch.object(controller, "validate", side_effect=lambda *a: calls.append("validate")), \
             patch.object(controller, "activate", side_effect=lambda *a: calls.append("activate")):
            result = controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        self.assertEqual(result, "promoted")
        self.assertEqual(calls, ["stage", "validate", "activate"])

    def test_failed_validation_does_not_switch(self):
        with patch.object(controller, "report", return_value={"selected": "/Applications/Xcode.app/Contents/Developer", "selectedMatches": False}), \
             patch.object(controller, "stage"), \
             patch.object(controller, "validate", side_effect=controller.UpdateError("failed")), \
             patch.object(controller, "activate") as activate:
            with self.assertRaises(controller.UpdateError):
                controller.reconcile("27.2", "27B52", [], "promote", Path("/tmp/state"))
        activate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
