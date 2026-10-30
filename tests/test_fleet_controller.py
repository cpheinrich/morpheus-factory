import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fleet"))
import controller
import with_github


class FleetControllerTests(unittest.TestCase):
    def test_pin_requires_exact_version_and_build(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "updates.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"zoe-mac-mini": {"xcode": {"version": "27.2", "build": "27B52", "phase": "stage"}}}}))
            self.assertEqual(controller.read_pin(manifest, "zoe-mac-mini"), controller.Pin("27.2", "27B52", "stage"))
            manifest.write_text(manifest.read_text().replace("27B52", "27B52;sh"))
            with self.assertRaisesRegex(controller.FleetError, "invalid exact Xcode pin"):
                controller.read_pin(manifest, "zoe-mac-mini")

    def test_consumer_must_have_merged_workflow_and_build_pins(self):
        pin = controller.Pin("27.2", "27B52", "stage")
        files = {path: "runner: xcode-27.2\n" + "xcode-version: '27.2'\n" * count + ("prepare-pinned-xcode-tests.sh\n" if path.endswith(("ios-full-verification.yml", "ios-nightly-build.yml", "ios-production-release.yml")) else "") + ("assert-xcode-build.sh --require-final\n" if path.endswith(("ios-nightly-build.yml", "ios-production-release.yml")) else "") for path, count in controller.EVO_WORKFLOWS.items()}
        files["apps/ios/scripts/ci/assert-xcode-build.sh"] = "Xcode 27.2\nBuild version 27B52"
        files["apps/ios/scripts/ci/prepare-pinned-xcode-tests.sh"] = 'bash "$script_directory/assert-xcode-build.sh" --require-final'
        files["apps/ios/scripts/ci/prepare-unit-tests.sh"] = 'bash "$script_directory/assert-xcode-build.sh" --require-final'
        files["apps/ios/scripts/ci/prepare-tests.sh"] = "shared test preparation"
        files[".github/workflows/cd-v2-delivery.yml"] = "pre-test-script: apps/ios/scripts/ci/prepare-tests.sh"
        with patch.object(controller, "command") as command, patch.object(controller, "git_file", side_effect=lambda repo, ref, path: files[path]):
            self.assertTrue(controller.evo_has_pin(Path("/evo"), pin))
            self.assertEqual(command.call_args.args[0], ["git", "-C", "/evo", "fetch", "origin", "main"])
            files["apps/ios/scripts/ci/assert-xcode-build.sh"] = "Xcode 27.2\nBuild version 27B48"
            self.assertFalse(controller.evo_has_pin(Path("/evo"), pin))
            files["apps/ios/scripts/ci/assert-xcode-build.sh"] = "Xcode 27.2\nBuild version 27B52"
            files[".github/workflows/ios-nightly-build.yml"] += "xcode-version: '26.6'\n"
            self.assertFalse(controller.evo_has_pin(Path("/evo"), pin))
            files[".github/workflows/ios-nightly-build.yml"] = files[".github/workflows/ios-nightly-build.yml"].replace("xcode-version: '26.6'\n", "")
            files.pop("apps/ios/scripts/ci/assert-xcode-build.sh")
            with patch.object(controller, "git_file", side_effect=lambda repo, ref, path: files[path] if path in files else (_ for _ in ()).throw(controller.FleetError("missing"))):
                self.assertFalse(controller.evo_has_pin(Path("/evo"), pin))

    def test_stage_gate_precedes_image_and_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "updates.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"zoe-mac-mini": {"xcode": {"version": "27.2", "build": "27B52", "phase": "stage"}}}}))
            with patch.object(controller, "host_status", return_value={"installedMatches": False, "selectedMatches": False}), \
                 patch.object(controller, "host_action") as host_action, patch.object(controller, "run_agent") as agent, \
                 patch.object(controller, "sync_host_tools") as sync:
                self.assertEqual(controller.reconcile(root, root, root, manifest, "zoe-mac-mini", root), "waiting-host-stage")
                self.assertEqual(host_action.call_args.args[-1], "stage")
                agent.assert_not_called()
                sync.assert_not_called()

    def test_consumer_merge_precedes_promotion_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "updates.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"zoe-mac-mini": {"xcode": {"version": "27.2", "build": "27B52", "phase": "stage"}}}}))
            called = []
            with patch.object(controller, "host_status", return_value={"installedMatches": True, "selectedMatches": False}), \
                 patch.object(controller, "host_action", side_effect=lambda factory, evo, manifest, host, action: called.append(action)), \
                 patch.object(controller, "evo_has_pin", side_effect=[False, True]), \
                 patch.object(controller, "sync_host_tools", side_effect=lambda path: called.append("sync-tools")), \
                 patch.object(controller, "run_agent", side_effect=lambda step, *_: called.append(step)):
                self.assertEqual(controller.reconcile(root, root, root, manifest, "zoe-mac-mini", root), "waiting-promotion-merge")
            self.assertEqual(called[0], "sync-tools")
            self.assertEqual(called[1], "image-stage")
            self.assertEqual(called[2:], ["consumer", "promote"])

    def test_early_promote_phase_cannot_switch_before_consumer_merge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "updates.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"zoe-mac-mini": {"xcode": {"version": "27.2", "build": "27B52", "phase": "promote"}}}}))
            actions = []
            with patch.object(controller, "host_status", return_value={"installedMatches": True, "selectedMatches": False}), \
                 patch.object(controller, "sync_host_tools"), \
                 patch.object(controller, "host_action", side_effect=lambda factory, evo, manifest, host, action: actions.append(action)), \
                 patch.object(controller, "evo_has_pin", side_effect=[False, False]), \
                 patch.object(controller, "run_agent"):
                self.assertEqual(controller.reconcile(root, root, root, manifest, "zoe-mac-mini", root), "waiting-consumer-merge")
            self.assertEqual(actions, ["image-stage"])

    def test_host_promotes_only_after_merged_consumer_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "updates.json"
            manifest.write_text(json.dumps({"schemaVersion": 1, "hosts": {"zoe-mac-mini": {"xcode": {"version": "27.2", "build": "27B52", "phase": "promote"}}}}))
            actions = []
            with patch.object(controller, "host_status", side_effect=[{"installedMatches": True, "selectedMatches": False}, {"installedMatches": True, "selectedMatches": True}]), \
                 patch.object(controller, "sync_host_tools"), \
                 patch.object(controller, "host_action", side_effect=lambda factory, evo, manifest, host, action: actions.append(action)), \
                 patch.object(controller, "evo_has_pin", return_value=True), \
                 patch.object(controller, "run_agent") as agent:
                self.assertEqual(controller.reconcile(root, root, root, manifest, "zoe-mac-mini", root), "host-converged")
                agent.assert_not_called()
            self.assertEqual(actions, ["image-stage", "reconcile"])

    def test_host_action_does_not_inherit_github_token(self):
        with patch.dict(os.environ, {"GH_TOKEN": "secret", "FACTORY_GH_TOKEN": "secret"}), patch.object(controller, "command") as command:
            controller.host_action(Path("/factory"), Path("/evo"), Path("/factory/worker/updates.json"), "zoe-mac-mini", "stage")
            self.assertNotIn("GH_TOKEN", command.call_args.kwargs["env"])
            self.assertNotIn("FACTORY_GH_TOKEN", command.call_args.kwargs["env"])
            controller.host_action(Path("/factory"), Path("/evo"), Path("/factory/worker/updates.json"), "zoe-mac-mini", "image-stage")
            self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], "secret")

    def test_dirty_host_tools_block_image_stage(self):
        with patch.object(controller, "command", side_effect=["main", " M ci-vm/build-image.sh"]):
            with self.assertRaisesRegex(controller.FleetError, "dirty"):
                controller.sync_host_tools(Path("/host-tools"))

    def test_state_resets_on_new_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.json"
            controller.save_state(path, {"pin": "27.2-27B52", "sessions": {"consumer": "old"}})
            self.assertEqual(controller.private_state(path, controller.Pin("27.3", "27C1", "stage")), {"pin": "27.3-27C1", "sessions": {}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_agent_session_is_recorded_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shim = root / "codex"
            argv_log = root / "argv.json"
            shim.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))\nprint(json.dumps({'type':'thread.started','thread_id':'00000000-0000-0000-0000-000000000001'}))\nprint(json.dumps({'type':'turn.completed'}))\n")
            shim.chmod(0o755)
            # The wrapper resolves the shim without invoking a real model.
            real_popen = controller.subprocess.Popen
            def fake_popen(argv, **kwargs):
                return real_popen([str(shim), str(argv_log), *argv[1:]], **kwargs)
            state = {"pin": "27.2-27B52", "sessions": {}}
            with patch.object(controller.subprocess, "Popen", side_effect=fake_popen):
                controller.run_agent("consumer", controller.Pin("27.2", "27B52", "stage"), root, root, root, state, root / "state.json")
            self.assertEqual(state["sessions"]["consumer"], "00000000-0000-0000-0000-000000000001")
            self.assertEqual(json.loads((root / "state.json").read_text())["sessions"]["consumer"], state["sessions"]["consumer"])

    def test_github_wrapper_scopes_credential_environment(self):
        class Result:
            returncode = 0
            stdout = "example-token\n"
        seen = []
        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return Result()
        with patch.object(sys, "argv", ["with_github.py", "--user", "cpheinrich", "--", "python3", "fleet/controller.py"]), \
             patch.dict(os.environ, {"ZOE_APPLE_ID_PASSWORD": "must-not-pass", "FACTORY_VERIFICATION_CODE_TOKEN": "must-not-pass"}), \
             patch.object(with_github.subprocess, "run", side_effect=fake_run):
            self.assertEqual(with_github.main(), 0)
        self.assertEqual(seen[0][0], ["gh", "auth", "token", "-u", "cpheinrich"])
        child_env = seen[1][1]["env"]
        self.assertEqual(child_env["GH_TOKEN"], "example-token")
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", child_env)
        self.assertNotIn("FACTORY_VERIFICATION_CODE_TOKEN", child_env)


if __name__ == "__main__":
    unittest.main()
