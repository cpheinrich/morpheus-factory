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


PIN = controller.Pin("27.2", "27B52", "promote")
CONSUMER = controller.Consumer()
ASSERTION = "apps/ios/scripts/ci/assert-xcode-build.sh"
CI = {"repository": "darwin-health/evo", "image": "ci-xcode-27.2", "label": "xcode-27.2"}


# Evo's apps/ios/scripts/ci/assert-xcode-build.sh, copied verbatim.
REAL_ASSERTION = r'''#!/bin/bash
# A version match alone can admit a release candidate with a different build.
set -euo pipefail
mode="${1:-}"
[[ -z "$mode" || "$mode" == --require-final ]] || { echo "unknown assertion mode" >&2; exit 2; }
developer="${DEVELOPER_DIR:-}"
if [[ -z "$developer" ]]; then
  if [[ -d /Applications/Xcode_27.2.app ]]; then
    developer=/Applications/Xcode_27.2.app/Contents/Developer
  else
    developer=/Applications/Xcode.app/Contents/Developer
  fi
fi
identity="$("$developer/usr/bin/xcodebuild" -version)"
if [[ "$mode" == --require-final || "$identity" == Xcode\ 27.2$'\n'* ]]; then
  [[ "$identity" == $'Xcode 27.2\nBuild version 27B52' ]] || {
    echo "Expected final Xcode 27.2 build 27B52, got a different toolchain" >&2
    exit 1
  }
fi
'''


def write_manifest(root: Path, entry: dict, **top) -> Path:
    manifest = root / "updates.json"
    manifest.write_text(json.dumps({"schemaVersion": 1, **top, "hosts": {"zoe-mac-mini": entry}}))
    return manifest


def pinned_files():
    return {
        ".github/workflows/ios-delivery.yml": "jobs:\n  test:\n    runs-on: [self-hosted, xcode-27.2]\n    steps:\n      - uses: ./setup\n        with:\n          xcode-version: '27.2'\n",
        ".github/workflows/ios-ci.yml": "jobs:\n  unit:\n    runs-on: xcode-27.2\n    env:\n      runner: xcode-27.2\n",
        ".github/workflows/web.yml": "jobs:\n  lint:\n    runs-on: ubuntu-latest\n",
        ASSERTION: 'expected="Xcode 27.2"\nbuild="Build version 27B52"\n',
    }


class FakeRepo:
    """Answers the controller's git and gh calls from an in-memory origin/main."""

    def __init__(self, files, prs=None, remote_branch=""):
        self.files, self.prs, self.remote_branch, self.calls = files, prs or [], remote_branch, []

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ["gh", "pr"] and argv[2] == "list":
            return json.dumps(self.prs)
        if argv[:2] == ["gh", "pr"] and argv[2] == "create":
            return "https://github.com/darwin-health/evo/pull/700"
        if "ls-tree" in argv:
            return "\n".join(path for path in self.files if path.startswith(".github/workflows/"))
        if "ls-remote" in argv:
            return self.remote_branch
        if "worktree" in argv and "add" in argv:
            worktree = Path(argv[argv.index("--detach") + 1])
            for path, body in self.files.items():
                (worktree / path).parent.mkdir(parents=True, exist_ok=True)
                (worktree / path).write_text(body)
            (worktree / "untracked.yml").write_text("xcode-version: '26.6'\n")
            return ""
        if "ls-files" in argv:
            return "\n".join(self.files)
        if "add" in argv and "--" in argv:
            worktree = Path(argv[2])
            self.added = {path: (worktree / path).read_text() for path in argv[argv.index("--") + 1:]}
        return ""

    def git_file(self, repo, ref, path):
        if path not in self.files:
            raise controller.FleetError("missing")
        return self.files[path]

    def patches(self):
        return patch.object(controller, "command", side_effect=self.command), patch.object(controller, "git_file", side_effect=self.git_file)


class ReadHostTests(unittest.TestCase):
    def test_pin_requires_exact_version_and_build(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), {"xcode": {"version": "27.2", "build": "27B52", "phase": "stage"}})
            self.assertEqual(controller.read_pin(manifest, "zoe-mac-mini"), controller.Pin("27.2", "27B52", "stage"))
            manifest.write_text(manifest.read_text().replace("27B52", "27B52;sh"))
            with self.assertRaisesRegex(controller.FleetError, "invalid exact Xcode pin"):
                controller.read_pin(manifest, "zoe-mac-mini")

    def test_defaults_without_consumer_or_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = write_manifest(Path(temp), {"xcode": {"version": "27.2", "build": "27B52"}})
            config = controller.read_host(manifest, "zoe-mac-mini")
            self.assertEqual(config.pin, controller.Pin("27.2", "27B52", "promote"))
            self.assertEqual(config.role, "phone-gate")
            self.assertFalse(config.has_ci)
            self.assertEqual(config.paths, {})
            self.assertEqual(config.consumer, controller.Consumer("darwin-health/evo", ASSERTION, (".github/workflows/*.yml", ASSERTION)))

    def test_reads_consumer_paths_and_role(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = {"factory": "/f", "consumer": "/c", "hostSetup": "/h", "credentialsProject": "/k"}
            manifest = write_manifest(Path(temp), {"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI, "paths": paths},
                                      consumer={"repository": "acme/app", "assertion": "ci/assert.sh", "pinPaths": ["ci/*.yml"]})
            config = controller.read_host(manifest, "zoe-mac-mini")
            self.assertEqual(config.role, "ci-image")
            self.assertTrue(config.has_ci)
            self.assertEqual(config.paths, paths)
            self.assertEqual(config.consumer, controller.Consumer("acme/app", "ci/assert.sh", ("ci/*.yml",)))

    def test_rejects_bad_consumer_and_role(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xcode = {"xcode": {"version": "27.2", "build": "27B52"}}
            for consumer, message in (({"repository": "evo"}, "owner/name"), ({"assertion": "../x.sh"}, "relative"),
                                      ({"pinPaths": ["/etc/*.yml"]}, "relative"), ({"pinPaths": []}, "non-empty")):
                with self.assertRaisesRegex(controller.FleetError, message):
                    controller.read_host(write_manifest(root, xcode, consumer=consumer), "zoe-mac-mini")
            with self.assertRaisesRegex(controller.FleetError, "host role"):
                controller.read_host(write_manifest(root, {**xcode, "role": "ci-image"}), "zoe-mac-mini")
            with self.assertRaisesRegex(controller.FleetError, "host paths"):
                controller.read_host(write_manifest(root, {**xcode, "paths": {"consumer": "code/evo"}}), "zoe-mac-mini")

    def test_cli_paths_override_manifest_then_defaults(self):
        config = controller.HostConfig(PIN, "ci-image", True, paths={"consumer": "/m/evo", "hostSetup": "/m/setup"})
        args = type("Args", (), {"consumer_root": None, "host_setup_root": Path("/cli/setup"), "credentials_project": None})()
        self.assertEqual(controller.resolve_paths(args, config), (Path("/m/evo"), Path("/cli/setup"), Path("/m/evo")))
        bare = controller.HostConfig(PIN, "phone-gate", False)
        self.assertEqual(controller.resolve_paths(args, bare)[0], (Path.home() / "code/evo").resolve())


class ConsumerPinTests(unittest.TestCase):
    def check(self, files):
        repo = FakeRepo(files)
        command, git_file = repo.patches()
        with command, git_file:
            result = controller.consumer_has_pin(Path("/evo"), PIN, CONSUMER)
        return result, repo

    def test_passes_with_only_delivery_and_ci_workflows(self):
        result, repo = self.check(pinned_files())
        self.assertIs(result, True)
        self.assertEqual(repo.calls[0], ["git", "-C", "/evo", "fetch", "origin", "main"])
        self.assertEqual(repo.calls[1], ["git", "-C", "/evo", "ls-tree", "--name-only", "origin/main", ".github/workflows/"])

    def test_other_xcode_version_fails(self):
        files = pinned_files()
        files[".github/workflows/web.yml"] += "      xcode-version: \"26.6\"\n"
        self.assertIs(self.check(files)[0], False)

    def test_stale_runner_label_fails(self):
        files = pinned_files()
        files[".github/workflows/ios-ci.yml"] = files[".github/workflows/ios-ci.yml"].replace("runs-on: xcode-27.2", "runs-on: xcode-26.6")
        self.assertIs(self.check(files)[0], False)
        files = pinned_files()
        files[".github/workflows/ios-delivery.yml"] = files[".github/workflows/ios-delivery.yml"].replace("[self-hosted, xcode-27.2]", "[self-hosted, xcode-26.6]")
        self.assertIs(self.check(files)[0], False)

    def test_wrong_assertion_build_fails(self):
        files = pinned_files()
        files[ASSERTION] = files[ASSERTION].replace("27B52", "27B48")
        self.assertIs(self.check(files)[0], False)
        files.pop(ASSERTION)
        self.assertIs(self.check(files)[0], False)

    def test_ignored_transitional_lane_is_not_judged(self):
        files = pinned_files()
        files[".github/workflows/cd-v2-delivery.yml"] = "jobs:\n  build:\n    runs-on: macos-vm\n    with:\n      xcode-version: '26.6'\n"
        self.assertIs(self.check(files)[0], False)
        consumer = controller.Consumer(ignore_paths=(".github/workflows/cd-v2-*.yml",))
        repo = FakeRepo(files)
        command, git_file = repo.patches()
        with command, git_file:
            self.assertIs(controller.consumer_has_pin(Path("/evo"), PIN, consumer), True)

    def test_real_assertion_shape_with_literal_newline_escape(self):
        # Evo's script compares against $'Xcode 27.2\nBuild version 27B52': "\n" is two literal characters.
        files = pinned_files()
        files[ASSERTION] = "[[ \"$identity\" == $'Xcode 27.2\\nBuild version 27B52' ]] || exit 1\n"
        self.assertIs(self.check(files)[0], True)
        self.assertEqual(controller.assertion_pin(files[ASSERTION]), ("27.2", "27B52"))
        rewritten = controller.substitute_pin(files[ASSERTION], ("27.2", "27B52"), controller.Pin("27.3", "27C5", "promote"))
        self.assertEqual(rewritten, "[[ \"$identity\" == $'Xcode 27.3\\nBuild version 27C5' ]] || exit 1\n")

    def test_real_assertion_script_passes_only_when_every_token_matches(self):
        files = pinned_files()
        files[ASSERTION] = REAL_ASSERTION
        self.assertIs(self.check(files)[0], True)
        # One stale line beside updated ones is not pinned.
        files[ASSERTION] = REAL_ASSERTION.replace("Xcode\\ 27.2", "Xcode\\ 26.6")
        self.assertNotEqual(files[ASSERTION], REAL_ASSERTION)
        self.assertIs(self.check(files)[0], False)
        # A substring of the pin is not the pin.
        files[ASSERTION] = REAL_ASSERTION.replace("27.2", "27.21")
        self.assertIs(self.check(files)[0], False)
        self.assertIs(self.check({**pinned_files(), ASSERTION: "echo Xcode 27.2\n"})[0], False)

    def test_real_assertion_script_is_fully_rewritten(self):
        new = controller.Pin("27.3", "27C5", "promote")
        old = controller.assertion_pin(REAL_ASSERTION)
        self.assertEqual(old, ("27.2", "27B52"))
        rewritten = controller.substitute_pin(REAL_ASSERTION, old, new)
        self.assertIn('"$identity" == Xcode\\ 27.3$\'\\n\'*', rewritten)
        self.assertIn("$'Xcode 27.3\\nBuild version 27C5'", rewritten)
        self.assertIn("/Applications/Xcode_27.3.app", rewritten)
        self.assertIn("Expected final Xcode 27.3 build 27C5", rewritten)
        self.assertNotIn("27.2", rewritten)
        self.assertNotIn("27B52", rewritten)
        self.assertFalse(controller.residual_pin(rewritten, old, new))
        repo = FakeRepo({**pinned_files(), ASSERTION: rewritten})
        command, git_file = repo.patches()
        with command, git_file:
            self.assertIs(controller.consumer_has_pin(Path("/evo"), new, CONSUMER), False)  # workflows still say 27.2
            self.assertIs(controller.consumer_has_pin(Path("/evo"), PIN, CONSUMER), False)  # assertion says 27.3

    def test_no_workflow_mentions_xcode_version_fails(self):
        files = pinned_files()
        files[".github/workflows/ios-delivery.yml"] = "jobs:\n  test:\n    runs-on: xcode-27.2\n"
        self.assertIs(self.check(files)[0], False)

    def test_controller_names_no_retired_workflows(self):
        source = (Path(controller.__file__)).read_text()
        for retired in ("cd-v2", "nightly", "production-release", "prepare-"):
            self.assertNotIn(retired, source)


class SubstitutionTests(unittest.TestCase):
    def test_rewrites_every_pattern(self):
        old = ("26.6", "26F12")
        text = ("xcode-version: '26.6'\nxcode-version: \"26.6\"\nruns-on: [self-hosted, xcode-26.6]\n"
                "path: /Applications/Xcode_26.6.app\necho Xcode 26.6\nBuild version 26F12\nBUILD=26F12\n"
                "keep: 26.61 xcode-26.6.1 Xcode 26.6.1 26F123\n")
        self.assertEqual(controller.substitute_pin(text, old, PIN),
                         "xcode-version: '27.2'\nxcode-version: \"27.2\"\nruns-on: [self-hosted, xcode-27.2]\n"
                         "path: /Applications/Xcode_27.2.app\necho Xcode 27.2\nBuild version 27B52\nBUILD=27B52\n"
                         "keep: 26.61 xcode-26.6.1 Xcode 26.6.1 26F123\n")

    def test_assertion_pin_reads_old_values_or_refuses(self):
        self.assertEqual(controller.assertion_pin('expected="Xcode 26.6"\nbuild="Build version 26F12"\n'), ("26.6", "26F12"))
        with self.assertRaisesRegex(controller.FleetError, "cannot read"):
            controller.assertion_pin("echo checking xcode\n")

    def test_refuses_when_old_pin_survives_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.yml").write_text("xcode-version: '26.6'\nDEVELOPER_DIR: /Applications/Xcode-26.6.app\n")
            with self.assertRaisesRegex(controller.FleetError, "left the old Xcode pin in a.yml"):
                controller.rewrite_pin_files(root, {"a.yml"}, ("*.yml",), ("26.6", "26F12"), PIN)
            (root / "a.yml").write_text("xcode-version: '26.6'\nid: build-26F12\n")
            self.assertEqual(controller.rewrite_pin_files(root, {"a.yml"}, ("*.yml",), ("26.6", "26F12"), PIN), ["a.yml"])
            (root / "a.yml").write_text("xcode-version: '26.6'\nid: build_26F12\n")
            with self.assertRaisesRegex(controller.FleetError, "left the old Xcode pin"):
                controller.rewrite_pin_files(root, {"a.yml"}, ("*.yml",), ("26.6", "26F12"), PIN)

    def test_build_only_bump_keeps_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "assert.sh").write_text("Xcode 27.2\nBuild version 27B48\n")
            self.assertEqual(controller.rewrite_pin_files(root, {"assert.sh"}, ("*.sh",), ("27.2", "27B48"), PIN), ["assert.sh"])
            self.assertEqual((root / "assert.sh").read_text(), "Xcode 27.2\nBuild version 27B52\n")

    def test_refuses_when_nothing_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.yml").write_text("runs-on: ubuntu-latest\n")
            with self.assertRaisesRegex(controller.FleetError, "changed nothing"):
                controller.rewrite_pin_files(root, {"a.yml"}, ("*.yml",), ("26.6", "26F12"), PIN)

    def test_ignored_files_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.yml").write_text("xcode-version: '26.6'\n")
            (root / "cd-v2-delivery.yml").write_text("xcode-version: '26.6'\n")
            ignores = controller.Consumer(ignore_paths=("cd-v2-*.yml",)).ignores
            self.assertEqual(controller.rewrite_pin_files(root, {"a.yml", "cd-v2-delivery.yml"}, ("*.yml",), ("26.6", "26F12"), PIN, ignores), ["a.yml"])
            self.assertEqual((root / "cd-v2-delivery.yml").read_text(), "xcode-version: '26.6'\n")

    def test_only_tracked_files_are_rewritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "a.yml").write_text("xcode-version: '26.6'\n")
            (root / "b.yml").write_text("xcode-version: '26.6'\n")
            self.assertEqual(controller.rewrite_pin_files(root, {"a.yml"}, ("*.yml",), ("26.6", "26F12"), PIN), ["a.yml"])
            self.assertEqual((root / "b.yml").read_text(), "xcode-version: '26.6'\n")


def old_files():
    return {path: body.replace("27.2", "26.6").replace("27B52", "26F12") for path, body in pinned_files().items()}


class ConsumerPrTests(unittest.TestCase):
    def test_opens_draft_pr_once_with_substitution(self):
        repo = FakeRepo(old_files())
        command, git_file = repo.patches()
        with command, git_file:
            url, opened = controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER)
        self.assertEqual((url, opened), ("https://github.com/darwin-health/evo/pull/700", "opened"))
        self.assertEqual(sorted(repo.added), [".github/workflows/ios-ci.yml", ".github/workflows/ios-delivery.yml", ASSERTION])
        self.assertEqual(repo.added[ASSERTION], 'expected="Xcode 27.2"\nbuild="Build version 27B52"\n')
        worktree = next(call for call in repo.calls if "worktree" in call and "add" in call)
        self.assertEqual(worktree[:5], ["git", "-C", "/evo", "worktree", "add"])
        self.assertEqual(worktree[-1], "origin/main")
        self.assertIn(["git", "-C", worktree[6], "checkout", "-B", "factory/xcode-27.2-27B52"], repo.calls)
        commit = next(call for call in repo.calls if "commit" in call)
        self.assertEqual(commit[-3:], ["commit", "-m", "Pin iOS CI to Xcode 27.2 (build 27B52)"])
        self.assertIn("user.name=morpheus-factory", commit)
        self.assertIn("user.email=noreply@github.com", commit)
        push = next(call for call in repo.calls if "push" in call)
        self.assertEqual(push[-4:], ["push", "-u", "origin", "factory/xcode-27.2-27B52"])
        create = next(call for call in repo.calls if call[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[3:11], ["--repo", "darwin-health/evo", "--draft", "--head", "factory/xcode-27.2-27B52", "--base", "main", "--title"])
        body = create[create.index("--body") + 1]
        self.assertIn("`.github/workflows/ios-ci.yml`", body)
        self.assertTrue(body.rstrip().endswith("🤖 Generated with Factory"))
        remove = next(call for call in repo.calls if "remove" in call)
        self.assertFalse(Path(remove[-1]).exists())

    def test_existing_pr_is_reused(self):
        repo = FakeRepo(old_files(), prs=[{"number": 7, "url": "https://github.com/darwin-health/evo/pull/7", "state": "OPEN", "isDraft": True, "mergedAt": None}])
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/7", "open"))
        self.assertEqual(repo.calls, [["gh", "pr", "list", "--repo", "darwin-health/evo", "--head", "factory/xcode-27.2-27B52",
                                       "--state", "all", "--json", "number,url,state,isDraft,mergedAt"]])

    def test_closed_pr_is_never_recreated(self):
        closed = {"number": 7, "url": "https://github.com/darwin-health/evo/pull/7", "state": "CLOSED", "isDraft": True, "mergedAt": None}
        repo = FakeRepo(old_files(), prs=[closed], remote_branch="abc123\trefs/heads/factory/xcode-27.2-27B52")
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/7", "closed"))
        self.assertFalse(any(call[:3] == ["gh", "pr", "create"] or "push" in call or "worktree" in call for call in repo.calls))

    def test_merged_pr_falls_through_to_normal_path(self):
        merged = {"number": 6, "url": "https://github.com/darwin-health/evo/pull/6", "state": "MERGED", "isDraft": False, "mergedAt": "2026-10-29T00:00:00Z"}
        repo = FakeRepo(old_files(), prs=[merged])
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/700", "opened"))

    def test_merged_pr_with_surviving_branch_waits_for_human(self):
        merged = {"number": 6, "url": "https://github.com/darwin-health/evo/pull/6", "state": "MERGED", "isDraft": False, "mergedAt": "2026-10-29T00:00:00Z"}
        older_closed = {"number": 5, "url": "https://github.com/darwin-health/evo/pull/5", "state": "CLOSED", "isDraft": True, "mergedAt": None}
        repo = FakeRepo(old_files(), prs=[older_closed, merged], remote_branch="abc123\trefs/heads/factory/xcode-27.2-27B52")
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/6", "merged"))
        self.assertFalse(any(call[:3] == ["gh", "pr", "create"] or "push" in call for call in repo.calls))

    def test_newest_closed_pr_wins_over_older_merged(self):
        merged = {"number": 5, "url": "https://github.com/darwin-health/evo/pull/5", "state": "MERGED", "isDraft": False, "mergedAt": "2026-10-28T00:00:00Z"}
        closed = {"number": 6, "url": "https://github.com/darwin-health/evo/pull/6", "state": "CLOSED", "isDraft": True, "mergedAt": None}
        repo = FakeRepo(old_files(), prs=[merged, closed], remote_branch="abc123\trefs/heads/factory/xcode-27.2-27B52")
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/6", "closed"))

    def test_closed_pr_with_deleted_branch_is_retried(self):
        # "Delete the branch to retry" must actually work: gh still lists the closed PR afterwards.
        closed = {"number": 7, "url": "https://github.com/darwin-health/evo/pull/7", "state": "CLOSED", "isDraft": True, "mergedAt": None}
        repo = FakeRepo(old_files(), prs=[closed])
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER), ("https://github.com/darwin-health/evo/pull/700", "opened"))
        self.assertTrue(any(call[:3] == ["gh", "pr", "create"] for call in repo.calls))

    def test_existing_branch_is_not_rewritten(self):
        repo = FakeRepo(old_files(), remote_branch="abc123\trefs/heads/factory/xcode-27.2-27B52")
        command, git_file = repo.patches()
        with command, git_file:
            self.assertEqual(controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER)[1], "opened")
        self.assertFalse(any("push" in call or "worktree" in call for call in repo.calls))
        self.assertEqual(repo.calls[-1][:3], ["gh", "pr", "create"])

    def test_worktree_is_removed_when_substitution_fails(self):
        repo = FakeRepo(pinned_files())
        command, git_file = repo.patches()
        with command, git_file, self.assertRaisesRegex(controller.FleetError, "changed nothing"):
            controller.ensure_consumer_pr(Path("/evo"), PIN, CONSUMER)
        remove = next(call for call in repo.calls if "remove" in call)
        self.assertFalse(Path(remove[-1]).exists())
        self.assertFalse(any(call[:3] == ["gh", "pr", "create"] for call in repo.calls))


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def run_reconcile(self, entry, *, status, pinned, pr=("https://github.com/darwin-health/evo/pull/7", "open")):
        manifest = write_manifest(self.root, entry)
        events = []
        with patch.object(controller, "host_status", side_effect=status if isinstance(status, list) else None, return_value=status), \
             patch.object(controller, "host_action", side_effect=lambda factory, creds, manifest, host, action: events.append(action)), \
             patch.object(controller, "sync_host_tools", side_effect=lambda path: events.append("sync-tools")), \
             patch.object(controller, "consumer_has_pin", side_effect=pinned), \
             patch.object(controller, "ensure_consumer_pr", side_effect=lambda *a: (events.append("pr"), pr)[1]), \
             patch.object(controller, "run_agent", side_effect=lambda *a: events.append("agent")):
            result = controller.reconcile(self.root, self.root, self.root, manifest, "zoe-mac-mini", self.root)
        return result, events

    def test_stage_gate_precedes_everything(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status={"installedMatches": False, "selectedMatches": False}, pinned=[])
        self.assertEqual((result, events), ("waiting-host-stage", ["stage"]))

    def test_ci_host_promotion_order(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52", "phase": "promote"}, "ci": CI},
                                            status=[{"installedMatches": True, "selectedMatches": False}, {"installedMatches": True, "selectedMatches": True}],
                                            pinned=[True])
        self.assertEqual((result, events), ("host-converged", ["sync-tools", "image-stage", "reconcile"]))

    def test_phone_gate_skips_tools_and_image(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}},
                                            status=[{"installedMatches": True, "selectedMatches": False}, {"installedMatches": True, "selectedMatches": False}],
                                            pinned=[True])
        self.assertEqual((result, events), ("waiting-host-promotion", ["reconcile"]))

    def test_converged_host_never_touches_the_consumer(self):
        # Evo's V2 lane sits on 26.6 on purpose; a host already on the pin must not open PRs over it.
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52", "phase": "promote"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": True}, pinned=[False])
        self.assertEqual((result, events), ("host-converged", ["sync-tools", "image-stage"]))

    def test_stage_phase_holds_before_consumer_step(self):
        # pinned=[] means any consumer check raises StopIteration: the hold must not reach it.
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52", "phase": "stage"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[])
        self.assertEqual((result, events), ("held-at-stage", ["sync-tools", "image-stage"]))
        self.assertFalse((self.root / "fleet.json").exists())

    def test_reverted_merge_waits_for_human_without_agent(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[False],
                                            pr=("https://github.com/darwin-health/evo/pull/6", "merged"))
        self.assertEqual(result, "waiting-human: consumer pin PR https://github.com/darwin-health/evo/pull/6 merged but main "
                                 "no longer carries the pin; delete branch factory/xcode-27.2-27B52 to retry")
        self.assertEqual(events, ["sync-tools", "image-stage", "pr"])
        self.assertFalse((self.root / "fleet.json").exists())

    def test_closed_pr_waits_for_human_without_agent(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[False],
                                            pr=("https://github.com/darwin-health/evo/pull/7", "closed"))
        self.assertEqual(result, "waiting-human: consumer pin PR https://github.com/darwin-health/evo/pull/7 was closed; "
                                 "delete branch factory/xcode-27.2-27B52 to retry")
        self.assertEqual(events, ["sync-tools", "image-stage", "pr"])

    def test_opened_pr_waits_without_agent(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[False],
                                            pr=("https://github.com/darwin-health/evo/pull/7", "opened"))
        self.assertEqual((result, events), ("waiting-consumer-pr", ["sync-tools", "image-stage", "pr"]))

    def test_existing_pr_runs_one_agent_then_waits_for_merge(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[False, False])
        self.assertEqual((result, events), ("waiting-consumer-merge", ["sync-tools", "image-stage", "pr", "agent"]))
        state = json.loads((self.root / "fleet.json").read_text())
        self.assertEqual(state["pin"], "27.2-27B52")
        self.assertEqual(len(state["agentRuns"]), 1)
        self.assertNotIn("sessions", state)

    def test_agent_cap_refuses_fourth_run(self):
        entry = {"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI}
        status = {"installedMatches": True, "selectedMatches": False}
        for _ in range(3):
            self.assertEqual(self.run_reconcile(entry, status=status, pinned=[False, False])[0], "waiting-consumer-merge")
        result, events = self.run_reconcile(entry, status=status, pinned=[False])
        self.assertEqual(result, "waiting-human: consumer pin PR https://github.com/darwin-health/evo/pull/7 needs attention")
        self.assertNotIn("agent", events)
        self.assertEqual(len(json.loads((self.root / "fleet.json").read_text())["agentRuns"]), 3)

    def test_phone_gate_with_unpinned_consumer_drives_nothing(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}},
                                            status={"installedMatches": True, "selectedMatches": False}, pinned=[False])
        self.assertEqual((result, events), ("waiting-consumer-merge", []))
        self.assertFalse((self.root / "fleet.json").exists())

    def test_agent_that_merges_lets_host_promote(self):
        result, events = self.run_reconcile({"xcode": {"version": "27.2", "build": "27B52"}, "ci": CI},
                                            status=[{"installedMatches": True, "selectedMatches": False}, {"installedMatches": True, "selectedMatches": True}],
                                            pinned=[False, True])
        self.assertEqual((result, events), ("host-converged", ["sync-tools", "image-stage", "pr", "agent", "reconcile"]))

    def test_reconcile_uses_host_credentials_project_not_consumer_checkout(self):
        manifest = write_manifest(self.root, {"xcode": {"version": "27.2", "build": "27B52"}})
        with patch.object(controller, "host_status", return_value={"installedMatches": False, "selectedMatches": False}), \
             patch.object(controller, "host_action") as action:
            controller.reconcile(self.root, self.root / "consumer", self.root, manifest, "zoe-mac-mini", self.root, self.root / "host-credentials")
        self.assertEqual(action.call_args.args[1], self.root / "host-credentials")


class HostAndStateTests(unittest.TestCase):
    def test_host_action_does_not_inherit_github_token(self):
        with patch.dict(os.environ, {"GH_TOKEN": "secret", "FACTORY_GH_TOKEN": "secret", "GIT_ASKPASS": "/askpass"}), patch.object(controller, "command") as command:
            for action in ("stage", "reconcile"):
                controller.host_action(Path("/factory"), Path("/host-credentials"), Path("/factory/worker/updates.json"), "zoe-mac-mini", action)
                env = command.call_args.kwargs["env"]
                self.assertNotIn("GH_TOKEN", env)
                self.assertNotIn("FACTORY_GH_TOKEN", env)
                self.assertNotIn("GIT_ASKPASS", env)
                self.assertEqual(command.call_args.kwargs["cwd"], Path("/host-credentials"))
            controller.host_action(Path("/factory"), Path("/host-credentials"), Path("/factory/worker/updates.json"), "zoe-mac-mini", "image-stage")
            self.assertEqual(command.call_args.kwargs["env"]["GH_TOKEN"], "secret")

    def test_dirty_host_tools_block_image_stage(self):
        with patch.object(controller, "command", side_effect=["main", " M ci-vm/build-image.sh"]):
            with self.assertRaisesRegex(controller.FleetError, "dirty"):
                controller.sync_host_tools(Path("/host-tools"))

    def test_state_resets_on_new_pin_and_old_shape(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "fleet.json"
            controller.save_state(path, {"pin": "27.2-27B52", "agentRuns": ["2026-10-30T00:00:00+00:00"]})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(controller.private_state(path, PIN), {"pin": "27.2-27B52", "agentRuns": ["2026-10-30T00:00:00+00:00"]})
            self.assertEqual(controller.private_state(path, controller.Pin("27.3", "27C1", "promote")), {"pin": "27.3-27C1", "agentRuns": []})
            controller.save_state(path, {"pin": "27.2-27B52", "sessions": {"consumer": "old"}})
            self.assertEqual(controller.private_state(path, PIN), {"pin": "27.2-27B52", "agentRuns": []})

    def test_status_reports_unknown_consumer_pin_as_null(self):
        config = controller.HostConfig(PIN, "ci-image", True)
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(controller.consumer_pinned(Path(temp) / "missing", config))
            with patch.object(controller, "consumer_has_pin", side_effect=controller.FleetError("offline")):
                self.assertIsNone(controller.consumer_pinned(Path(temp), config))
            with patch.object(controller, "consumer_has_pin", return_value=False):
                self.assertIs(controller.consumer_pinned(Path(temp), config), False)

    def test_agent_is_fresh_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shim = root / "codex"
            argv_log = root / "argv.json"
            shim.write_text("#!/usr/bin/env python3\nimport json,sys\nfrom pathlib import Path\n"
                            "Path(sys.argv[1]).write_text(json.dumps({'argv': sys.argv[2:], 'prompt': sys.stdin.read()}))\n"
                            "print(json.dumps({'type':'thread.started','thread_id':'00000000-0000-0000-0000-000000000001'}))\n"
                            "print(json.dumps({'type':'turn.completed'}))\n")
            shim.chmod(0o755)
            real_popen = controller.subprocess.Popen
            def fake_popen(argv, **kwargs):
                return real_popen([str(shim), str(argv_log), *argv[1:]], **kwargs)
            with patch.object(controller.subprocess, "Popen", side_effect=fake_popen):
                controller.run_agent(PIN, "https://github.com/darwin-health/evo/pull/7", CONSUMER, Path("/factory"), Path("/evo"))
            seen = json.loads(argv_log.read_text())
            self.assertEqual(seen["argv"], ["exec", "--json", "--approve-for-me", "-C", "/evo", "--add-dir", "/factory", "--worktree", "-"])
            self.assertIn("https://github.com/darwin-health/evo/pull/7", seen["prompt"])
            self.assertIn("Xcode 27.2, build 27B52", seen["prompt"])
            self.assertIn("Do not open another PR", seen["prompt"])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["argv.json", "codex"])

    def test_agent_without_completed_turn_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            shim = Path(temp) / "codex"
            shim.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"turn.failed\"}'\n")
            shim.chmod(0o755)
            real_popen = controller.subprocess.Popen
            with patch.object(controller.subprocess, "Popen", side_effect=lambda argv, **kw: real_popen([str(shim)], **kw)):
                with self.assertRaisesRegex(controller.FleetError, "did not complete"):
                    controller.run_agent(PIN, "https://x/pull/1", CONSUMER, Path("/factory"), Path("/evo"))

    def test_watchdog_kills_grandchild_holding_stdout(self):
        import time
        with tempfile.TemporaryDirectory() as temp:
            shim = Path(temp) / "codex"
            # codex exits at once; its child keeps stdout open for 30 s.
            shim.write_text("#!/bin/sh\ncat >/dev/null\nsleep 30 &\nexit 0\n")
            shim.chmod(0o755)
            real_popen = controller.subprocess.Popen
            with patch.object(controller, "AGENT_TIMEOUT_SECONDS", 1), \
                 patch.object(controller.subprocess, "Popen", side_effect=lambda argv, **kw: real_popen([str(shim)], **kw)) as popen:
                started = time.monotonic()
                with self.assertRaisesRegex(controller.FleetError, "did not complete"):
                    controller.run_agent(PIN, "https://x/pull/1", CONSUMER, Path("/factory"), Path("/evo"))
                elapsed = time.monotonic() - started
            self.assertIs(popen.call_args.kwargs["start_new_session"], True)
            self.assertLess(elapsed, 10)

    def test_github_wrapper_scopes_credential_environment(self):
        class Result:
            returncode = 0
            stdout = "example-token\n"
        seen = []
        def fake_run(argv, **kwargs):
            seen.append((argv, kwargs))
            return Result()
        with patch.object(sys, "argv", ["with_github.py", "--user", "cpheinrich", "--", "python3", "fleet/controller.py"]), \
             patch.dict(os.environ, {"ZOE_APPLE_ID_PASSWORD": "must-not-pass", "FACTORY_VERIFICATION_CODE_TOKEN": "must-not-pass", "SSH_AUTH_SOCK": "/private/sensitive.sock",
                                     "FACTORY_VERIFICATION_CODE_COMMAND": "/path/to/provider", "FACTORY_VERIFICATION_CODE_URL": "https://broker.example"}), \
             patch.object(with_github.subprocess, "run", side_effect=fake_run):
            self.assertEqual(with_github.main(), 0)
        self.assertEqual(seen[0][0], ["gh", "auth", "token", "-u", "cpheinrich"])
        child_env = seen[1][1]["env"]
        self.assertEqual(child_env["GH_TOKEN"], "example-token")
        self.assertNotIn("ZOE_APPLE_ID_PASSWORD", child_env)
        self.assertNotIn("FACTORY_VERIFICATION_CODE_TOKEN", child_env)
        self.assertNotIn("SSH_AUTH_SOCK", child_env)
        self.assertEqual(child_env["FACTORY_VERIFICATION_CODE_COMMAND"], "/path/to/provider")
        self.assertEqual(child_env["FACTORY_VERIFICATION_CODE_URL"], "https://broker.example")


if __name__ == "__main__":
    unittest.main()
