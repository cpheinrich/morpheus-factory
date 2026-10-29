"""Ownership checks for the macOS cleanup job; no simulator is touched."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


source = Path(__file__).resolve().parents[1] / "scripts/cleanup-development-cache.py"
spec = importlib.util.spec_from_file_location("cleanup", source)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class SimulatorOwnershipTests(unittest.TestCase):
    def test_watchdog_reserve_scales_with_disk_and_stops_at_target(self):
        self.assertEqual(cleanup.watch_limits(460), (69, 99))
        self.assertEqual(cleanup.watch_limits(931), (140, 170))
        self.assertEqual(cleanup.watch_limits(931, min_free_percent=10), (94, 124))
        calls = []
        free = iter([40, 90])
        stages = [("first", lambda: calls.append("first")),
                  ("second", lambda: calls.append("second"))]
        self.assertEqual(cleanup.execute_stages(stages, 80, lambda: next(free)), [])
        self.assertEqual(calls, ["first"])

    def test_healthy_watchdog_only_checks_trash(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cleanup, "HOME", Path(directory)), \
             patch.object(cleanup.sys, "argv", ["cleanup", "--watch"]), \
             patch.object(cleanup, "space_gib", return_value=(97, 460)), \
             patch.object(cleanup, "execute_stages", return_value=[]) as stages:
            self.assertEqual(cleanup.main(), 0)
            self.assertEqual([name for name, _ in stages.call_args.args[0]], ["Trash"])
            self.assertIsNone(stages.call_args.args[1])

    def test_one_terabyte_worker_enters_pressure_cleanup_at_100_gib(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cleanup, "HOME", Path(directory)), \
             patch.object(cleanup.sys, "argv", ["cleanup", "--watch"]), \
             patch.object(cleanup, "space_gib", return_value=(100, 931)), \
             patch.object(cleanup, "execute_stages", return_value=[]) as stages:
            self.assertEqual(cleanup.main(), 2)
            labels = [name for name, _ in stages.call_args.args[0]]
            self.assertIn("unused Apple apps", labels)
            self.assertIn("Spotlight exclusions", labels)
            self.assertIn("Spotlight", labels)
            self.assertEqual(labels[-1], "recent DerivedData")
            self.assertEqual(stages.call_args.args[1], 170)

    def test_manual_cleanup_includes_apps_and_pressure_only_spotlight(self):
        for free, spotlight in ((100, True), (200, False)):
            with self.subTest(free=free), tempfile.TemporaryDirectory() as directory, \
                 patch.object(cleanup, "HOME", Path(directory)), \
                 patch.object(cleanup.sys, "argv", ["cleanup", "--apply"]), \
                 patch.object(cleanup, "space_gib", return_value=(free, 931)), \
                 patch.object(cleanup, "execute_stages", return_value=[]) as stages:
                self.assertEqual(cleanup.main(), 2 if spotlight else 0)
                labels = [name for name, _ in stages.call_args.args[0]]
                self.assertIn("unused Apple apps", labels)
                self.assertEqual("Spotlight" in labels, spotlight)

    def test_low_reserve_runs_to_target_and_reports_remaining_pressure(self):
        for ending_space, expected_exit in [(81, 0), (45, 2)]:
            with self.subTest(ending_space=ending_space), tempfile.TemporaryDirectory() as directory, \
                 patch.object(cleanup, "HOME", Path(directory)), \
                 patch.object(cleanup.sys, "argv", ["cleanup", "--watch"]), \
                 patch.object(cleanup, "space_gib", side_effect=[(40, 460), (ending_space, 460)]), \
                 patch.object(cleanup, "execute_stages", return_value=[]) as stages:
                self.assertEqual(cleanup.main(), expected_exit)
                self.assertEqual(stages.call_args.args[1], 99)

    def test_installed_app_installers_need_no_age_but_unrelated_archives_remain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            downloads = root / "Downloads"
            apps = root / "Applications"
            downloads.mkdir()
            (apps / "Claude.app").mkdir(parents=True)
            (apps / "Xcode-beta.app").mkdir()
            (apps / "Brave Browser.app").mkdir()
            old_xcode = downloads / "Xcode_27.xip"
            old_claude = downloads / "Claude.dmg"
            new_xcode = downloads / "Xcode_28_Beta.xip"
            brave = downloads / "Brave-Browser-1.0.dmg"
            unrelated = downloads / "important.zip"
            not_installed = downloads / "UnknownApp.pkg"
            for path in (old_xcode, old_claude, new_xcode, brave, unrelated, not_installed):
                path.write_bytes(b"installer")
            age = time.time() - 40 * cleanup.DAY
            for path in (old_xcode, old_claude, unrelated):
                os.utime(path, (age, age))
            self.assertEqual(cleanup.remove_installed_app_installers(downloads, False, apps), 4)
            self.assertTrue(old_xcode.exists())
            self.assertEqual(cleanup.remove_installed_app_installers(downloads, True, apps), 4)
            self.assertFalse(old_xcode.exists())
            self.assertFalse(old_claude.exists())
            self.assertFalse(new_xcode.exists())
            self.assertFalse(brave.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(not_installed.exists())

    def test_old_build_directories_stop_at_target_without_touching_recent_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oldest = root / "oldest"
            older = root / "older"
            third = root / "third"
            recent = root / "recent"
            for path in (oldest, older, third, recent):
                path.mkdir()
                (path / "build").write_bytes(b"output")
            now = time.time()
            os.utime(oldest, (now - 10 * cleanup.DAY,) * 2)
            os.utime(older, (now - 8 * cleanup.DAY,) * 2)
            os.utime(third, (now - 7.5 * cleanup.DAY,) * 2)
            with patch.object(cleanup, "space_gib", side_effect=[(40, 460), (70, 460), (105, 460)]):
                self.assertEqual(cleanup.remove_old_children(root, 7, True, target=100), 2)
            self.assertFalse(oldest.exists())
            self.assertFalse(older.exists())
            self.assertTrue(third.exists())
            self.assertTrue(recent.exists())

    def test_simulator_window_does_not_count_as_xcode_editor(self):
        simulator = "/Applications/Xcode.app/Contents/Developer/Applications/Simulator.app/Contents/MacOS/Simulator\n"
        editor = "/Applications/Xcode.app/Contents/MacOS/Xcode\n"
        with patch.object(cleanup.subprocess, "check_output", return_value=simulator):
            self.assertFalse(cleanup.xcode_editor_is_running())
        with patch.object(cleanup.subprocess, "check_output", return_value=editor):
            self.assertTrue(cleanup.xcode_editor_is_running())

    def test_trash_removes_only_entries_older_than_30_days(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            trash = Path(directory) / ".Trash"
            trash.mkdir()
            old_file = trash / "old-file"
            recent_file = trash / "recent-file"
            boundary_file = trash / "exactly-30-days"
            old_folder = trash / "old-folder"
            old_file.write_bytes(b"old")
            recent_file.write_bytes(b"recent")
            boundary_file.write_bytes(b"boundary")
            old_folder.mkdir()
            (old_folder / "content").write_bytes(b"old")
            now = time.time()
            original_lstat = Path.lstat

            def age(path):
                if path.parent == trash and path.name != ".DS_Store":
                    age_days = 1 if path == recent_file else 30 if path == boundary_file else 31
                    stamp = now - age_days * cleanup.DAY
                    return SimpleNamespace(st_ctime=stamp, st_birthtime=stamp,
                                           st_mode=original_lstat(path).st_mode)
                return original_lstat(path)

            with patch.object(Path, "lstat", age), patch.object(cleanup.time, "time", return_value=now):
                self.assertEqual(cleanup.remove_old_trash(trash, 30, False), 2)
                self.assertTrue(old_file.exists())
                self.assertEqual(cleanup.remove_old_trash(trash, 30, True), 2)
            self.assertFalse(old_file.exists())
            self.assertTrue(recent_file.exists())
            self.assertTrue(boundary_file.exists())
            self.assertFalse(old_folder.exists())

    def test_trash_uses_later_inode_timestamp_and_keeps_finder_metadata(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            trash = Path(directory) / ".Trash"
            trash.mkdir()
            metadata = trash / ".DS_Store"
            metadata.write_bytes(b"finder")
            item = trash / "old-source-recently-trashed"
            item.write_bytes(b"content")
            now = time.time()
            original_lstat = Path.lstat

            def age(path):
                if path == item:
                    return SimpleNamespace(st_ctime=now - 31 * cleanup.DAY,
                                           st_birthtime=now - 1 * cleanup.DAY,
                                           st_mode=original_lstat(path).st_mode)
                return original_lstat(path)

            with patch.object(Path, "lstat", age):
                self.assertEqual(cleanup.remove_old_trash(trash, 30, True), 0)
            self.assertTrue(metadata.exists())
            self.assertTrue(item.exists())

    def test_swiftpm_cache_must_exceed_five_gib_and_have_no_build(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "swiftpm"
            cache.mkdir()
            for name in ("artifacts", "manifests"):
                (cache / name).mkdir()
                (cache / name / "download").write_bytes(b"cached")
            with patch.object(cleanup.subprocess, "check_output", return_value=f"{5 * 1024 ** 2}\t{cache}"), \
                 patch.object(cleanup, "swift_build_is_running") as build, \
                 patch.object(cleanup, "run") as purge:
                self.assertFalse(cleanup.prune_large_swiftpm_cache(cache, 5, True))
                build.assert_not_called()
                purge.assert_not_called()
            with patch.object(cleanup.subprocess, "check_output", return_value=f"{5 * 1024 ** 2 + 1}\t{cache}"), \
                 patch.object(cleanup, "swift_build_is_running", return_value=True), \
                 patch.object(cleanup, "run") as purge:
                self.assertFalse(cleanup.prune_large_swiftpm_cache(cache, 5, True))
                purge.assert_not_called()
                self.assertTrue((cache / "artifacts/download").exists())
            with patch.object(cleanup.subprocess, "check_output", return_value=f"{5 * 1024 ** 2 + 1}\t{cache}"), \
                 patch.object(cleanup, "swift_build_is_running", return_value=False), \
                 patch.object(cleanup.shutil, "which", return_value="/usr/bin/swift"), \
                 patch.object(cleanup, "run") as purge:
                self.assertTrue(cleanup.prune_large_swiftpm_cache(cache, 5, True))
                purge.assert_called_once_with(
                    ["swift", "package", "purge-cache", "--cache-path", str(cache)], True)
                self.assertFalse((cache / "artifacts").exists())
                self.assertFalse((cache / "manifests").exists())

    def test_spotlight_reset_only_above_limit_and_restores_indexing(self):
        with patch.object(cleanup, "spotlight_index_gib", return_value=4.9), \
             patch.object(cleanup.subprocess, "check_output") as check, \
             patch.object(cleanup, "run") as run:
            self.assertFalse(cleanup.reset_large_spotlight_index(True))
            check.assert_not_called()
            run.assert_not_called()
        with patch.object(cleanup, "spotlight_index_gib", return_value=6), \
             patch.object(cleanup.subprocess, "check_output", return_value="Indexing enabled."), \
             patch.object(cleanup, "run") as run:
            self.assertTrue(cleanup.reset_large_spotlight_index(True))
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args_list[0].args[0][-2:], ["-E", "/System/Volumes/Data"])
            self.assertEqual(run.call_args_list[1].args[0][-3:], ["-i", "on", "/System/Volumes/Data"])

    def test_agent_cache_cleanup_requires_closed_app_and_preserves_session_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "Cache"
            cache.mkdir()
            (cache / "data").write_bytes(b"cached")
            session = root / "Session Storage"
            session.mkdir()
            (session / "state").write_bytes(b"session")
            with patch.object(cleanup, "app_is_running", return_value=True):
                self.assertEqual(cleanup.remove_app_cache_contents(root, ("Cache",), ("Claude",), True), 0)
                self.assertTrue((cache / "data").exists())
            with patch.object(cleanup, "app_is_running", return_value=False):
                self.assertEqual(cleanup.remove_app_cache_contents(root, ("Cache",), ("Claude",), False), 1)
                self.assertTrue((cache / "data").exists())
                self.assertEqual(cleanup.remove_app_cache_contents(root, ("Cache",), ("Claude",), True), 1)
            self.assertFalse((cache / "data").exists())
            self.assertEqual((session / "state").read_bytes(), b"session")

    def test_spotlight_helper_runs_without_launchd_tmpdir(self):
        helper = source.parents[1] / "worker/setup/spotlight.sh"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            code = home / "code"
            code.mkdir()
            environment = os.environ.copy()
            environment.pop("TMPDIR", None)
            environment.update({"HOME": str(home), "WORKER_CODE_ROOT": str(code),
                                "WORKER_CHECK_ONLY": "1"})
            result = subprocess.run(["bash", str(helper)], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((code / ".metadata_never_index").exists())

    def test_worker_lease_protects_device_until_expiry(self):
        udid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as directory, patch.object(cleanup, "LEASE_DIR", Path(directory)):
            lease = Path(directory) / f"{udid}.json"
            lease.write_text(json.dumps({"udid": udid, "expiresAt": time.time() + 600}))
            self.assertTrue(cleanup.worker_lease_is_active(udid, time.time()))
            lease.write_text("broken JSON")
            self.assertTrue(cleanup.worker_lease_is_active(udid, time.time()))
            lease.write_text(json.dumps({"udid": udid, "expiresAt": time.time() - 600}))
            self.assertFalse(cleanup.worker_lease_is_active(udid, time.time()))

    def test_preview_lease_protects_shutdown_device(self):
        udid = "11111111-2222-3333-4444-555555555555"
        device = {"name": "Evo QA 123456abcdef", "udid": udid}
        with tempfile.TemporaryDirectory() as directory, patch.object(cleanup, "HOME", Path(directory)):
            state = Path(directory) / "Library/Caches/med.evo.preview/123456abcdef/session.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"udid": udid, "expiresAt": (time.time() + 600) * 1000}))
            with patch.object(cleanup.subprocess, "run") as run:
                run.return_value.returncode = 1  # supervisor not loaded
                self.assertTrue(cleanup.preview_is_owned(device, time.time()))
                state.write_text(json.dumps({"udid": udid, "expiresAt": 0}))
                self.assertFalse(cleanup.preview_is_owned(device, time.time()))
                run.return_value.returncode = 0  # supervisor loaded
                self.assertTrue(cleanup.preview_is_owned(device, time.time()))

    def test_only_named_dangling_devices_are_candidates(self):
        self.assertTrue(cleanup.CI_NAME.fullmatch("Morpheus CI 11111111-2222-3333-4444-555555555555"))
        self.assertTrue(cleanup.CI_NAME.fullmatch("Lakina Test 11111111-2222-3333-4444-555555555555"))
        self.assertTrue(cleanup.CLONE_NAME.fullmatch("Clone 1 of Morpheus CI 11111111-2222-3333-4444-555555555555"))
        self.assertFalse(cleanup.CI_NAME.fullmatch("Morpheus CI Template iPhone-17-Pro-Max iOS-26-5"))
        self.assertFalse(cleanup.QA_NAME.fullmatch("iPhone 17"))

    def test_live_lease_skips_deletion_but_dangling_device_is_selected(self):
        udid = "11111111-2222-3333-4444-555555555555"
        with tempfile.TemporaryDirectory() as directory:
            os.utime(directory, (time.time() - 20 * cleanup.DAY,) * 2)
            device = {"name": "Evo QA 123456abcdef", "udid": udid,
                      "state": "Shutdown", "dataPath": directory}
            inventory = json.dumps({"devices": {"ios": [device]}})

            def output(command, **_kwargs):
                return inventory if command[:2] == ["xcrun", "simctl"] else ""

            def process(command, **_kwargs):
                return Mock(returncode=1, stderr="Provided set path does not exist")

            with patch.object(cleanup.shutil, "which", return_value="xcrun"), \
                 patch.object(cleanup.subprocess, "check_output", side_effect=output), \
                 patch.object(cleanup.subprocess, "run", side_effect=process), \
                 patch.object(cleanup, "preview_is_owned", return_value=False), \
                 patch.object(cleanup, "worker_lease_is_active", return_value=True), \
                 patch.object(cleanup, "run") as delete:
                self.assertEqual(cleanup.remove_old_simulators(14, 2, True), 0)
                delete.assert_not_called()
            with patch.object(cleanup.shutil, "which", return_value="xcrun"), \
                 patch.object(cleanup.subprocess, "check_output", side_effect=output), \
                 patch.object(cleanup.subprocess, "run", side_effect=process), \
                 patch.object(cleanup, "preview_is_owned", return_value=False), \
                 patch.object(cleanup, "worker_lease_is_active", return_value=False), \
                 patch.object(cleanup, "run") as delete:
                self.assertEqual(cleanup.remove_old_simulators(14, 2, True), 1)
                delete.assert_called_once_with(["xcrun", "simctl", "delete", udid], True)


if __name__ == "__main__":
    unittest.main()
