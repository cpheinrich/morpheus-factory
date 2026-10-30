#!/usr/bin/env python3
"""Reconcile one Mac's Xcode installation with a reviewed fleet pin.

Runs as the login user. Only the small child environment passed to xcodes contains
Apple credentials; logs and the persisted state never contain them.
"""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")
BUILD = re.compile(r"^[A-Za-z0-9]+$")
HOST = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class UpdateError(Exception):
    pass


def run(argv, *, env=None, capture=True, cwd=None, timeout=None):
    if env is None:
        env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME") if key in os.environ}
    result = subprocess.run(argv, env=env, text=True, capture_output=capture, check=False, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode:
        # Child output may contain private account information. Never echo it.
        if Path(argv[0]).name == "xcodes":
            details = (result.stdout + result.stderr).lower()
            for words, category in (
                (("two-factor", "verification code", "trusted device"), "authentication-required: two-factor challenge"),
                (("terms and conditions", "agreement"), "authentication-required: Apple Developer agreement"),
                (("401", "403", "unauthorized"), "authentication-required: Apple refused login"),
                (("password", "login", "sign in"), "authentication-required: Apple login"),
                (("network", "offline", "could not connect"), "network unavailable"),
            ):
                if any(word in details for word in words):
                    raise UpdateError(f"xcodes: {category}")
        raise UpdateError(f"{Path(argv[0]).name} exited {result.returncode}")
    return result.stdout.strip() if capture else ""


def pin_for_host(manifest_path, host):
    raw = json.loads(Path(manifest_path).read_text())
    if raw.get("schemaVersion") != 1 or not isinstance(raw.get("hosts"), dict):
        raise UpdateError("manifest requires schemaVersion 1 and hosts object")
    pin = raw["hosts"].get(host)
    if not isinstance(pin, dict) or not isinstance(pin.get("xcode"), dict):
        raise UpdateError(f"host {host} has no Xcode pin")
    xcode = pin["xcode"]
    version, build = xcode.get("version"), xcode.get("build")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise UpdateError("Xcode version must be exact numeric form")
    if not isinstance(build, str) or not BUILD.fullmatch(build):
        raise UpdateError("Xcode build must be an exact build identifier")
    checks = pin.get("validation")
    if not isinstance(checks, list) or not checks:
        raise UpdateError(f"host {host} requires nonempty validation commands")
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("argv"), list) or not check["argv"]:
            raise UpdateError("each validation requires an argv array")
        if any(not isinstance(arg, str) or not arg for arg in check["argv"]):
            raise UpdateError("validation arguments must be nonempty strings")
        if not Path(check["argv"][0]).is_absolute():
            raise UpdateError("validation executable must be an absolute path")
        if "cwd" in check and (not isinstance(check["cwd"], str) or not Path(check["cwd"]).is_absolute()):
            raise UpdateError("validation cwd must be absolute")
    phase = pin.get("phase", "stage")
    if phase not in ("stage", "promote"):
        raise UpdateError("phase must be stage or promote")
    return version, build, checks, phase


def app_path(version):
    return Path(f"/Applications/Xcode_{version}.app")


def matching_app(version, build):
    for app in (app_path(version), Path("/Applications/Xcode.app")):
        if installed_build(app) == (version, build):
            return app
    return None


def installed_build(app):
    binary = app / "Contents/Developer/usr/bin/xcodebuild"
    if not binary.is_file():
        return None
    output = run([str(binary), "-version"])
    match = re.fullmatch(r"Xcode ([^\n]+)\nBuild version ([^\n]+)", output)
    if not match:
        raise UpdateError(f"unrecognized Xcode version output from {app}")
    return match.group(1), match.group(2)


def report(version, build):
    app = matching_app(version, build)
    have = None if app is None else (version, build)
    selected = run(["/usr/bin/xcode-select", "-p"])
    return {
        "desired": {"version": version, "build": build},
        "installed": None if have is None else {"version": have[0], "build": have[1]},
        "installedMatches": have == (version, build),
        "selected": selected,
        "selectedMatches": app is not None and selected == str(app / "Contents/Developer"),
    }


def xcodes_environment():
    username = os.environ.get("ZOE_APPLE_ID", "")
    password = os.environ.get("ZOE_APPLE_ID_PASSWORD", "")
    if not username or not password:
        raise UpdateError("authentication-required: ZOE_APPLE_ID and ZOE_APPLE_ID_PASSWORD are missing")
    # Do not pass Evo's other credentials to a third-party installer.
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL")
    child = {key: os.environ[key] for key in allowed if key in os.environ}
    child["XCODES_USERNAME"] = username
    child["XCODES_PASSWORD"] = password
    return child


def check_authentication():
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    output = run(["xcodes", "list", "--data-source", "apple", "--no-color"], env=xcodes_environment(), timeout=60)
    if not output:
        raise UpdateError("Apple Developer login returned no Xcode listings")
    return "apple-authenticated"


def interactive_login():
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    # Only this explicit command inherits the user's terminal for Apple's 2FA.
    # It still passes only the Apple variables to the installer.
    result = subprocess.run(["xcodes", "list", "--data-source", "apple", "--no-color"], env=xcodes_environment(), check=False)
    if result.returncode:
        raise UpdateError("authentication-required: interactive Apple login did not complete")
    return "apple-authenticated"


def stage(version, build, stage_root):
    app = app_path(version)
    if matching_app(version, build) is not None:
        return "already-installed"
    have = installed_build(app)
    if have is not None or app.exists():
        raise UpdateError(f"{app} exists with the wrong build; refusing overwrite")
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    if shutil.disk_usage("/Applications").free < 70 * 1024**3:
        raise UpdateError("at least 70 GiB free space is required to stage Xcode")
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"xcode-{version}-", dir=stage_root) as temporary:
        directory = Path(temporary)
        # xcodes handles Apple's login and download. The archive is then expanded
        # by macOS xip, which verifies its signature, before the app is moved.
        run(["xcodes", "download", version, "--directory", str(directory)], env=xcodes_environment())
        archives = list(directory.glob("*.xip"))
        if len(archives) != 1:
            raise UpdateError("expected exactly one Xcode archive from xcodes")
        run(["/usr/bin/xip", "--expand", str(archives[0])], env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"}, capture=False, cwd=directory)
        expanded = directory / "Xcode.app"
        if not expanded.is_dir():
            raise UpdateError("expanded archive contains no Xcode.app")
        actual = installed_build(expanded)
        if actual != (version, build):
            raise UpdateError(f"downloaded Xcode identity {actual} differs from approved pin")
        run(["/usr/sbin/spctl", "--assess", "--type", "execute", str(expanded)])
        run(["/usr/bin/sudo", "-n", "/bin/mv", str(expanded), str(app)])
    if installed_build(app) != (version, build):
        raise UpdateError("installed Xcode did not retain the approved identity")
    run(["/usr/bin/sudo", "-n", str(app / "Contents/Developer/usr/bin/xcodebuild"), "-license", "accept"])
    run(["/usr/bin/sudo", "-n", str(app / "Contents/Developer/usr/bin/xcodebuild"), "-runFirstLaunch"], capture=False)
    return "staged"


def activate(version, build):
    app = matching_app(version, build)
    if app is None:
        raise UpdateError("approved Xcode is not installed; activation refused")
    # A busy machine is not a safe place to change the system-wide selection.
    process_list = run(["/bin/ps", "-axo", "comm="])
    if any(Path(line).name in {"xcodebuild", "swift-frontend"} for line in process_list.splitlines()):
        raise UpdateError("Xcode or Swift build is active; activation deferred")
    run(["/usr/bin/sudo", "-n", "/usr/bin/xcode-select", "--switch", str(app / "Contents/Developer")])
    if not report(version, build)["selectedMatches"]:
        raise UpdateError("xcode-select did not select the approved Xcode")
    return "activated"


def validate(version, build, checks):
    app = matching_app(version, build)
    if app is None:
        raise UpdateError("approved Xcode is not installed; validation refused")
    developer = str(app / "Contents/Developer")
    child = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME") if key in os.environ}
    child["DEVELOPER_DIR"] = developer
    for index, check in enumerate(checks, 1):
        try:
            run(check["argv"], env=child, cwd=check.get("cwd"), timeout=7200, capture=False)
        except (UpdateError, subprocess.TimeoutExpired) as error:
            raise UpdateError(f"validation {index} failed: {type(error).__name__}") from error
    return "validated"


def reconcile(version, build, checks, phase, stage_root):
    before = report(version, build)
    stage(version, build, stage_root)
    if phase == "stage":
        return "staged"
    validate(version, build, checks)
    if before["selectedMatches"]:
        return "already-current"
    previous = before["selected"]
    try:
        activate(version, build)
    except UpdateError:
        # If a switch succeeded but verification failed, restore the prior toolchain.
        if run(["/usr/bin/xcode-select", "-p"]) != previous:
            run(["/usr/bin/sudo", "-n", "/usr/bin/xcode-select", "--switch", previous])
        raise
    return "promoted"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("auth-check", "auth-login", "status", "stage", "validate", "activate", "reconcile"))
    parser.add_argument("--manifest")
    parser.add_argument("--host")
    parser.add_argument("--state-dir", default=str(Path.home() / ".local/state/morpheus-factory/updates"))
    args = parser.parse_args()
    if args.command == "auth-check":
        print(json.dumps({"result": check_authentication()}))
        return 0
    if args.command == "auth-login":
        print(json.dumps({"result": interactive_login()}))
        return 0
    if not args.host or not HOST.fullmatch(args.host):
        raise UpdateError("host id must use lowercase letters, digits, and hyphens")
    if not args.manifest:
        raise UpdateError("--manifest is required")
    version, build, checks, phase = pin_for_host(args.manifest, args.host)
    if args.command == "status":
        print(json.dumps(report(version, build), indent=2))
        return 0
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (state_dir / "controller.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateError("another update is in progress") from error
        if args.command == "stage":
            result = stage(version, build, state_dir)
        elif args.command == "validate":
            result = validate(version, build, checks)
        elif args.command == "reconcile":
            result = reconcile(version, build, checks, phase, state_dir)
        else:
            result = activate(version, build)
        print(json.dumps({"result": result, "host": args.host, "xcode": {"version": version, "build": build}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"update-controller: {error}", file=sys.stderr)
        sys.exit(1)
