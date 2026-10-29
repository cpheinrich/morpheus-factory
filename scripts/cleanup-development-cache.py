#!/usr/bin/env python3
"""Reclaim disposable developer data on a macOS worker.

Dry run by default. The hourly launchd job passes --watch. This deliberately
does not touch installed simulator runtimes, normal simulator devices, browser
profiles, repositories, credentials, or application support databases.
"""

import argparse
import fcntl
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


HOME = Path.home()
QA_NAME = re.compile(r"Evo QA [0-9a-f]{12}\Z")
JOB_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CI_NAME = re.compile(rf"(?:Morpheus CI|Evo Test|Lakina Test) {JOB_ID}\Z")
CLONE_NAME = re.compile(rf"Clone [0-9]+ of Morpheus CI {JOB_ID}\Z")
XCODE_INSTALLER = re.compile(r"Xcode_[0-9]+(?:\.[0-9]+)*(?:_Apple_silicon)?\.xip\Z")
DAY = 86400
LEASE_DIR = HOME / ".local/state/morpheus-factory/simulator-leases"
UDID = re.compile(JOB_ID + r"\Z")


def log(message):
    print(message, flush=True)


def run(command, apply):
    log("$ " + " ".join(command))
    if apply:
        subprocess.run(command, check=True)


def space_gib():
    stat = os.statvfs(HOME)
    unit = 1024 ** 3
    return stat.f_bavail * stat.f_frsize / unit, stat.f_blocks * stat.f_frsize / unit


def spotlight_index_gib():
    store = Path("/System/Volumes/Data/.Spotlight-V100")
    if not store.is_dir():
        return 0.0
    output = subprocess.check_output(["sudo", "-n", "/usr/bin/du", "-sk", str(store)], text=True)
    return int(output.split()[0]) / 1024 ** 2


def reset_large_spotlight_index(apply, limit_gib=5):
    size = spotlight_index_gib()
    log(f"Spotlight index: {size:.1f} GiB; reset threshold {limit_gib} GiB")
    if size <= limit_gib:
        return False
    volume = "/System/Volumes/Data"
    status = subprocess.check_output(["/usr/bin/mdutil", "-s", volume], text=True)
    if "Indexing enabled." not in status and "Indexing disabled." not in status:
        raise RuntimeError("Cannot determine Spotlight indexing state; retain index")
    enabled = "Indexing enabled." in status
    if apply:
        try:
            run(["sudo", "-n", "/usr/bin/mdutil", "-E", volume], True)
        finally:
            if enabled:
                run(["sudo", "-n", "/usr/bin/mdutil", "-i", "on", volume], True)
    else:
        log(f"Would reset Spotlight index on {volume}")
    return True


def watch_limits(total_gib, base_floor=50):
    floor = max(base_floor, math.ceil(total_gib * 0.10))
    return floor, floor + 30


def execute_stages(stages, target=None, free_space=None):
    """Run least disruptive steps first; stop once a watchdog has its reserve."""
    failures = []
    for label, action in stages:
        if target is not None and free_space() >= target:
            log(f"Target reserve reached before {label}; stopping cleanup")
            break
        try:
            action()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError, RuntimeError) as error:
            failures.append(f"{label}: {error}")
    return failures


def remove_old_children(parent, days, apply):
    if not parent.is_dir() or parent.is_symlink():
        return 0
    cutoff = time.time() - days * DAY
    count = 0
    for path in sorted(parent.iterdir()):
        if path.is_symlink() or not path.is_dir() or path.stat().st_mtime >= cutoff:
            continue
        log(f"{'Remove' if apply else 'Would remove'} {path}")
        if apply:
            shutil.rmtree(path)
        count += 1
    return count


def remove_old_installers(downloads, days, apply, applications=Path("/Applications")):
    """Only remove recognized Downloads installers, never arbitrary archives."""
    if not downloads.is_dir() or downloads.is_symlink():
        return 0
    cutoff = time.time() - days * DAY
    candidates = list(downloads.iterdir())
    ibkr = downloads / "ibkr"
    if ibkr.is_dir() and not ibkr.is_symlink():
        candidates.extend(ibkr.iterdir())
    installed = {
        "Brave-Browser.dmg": "Brave Browser.app",
        "Claude.dmg": "Claude.app",
        "Hermes-Setup.dmg": "Hermes.app",
        "1Password Installer.app": "1Password.app",
        "ibgateway-stable-standalone-macos-arm.dmg": "IB Gateway",
    }
    prefixes = {
        "Slack-": "Slack.app",
        "MacLock-": "MacLock.app",
        "Zed-": "Zed.app",
    }
    count = 0
    for path in sorted(candidates):
        if path.is_symlink() or not (path.is_file() or path.name == "1Password Installer.app" and path.is_dir()):
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        if XCODE_INSTALLER.fullmatch(path.name):
            eligible = True
        else:
            app = installed.get(path.name)
            if app is None and path.suffix == ".dmg":
                app = next((value for prefix, value in prefixes.items() if path.name.startswith(prefix)), None)
            if app == "IB Gateway":
                eligible = any((HOME / "Applications").glob("IB Gateway *"))
            else:
                eligible = app is not None and (applications / app).is_dir()
        if not eligible:
            continue
        log(f"{'Remove' if apply else 'Would remove'} old installer {path}")
        if apply:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        count += 1
    return count


def remove_old_trash(trash, days, apply):
    """Use the latest inode timestamp so recently trashed items are retained."""
    if not trash.is_dir() or trash.is_symlink():
        return 0
    cutoff = time.time() - days * DAY
    count = 0
    for path in sorted(trash.iterdir()):
        if path.name in (".DS_Store", ".localized"):
            continue
        info = path.lstat()
        if max(info.st_ctime, getattr(info, "st_birthtime", info.st_ctime)) >= cutoff:
            continue
        log(f"{'Remove' if apply else 'Would remove'} old Trash entry {path}")
        if apply:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        count += 1
    return count


def swift_build_is_running():
    commands = subprocess.check_output(["ps", "ax", "-o", "comm="], text=True)
    return any(Path(command).name in ("swift", "swiftc", "swift-frontend", "xcodebuild")
               for command in commands.splitlines())


def prune_large_swiftpm_cache(cache, limit_gib, apply):
    if not cache.is_dir() or cache.is_symlink():
        return False
    size_kib = int(subprocess.check_output(["du", "-sk", str(cache)], text=True).split()[0])
    size_gib = size_kib / 1024 ** 2
    log(f"SwiftPM cache: {size_gib:.1f} GiB; cleanup threshold {limit_gib} GiB")
    if size_gib <= limit_gib:
        return False
    if swift_build_is_running():
        log("Swift or Xcode build running; skipping SwiftPM cache")
        return False
    if not shutil.which("swift"):
        log("swift unavailable; skipping SwiftPM cache")
        return False
    run(["swift", "package", "purge-cache", "--cache-path", str(cache)], apply)
    # SwiftPM's purge-cache command clears repositories, but leaves these
    # reproducible downloads and compiled manifests in the same cache root.
    for name in ("artifacts", "manifests"):
        path = cache / name
        if not path.is_dir() or path.is_symlink():
            continue
        log(f"{'Remove' if apply else 'Would remove'} SwiftPM {name} cache {path}")
        if apply:
            shutil.rmtree(path)
    return True


def app_is_running(app_names):
    """Cache directories may contain open Chromium files while their app runs."""
    commands = subprocess.check_output(["ps", "ax", "-o", "comm="], text=True)
    return any(f"/{name}.app/Contents/" in command for command in commands.splitlines()
               for name in app_names)


def remove_app_cache_contents(root, cache_names, app_names, apply):
    if app_is_running(app_names):
        log(f"{app_names[0]} running; skipping its application caches")
        return 0
    count = 0
    for name in cache_names:
        cache = root / name
        if not cache.is_dir() or cache.is_symlink():
            continue
        for path in cache.iterdir():
            log(f"{'Remove' if apply else 'Would remove'} application cache {path}")
            if apply:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            count += 1
    return count


def preview_is_owned(device, now):
    """A live preview lease or launchd supervisor owns its exact device."""
    key = device["name"].split()[-1]
    state_file = HOME / "Library/Caches/med.evo.preview" / key / "session.json"
    label = f"gui/{os.getuid()}/med.evo.preview.{key}"
    if subprocess.run(["launchctl", "print", label], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL).returncode == 0:
        return True
    if not state_file.exists():
        return False
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return True  # unknown ownership is protected
    return state.get("udid") == device["udid"] and state.get("expiresAt", 0) > now * 1000


def worker_lease_is_active(udid, now):
    lease = LEASE_DIR / f"{udid}.json"
    if not lease.exists():
        return False
    try:
        data = json.loads(lease.read_text())
        return data.get("udid") != udid or float(data["expiresAt"]) > now
    except (OSError, ValueError, TypeError, KeyError):
        return True  # unreadable lease must not authorize deletion


def update_worker_lease(udid, minutes, release):
    if not UDID.fullmatch(udid):
        raise ValueError("invalid simulator UDID")
    LEASE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = LEASE_DIR / f"{udid}.json"
    if release:
        path.unlink(missing_ok=True)
        log(f"Released simulator lease {udid}")
    else:
        temporary = LEASE_DIR / f"{udid}.tmp"
        temporary.write_text(json.dumps({"udid": udid, "expiresAt": time.time() + minutes * 60}))
        temporary.chmod(0o600)
        temporary.replace(path)
        log(f"Leased simulator {udid} for {minutes} minutes")


def remove_old_simulators(qa_days, job_days, apply):
    if not shutil.which("xcrun"):
        log("xcrun unavailable; skipping simulators")
        return 0
    inventories = [([], json.loads(subprocess.check_output(
        ["xcrun", "simctl", "list", "devices", "-j"], text=True
    )))]
    testing = subprocess.run(["xcrun", "simctl", "--set", "testing", "list", "devices", "-j"],
                             text=True, capture_output=True)
    if testing.returncode == 0:
        inventories.append((["--set", "testing"], json.loads(testing.stdout)))
    elif "Provided set path does not exist" not in testing.stderr:
        raise RuntimeError("Cannot inventory XCTest simulator set")
    devices_root = HOME / "Library/Developer/CoreSimulator/Devices"
    now = time.time()
    processes = subprocess.check_output(["ps", "ax", "-o", "command="], text=True)
    build_active = subprocess.run(["pgrep", "-f", "[x]codebuild"],
                                  stdout=subprocess.DEVNULL).returncode == 0
    count = 0
    for selector, inventory in inventories:
        for device in (d for group in inventory["devices"].values() for d in group):
            name, udid = device["name"], device["udid"]
            is_qa = bool(QA_NAME.fullmatch(name))
            is_job = bool(CI_NAME.fullmatch(name) or CLONE_NAME.fullmatch(name))
            if not (is_qa or is_job) or device["state"] != "Shutdown":
                continue
            directory = Path(device.get("dataPath") or devices_root / udid)
            if not directory.is_dir() or directory.is_symlink():
                continue
            days = qa_days if is_qa else job_days
            if directory.stat().st_mtime >= now - days * DAY:
                continue
            if udid in processes or name in processes:
                log(f"Protect {name} ({udid}): referenced by a live process")
                continue
            if worker_lease_is_active(udid, now):
                log(f"Protect {name} ({udid}): worker lease")
                continue
            if is_qa and preview_is_owned(device, now):
                log(f"Protect {name} ({udid}): preview lease or supervisor")
                continue
            if is_job and build_active:
                log(f"Protect {name} ({udid}): build activity on host")
                continue
            run(["xcrun", "simctl", *selector, "delete", udid], apply)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="perform a full manual cleanup")
    mode.add_argument("--watch", action="store_true", help="clean only below the free-space reserve")
    parser.add_argument("--min-free-gb", type=int, default=50,
                        help="minimum reserve; watchdog also uses 10%% of volume capacity")
    parser.add_argument("--simulator-days", type=int, default=14)
    parser.add_argument("--build-days", type=int, default=30)
    parser.add_argument("--job-simulator-days", type=int, default=2)
    parser.add_argument("--installer-days", type=int, default=30)
    parser.add_argument("--trash-days", type=int, default=30)
    lease = parser.add_mutually_exclusive_group()
    lease.add_argument("--lease-udid")
    lease.add_argument("--release-udid")
    parser.add_argument("--lease-minutes", type=int, default=720)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("macOS required")
    if (args.simulator_days < 2 or args.build_days < 7 or args.job_simulator_days < 2
            or args.installer_days < 7 or args.trash_days < 30):
        parser.error("retention is too short")
    if args.min_free_gb < 10:
        parser.error("minimum free space must be at least 10 GiB")
    if not 1 <= args.lease_minutes <= 1440:
        parser.error("lease minutes must be 1–1440")
    if args.lease_udid or args.release_udid:
        try:
            update_worker_lease(args.lease_udid or args.release_udid, args.lease_minutes,
                                bool(args.release_udid))
        except ValueError as error:
            parser.error(str(error))
        return 0
    apply = args.apply or args.watch
    target = None
    healthy_watch = False
    if args.watch:
        available, total = space_gib()
        floor, target = watch_limits(total, args.min_free_gb)
        log(f"Storage watchdog: {available:.1f} GiB free; floor {floor} GiB; target {target} GiB")
        if available >= floor:
            healthy_watch = True
            log("Reserve healthy; checking old Trash entries only")
    else:
        log(f"Development cleanup: {'apply' if apply else 'dry run'}")

    def simulators():
        count = remove_old_simulators(args.simulator_days, args.job_simulator_days, apply)
        log(f"Unowned shutdown QA and CI/CD simulators: {count}")

    def installers():
        count = remove_old_installers(HOME / "Downloads", args.installer_days, apply)
        log(f"Old recognized installers: {count}")

    def trash():
        count = remove_old_trash(HOME / ".Trash", args.trash_days, apply)
        log(f"Trash entries older than {args.trash_days} days: {count}")

    def derived_data():
        # A running build may be writing inside an old DerivedData directory.
        if subprocess.run(["pgrep", "-f", "[x]codebuild"], stdout=subprocess.DEVNULL).returncode == 0:
            log("xcodebuild running; skipping DerivedData")
        else:
            count = remove_old_children(
                HOME / "Library/Developer/Xcode/DerivedData", args.build_days, apply
            )
            log(f"Old DerivedData directories: {count}")

    def npx():
        if subprocess.run(["pgrep", "-f", "[n]px"], stdout=subprocess.DEVNULL).returncode == 0:
            log("npx running; skipping old npx installs")
            return
        count = remove_old_children(HOME / ".npm/_npx", args.simulator_days, apply)
        log(f"Old npx installs: {count}")

    def package_cache(label, command):
        if not shutil.which(command[0]):
            log(f"{label} unavailable; skipping")
            return
        active_pattern = {"npm": "[n]pm", "pnpm": "[p]npm", "uv": "[u]v ",
                          "Homebrew": "[b]rew ", "pip": "[p]ip "}[label]
        if subprocess.run(["pgrep", "-f", active_pattern], stdout=subprocess.DEVNULL).returncode == 0:
            log(f"{label} active; skipping cache cleanup")
            return
        if label == "pip" and apply:
            log("$ " + " ".join(command))
            result = subprocess.run(command, text=True, capture_output=True)
            if result.stdout.strip():
                log(result.stdout.strip())
            if result.stderr.strip() and "No matching packages" not in result.stderr:
                log(result.stderr.strip())
            if result.returncode and "No matching packages" not in result.stderr:
                raise RuntimeError(f"exit {result.returncode}")
        else:
            run(command, apply)

    def worktrees():
        helper = Path(__file__).with_name("cleanup-old-worktrees.py")
        if not helper.is_file():
            raise RuntimeError(f"Worktree cleanup helper missing: {helper}")
        command = [sys.executable, str(helper)]
        code_root_file = HOME / ".config/morpheus-factory/code-root"
        if code_root_file.is_file():
            command.extend(["--code-root", code_root_file.read_text().strip()])
        if apply:
            command.append("--apply")
        if target is not None:
            command.extend(["--target-free-gb", str(target)])
        run(command, True)

    def agent_app_caches():
        claude_root = HOME / "Library/Application Support/Claude"
        claude = remove_app_cache_contents(
            claude_root,
            ("Cache", "Code Cache", "GPUCache", "GraphiteDawnCache", "DawnGraphiteCache",
             "DawnWebGPUCache", "GPUPersistentCache"),
            ("Claude",), apply,
        )
        claude += remove_app_cache_contents(
            HOME / "Library/Caches", ("com.anthropic.claudefordesktop",),
            ("Claude",), apply,
        )
        codex_root = HOME / "Library/Application Support/Codex"
        codex = remove_app_cache_contents(
            codex_root, ("GraphiteDawnCache", "GPUCache", "Code Cache"),
            ("ChatGPT", "Codex"), apply,
        )
        codex += remove_app_cache_contents(
            HOME / ".codex", ("cache",), ("ChatGPT", "Codex"), apply,
        )
        log(f"Claude and Codex disposable application cache entries: {claude + codex}")

    stages = [
        ("Trash", trash),
        ("installers", installers),
        ("simulators", simulators),
        ("DerivedData", derived_data),
        ("npx", npx),
        ("SwiftPM", lambda: prune_large_swiftpm_cache(
            HOME / "Library/Caches/org.swift.swiftpm", 5, apply)),
        ("uv", lambda: package_cache("uv", ["uv", "cache", "prune"])),
        ("Homebrew", lambda: package_cache("Homebrew", ["brew", "cleanup", "--prune=30"])),
    ]
    if importlib.util.find_spec("pip"):
        stages.append(("pip", lambda: package_cache("pip", [sys.executable, "-m", "pip", "cache", "purge"])))
    stages.extend([
        ("pnpm", lambda: package_cache("pnpm", ["pnpm", "store", "prune"])),
        ("npm", lambda: package_cache("npm", ["npm", "cache", "clean", "--force"])),
        ("agent application caches", agent_app_caches),
        ("worktrees", worktrees),
    ])
    if args.watch:
        stages.append(("Spotlight", lambda: reset_large_spotlight_index(True)))

    lock_dir = HOME / ".local/state/morpheus-factory"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / "storage-watchdog.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Another cleanup is running; skipping")
            return 0
        failures = execute_stages(stages[:1] if healthy_watch else stages,
                                  None if healthy_watch else target, lambda: space_gib()[0])
    if failures:
        for failure in failures:
            log("ERROR " + failure)
        return 1
    if args.watch and not healthy_watch:
        available, _ = space_gib()
        log(f"Storage watchdog finished: {available:.1f} GiB free")
        if available < floor:
            log("ERROR Free space remains below the reserve after eligible cleanup")
            return 2
    log("Cleanup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
