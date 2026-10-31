#!/usr/bin/env python3
"""Reconcile one Mac's Xcode installation with a reviewed fleet pin.

Runs as the login user. Only the small child environment passed to xcodes contains
Apple credentials; logs and the persisted state never contain them.
"""

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")
BUILD = re.compile(r"^[A-Za-z0-9]+$")
HOST = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CODE = re.compile(r"^[0-9]{4,10}$")
SMS_PROMPT = re.compile(r"Enter the ([0-9]+) digit code sent to")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
LABEL = re.compile(r"^[A-Za-z0-9._-]+$")
TEAM = re.compile(r"^[A-Z0-9]{10}$")
UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$")
ORGANIZATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
RUNNER_KEYS = {"label", "dir", "requiredEnv", "organization"}
ROLES = ("ci-image", "phone-gate")
PATH_KEYS = ("factory", "consumer", "hostSetup", "credentialsProject")
DEFAULT_APPLE = {"idVar": "APPLE_ID", "passwordVar": "APPLE_ID_PASSWORD"}
MIN_FREE_BYTES = 70 * 1024**3
SUDOERS_FILE = "/etc/sudoers.d/morpheus-factory"
# The only command run as root: a root-owned copy of worker/xcode-root-helper.sh.
HELPER_PATH = "/usr/local/libexec/morpheus-factory-xcode"
HELPER_SOURCE = Path(__file__).resolve().with_name("xcode-root-helper.sh")
BUSY_PROCESSES = {"xcodebuild", "swift-frontend", "Runner.Worker"}
APPLICATIONS = Path("/Applications")


class UpdateError(Exception):
    pass


class CodeTimeout(UpdateError):
    def __init__(self, message, started_at=None):
        super().__init__(message)
        self.started_at = started_at


XCODES_CATEGORIES = (
    # Apple's identity service answers with 503 when it is down; that is not a
    # login problem, so it is checked before the login words.
    (("503", "non200statuscode", "service unavailable"), "apple-unavailable: Apple returned an HTTP error; retry later"),
    (("two-factor", "verification code", "trusted device"), "authentication-required: two-factor challenge"),
    (("terms and conditions", "agreement"), "authentication-required: Apple Developer agreement"),
    (("401", "403", "unauthorized"), "authentication-required: Apple refused login"),
    (("password", "login", "sign in"), "authentication-required: Apple login"),
    (("network", "offline", "could not connect"), "network unavailable"),
)


def xcodes_category(output):
    """A fixed category for failed xcodes output; the output itself is never echoed."""
    details = output.lower()
    for words, category in XCODES_CATEGORIES:
        if any(word in details for word in words):
            return category
    return None


def run(argv, *, env=None, capture=True, cwd=None, timeout=None):
    if env is None:
        env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME") if key in os.environ}
    result = subprocess.run(argv, env=env, text=True, capture_output=capture, check=False, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL)
    if result.returncode:
        # Child output may contain private account information. Never echo it.
        if Path(argv[0]).name == "xcodes":
            category = xcodes_category((result.stdout or "") + (result.stderr or ""))
            if category:
                raise UpdateError(f"xcodes: {category}")
        raise UpdateError(f"{Path(argv[0]).name} exited {result.returncode}")
    return result.stdout.strip() if capture else ""


@dataclass(frozen=True)
class HostPin:
    version: str
    build: str
    checks: list
    phase: str
    verification: Optional[dict]
    components: list
    role: str
    retain: int
    paths: dict
    apple: dict
    ci: Optional[dict]
    runner_label: Optional[str]
    runner: Optional[dict]
    signing: Optional[dict]
    device_udid: Optional[str]


def _object(pin, key):
    value = pin.get(key)
    if value is not None and not isinstance(value, dict):
        raise UpdateError(f"{key} must be an object")
    return value


def pin_for_host(manifest_path, host):
    raw = json.loads(Path(manifest_path).read_text())
    if raw.get("schemaVersion") != 1 or not isinstance(raw.get("hosts"), dict):
        raise UpdateError("manifest requires schemaVersion 1 and hosts object")
    pin = raw["hosts"].get(host)
    if not isinstance(pin, dict) or not isinstance(pin.get("xcode"), dict):
        raise UpdateError(f"host {host} has no Xcode pin")
    for key in ("paths", "apple", "runner", "signing", "device", "ci"):
        _object(pin, key)
    xcode = pin["xcode"]
    version, build = xcode.get("version"), xcode.get("build")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise UpdateError("Xcode version must be exact numeric form")
    if not isinstance(build, str) or not BUILD.fullmatch(build):
        raise UpdateError("Xcode build must be an exact build identifier")
    components = xcode.get("components", [])
    if not isinstance(components, list) or any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]+", item) for item in components):
        raise UpdateError("Xcode components must be named identifiers")
    if len(set(components)) != len(components):
        raise UpdateError("Xcode components must not repeat")
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
    # An absent phase means promote; holding a host at stage is an explicit,
    # reviewed manifest value.
    phase = xcode.get("phase", "promote")
    if phase not in ("stage", "promote"):
        raise UpdateError("phase must be stage or promote")
    retain = xcode.get("retain", 1)
    if type(retain) is not int or retain < 1:
        raise UpdateError("xcode.retain must be an integer of at least 1")
    verification = pin.get("verification")
    if verification is not None:
        if not isinstance(verification, dict) or set(verification) != {"purpose", "account"}:
            raise UpdateError("verification requires purpose and account")
        if any(not isinstance(verification[key], str) or not HOST.fullmatch(verification[key]) for key in ("purpose", "account")):
            raise UpdateError("verification purpose and account must be lowercase identifiers")
    ci = pin.get("ci")
    if ci is not None and "runtimes" in ci:
        runtimes = ci["runtimes"]
        # Exact iOS versions: the image builder copies these simulator runtimes
        # from the host, and the verifier proves each is in the guest.
        if not isinstance(runtimes, list) or not runtimes or any(not isinstance(item, str) or not VERSION.fullmatch(item) for item in runtimes) \
                or len(set(runtimes)) != len(runtimes):
            raise UpdateError("ci.runtimes must be a non-empty list of unique exact iOS versions")
    if ci is not None and "imagePruner" in ci and (not isinstance(ci["imagePruner"], str) or not Path(ci["imagePruner"]).is_absolute()):
        raise UpdateError("ci.imagePruner must be an absolute path")
    role = pin.get("role", "ci-image" if ci is not None else "phone-gate")
    if role not in ROLES:
        raise UpdateError("role must be ci-image or phone-gate")
    if (role == "ci-image") != (ci is not None):
        raise UpdateError("a ci-image host requires a ci block and a phone-gate host must not have one")
    apple = {**DEFAULT_APPLE, **(pin.get("apple") or {})}
    if set(apple) != set(DEFAULT_APPLE) or any(not isinstance(apple[key], str) or not ENV_NAME.fullmatch(apple[key]) for key in apple):
        raise UpdateError("apple idVar and passwordVar must be environment variable names")
    paths = pin.get("paths") or {}
    if set(paths) - set(PATH_KEYS):
        raise UpdateError(f"paths accepts only {', '.join(PATH_KEYS)}")
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in paths.values()):
        raise UpdateError("paths must be absolute")
    paths = default_paths(role, paths)
    runner = pin.get("runner")
    runner_label = None
    if runner is not None:
        runner_label = runner.get("label")
        if set(runner) - RUNNER_KEYS or not isinstance(runner_label, str) or not LABEL.fullmatch(runner_label):
            raise UpdateError("runner.label must be a LaunchAgent label")
        if "dir" in runner and (not isinstance(runner["dir"], str) or not Path(runner["dir"]).is_absolute()):
            raise UpdateError("runner.dir must be absolute")
        required = runner.get("requiredEnv", [])
        if not isinstance(required, list) or any(not isinstance(name, str) or not ENV_NAME.fullmatch(name) for name in required):
            raise UpdateError("runner.requiredEnv must list environment variable names")
        if "organization" in runner and (not isinstance(runner["organization"], str) or not ORGANIZATION.fullmatch(runner["organization"])):
            raise UpdateError("runner.organization must be a GitHub organization login")
    signing = pin.get("signing")
    if signing is not None:
        if set(signing) != {"identity", "team"} or not isinstance(signing["identity"], str) or not signing["identity"].strip() \
                or ":" in signing["identity"] or not isinstance(signing["team"], str) or not TEAM.fullmatch(signing["team"]):
            raise UpdateError("signing requires an identity name and a 10-character team id")
    device = pin.get("device")
    device_udid = None
    if device is not None:
        device_udid = device.get("udid")
        if set(device) != {"udid"} or not isinstance(device_udid, str) or not UDID.fullmatch(device_udid):
            raise UpdateError("device.udid must be a hardware UDID")
    if runner is not None:
        runner = {**runner, "requiredEnv": runner.get("requiredEnv", [])}
    return HostPin(version, build, checks, phase, verification, components, role, retain, paths, apple, ci,
                   runner_label, runner, signing, device_udid)


def default_paths(role, configured):
    # Generic defaults match the fleet coordinator's; a host overrides them in
    # its manifest entry rather than relying on one login name.
    home = Path.home()
    consumer = configured.get("consumer", str(home / "code/evo"))
    paths = {
        "factory": configured.get("factory", str(Path(__file__).resolve().parents[1])),
        "consumer": consumer,
        "credentialsProject": configured.get("credentialsProject", consumer),
    }
    if role == "ci-image" or "hostSetup" in configured:
        paths["hostSetup"] = configured.get("hostSetup", str(home / "code/agent-host-setup"))
    return paths


def ci_for_host(pin):
    ci, version = pin.ci, pin.version
    if not isinstance(ci, dict):
        raise UpdateError("host has no CI image configuration")
    if ci.get("image") != f"ci-xcode-{version}" or ci.get("label") != f"xcode-{version}":
        raise UpdateError("CI image and label must match the approved Xcode version")
    if not isinstance(ci.get("repository"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", ci["repository"]):
        raise UpdateError("CI repository must be an owner/name pair")
    for key in ("builder", "verifier", "activator"):
        path = ci.get(key)
        if not isinstance(path, str) or not Path(path).is_absolute() or not os.access(path, os.X_OK):
            raise UpdateError(f"CI {key} must be an executable absolute path")
    tart = ci.get("tart", str(Path.home() / ".local/bin/tart"))
    if not isinstance(tart, str) or not Path(tart).is_absolute() or not os.access(tart, os.X_OK):
        raise UpdateError("CI tart must be an executable absolute path")
    return {**ci, "tart": tart}


def image_identity(image):
    root = Path.home() / ".tart/vms" / image
    files = {}
    for name in ("disk.img", "config.json", "nvram.bin"):
        path = root / name
        stat = path.stat()
        files[name] = {"inode": stat.st_ino, "size": stat.st_size, "ctimeNs": stat.st_ctime_ns, "mtimeNs": stat.st_mtime_ns}
    files["configSha256"] = hashlib.sha256((root / "config.json").read_bytes()).hexdigest()
    return files


def image_is_sealed(tart, image):
    images = json.loads(run([tart, "list", "--format", "json"]))
    return any(item.get("Name") == image and item.get("Source") == "local" and not item.get("Running") for item in images)


def ci_environment():
    # Image tools may fetch GitHub runner assets, but never need Apple login or
    # verification-code credentials inherited from the host staging process.
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME",
               "GH_TOKEN", "FACTORY_GH_TOKEN", "GIT_ASKPASS", "GIT_TERMINAL_PROMPT",
               "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def stage_ci_image(version, build, stage_root, verification, components, ci, apple=None):
    # The fleet sequencer stages missing host apps before this command. Keep
    # ordinary hourly image checks cheap when the exact app is already present.
    if matching_app(version, build) is None:
        stage(version, build, stage_root, verification, components, apple)
    image = ci["image"]
    runtimes = list(ci.get("runtimes", []))
    runtime_args = [argument for runtime in runtimes for argument in ("--runtime", runtime)]
    if not image_is_sealed(ci["tart"], image):
        run([ci["builder"], "--xcode", str(app_path(version)), "--name", image, *runtime_args], env=ci_environment(), capture=False, timeout=14400)
    if not image_is_sealed(ci["tart"], image):
        raise UpdateError("CI image builder did not produce a sealed local image")
    identity = image_identity(image)
    receipt = stage_root / "ci-images" / f"{image}.json"
    # The runtimes are part of the proof: a changed list forces re-verification.
    expected = {"image": image, "version": version, "build": build, "runtimes": runtimes, "files": identity}
    try:
        cached = json.loads(receipt.read_text()) == expected
    except (OSError, ValueError):
        cached = False
    if not cached:
        try:
            run([ci["verifier"], "--name", image, "--version", version, "--build", build, *runtime_args], env=ci_environment(), capture=False, timeout=900)
        except UpdateError as error:
            # A sealed image is never rebuilt automatically, so one built before
            # the runtimes list changed fails here every tick until it is moved.
            raise UpdateError(f"CI image {image} failed verification ({error}); if it predates the current ci.runtimes, "
                              f"run `tart rename {image} {image}-stale` (or `tart delete {image}`), then rerun image-stage "
                              "to rebuild it") from error
        if image_identity(image) != identity:
            raise UpdateError("CI image changed during verification")
        receipt.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(expected, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        temporary.replace(receipt)
    run([ci["activator"], "--repo", ci["repository"], "--label", ci["label"], "--image", image], env=ci_environment(), capture=False, timeout=180)
    return "already-verified" if cached else "verified-and-activated"


def app_path(version):
    return APPLICATIONS / f"Xcode_{version}.app"


def matching_app(version, build):
    for app in (app_path(version), APPLICATIONS / "Xcode.app"):
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


def xcodes_environment(apple=None):
    apple = apple or DEFAULT_APPLE
    username = os.environ.get(apple["idVar"], "")
    password = os.environ.get(apple["passwordVar"], "")
    if not username or not password:
        raise UpdateError(f"authentication-required: {apple['idVar']} and {apple['passwordVar']} are missing")
    # Do not pass Evo's other credentials to a third-party installer.
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL")
    child = {key: os.environ[key] for key in allowed if key in os.environ}
    child["XCODES_USERNAME"] = username
    child["XCODES_PASSWORD"] = password
    return child


def request_verification_code(verification, started_at, timeout_seconds, length):
    """Ask a host-installed provider for one code without logging its output."""
    executable = os.environ.get("FACTORY_VERIFICATION_CODE_COMMAND", "")
    if not executable or not Path(executable).is_absolute() or not os.access(executable, os.X_OK):
        raise UpdateError("verification-code provider is not configured as an executable absolute path")
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME", "FACTORY_VERIFICATION_CODE_URL", "FACTORY_VERIFICATION_CODE_TOKEN") if key in os.environ}
    request = {"purpose": verification["purpose"], "account": verification["account"], "challengeStartedAt": started_at, "timeoutSeconds": timeout_seconds}
    try:
        result = subprocess.run([executable], input=json.dumps(request), text=True, capture_output=True, env=env, check=False, timeout=timeout_seconds + 10)
    except subprocess.TimeoutExpired as error:
        raise CodeTimeout("verification-code provider timed out") from error
    if result.returncode == 75:
        raise CodeTimeout("verification code did not arrive")
    if result.returncode:
        raise UpdateError("verification-code provider failed")
    try:
        response = json.loads(result.stdout)
        code = response["code"]
    except (ValueError, KeyError, TypeError) as error:
        raise UpdateError("verification-code provider returned an invalid response") from error
    if not isinstance(code, str) or not CODE.fullmatch(code) or len(code) != length:
        raise UpdateError("verification-code provider returned an invalid code length")
    return code


def _answer_hidden_prompt(master, line, timeout=5.0):
    """Write a reply to a prompt xcodes reads with readpassphrase(3).

    readpassphrase first resets the terminal with TCSAFLUSH, which discards any
    input already queued. Writing the reply the instant the prompt text arrives
    therefore loses it and both sides wait forever (the Pro, 2026-10-31). The
    terminal reports echo off once readpassphrase is reading, so wait for that
    before writing; the master shares the slave's termios on macOS.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not termios.tcgetattr(master)[3] & termios.ECHO:
                break
        except termios.error:
            break
        time.sleep(0.05)
    os.write(master, line)


def _xcodes_interactive_attempt(argv, verification, wait_seconds, first_challenge, apple=None):
    """Drive xcodes' one-shot SMS prompt through a private pseudo-terminal."""
    master, slave = pty.openpty()
    process = None
    output = ""
    sms_selected = False
    phone_selected = False
    code_submitted = False
    challenge_started = first_challenge
    try:
        process = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=xcodes_environment(apple), close_fds=True)
        os.close(slave)
        slave = -1
        deadline = time.monotonic() + 4 * 60 * 60
        while True:
            if time.monotonic() >= deadline:
                raise UpdateError("xcodes timed out")
            ready, _, _ = select.select([master], [], [], min(1, max(0, deadline - time.monotonic())))
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    output = (output + chunk.decode("utf-8", errors="replace"))[-131072:]
                    recent = output[-8192:]
                    if not sms_selected and 'Enter "sms"' in recent and "trusted devices:" in recent:
                        _answer_hidden_prompt(master, b"sms\n")
                        sms_selected = True
                    if not phone_selected and "Select a trusted phone number to receive a code via SMS:" in recent:
                        choices = re.findall(r"(?m)^([0-9]+): .+$", recent)
                        configured = os.environ.get("FACTORY_VERIFICATION_PHONE_INDEX", "")
                        if configured:
                            if configured not in choices:
                                raise UpdateError("configured trusted phone index is unavailable")
                            choice = configured
                        elif len(choices) == 1:
                            choice = choices[0]
                        else:
                            raise UpdateError("multiple trusted phone numbers require FACTORY_VERIFICATION_PHONE_INDEX")
                        challenge_started = challenge_started or time.time()
                        os.write(master, f"{choice}\n".encode())
                        phone_selected = True
                    match = SMS_PROMPT.search(recent)
                    if not code_submitted and match:
                        challenge_started = challenge_started or time.time()
                        try:
                            code = request_verification_code(verification, challenge_started, wait_seconds, int(match.group(1)))
                        except CodeTimeout as error:
                            raise CodeTimeout(str(error), challenge_started) from error
                        _answer_hidden_prompt(master, f"{code}\n".encode())
                        code_submitted = True
                    continue
            if process.poll() is not None:
                break
        if process.returncode:
            details = output.lower()
            if any(word in details for word in ("rate limit", "too many", "locked")):
                raise UpdateError("xcodes: Apple limited verification attempts")
            if code_submitted:
                raise UpdateError("xcodes: verification code was rejected")
            category = xcodes_category(output)
            if category:
                raise UpdateError(f"xcodes: {category}")
            raise UpdateError(f"xcodes exited {process.returncode}")
        return "xcodes-complete", challenge_started
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
        if slave >= 0:
            os.close(slave)


def run_xcodes(argv, verification=None, apple=None):
    if verification is None or not os.environ.get("FACTORY_VERIFICATION_CODE_COMMAND"):
        return run(argv, env=xcodes_environment(apple))
    first_challenge = None
    for attempt, seconds in enumerate((120, 120, 180), 1):
        try:
            output, _ = _xcodes_interactive_attempt(argv, verification, seconds, first_challenge, apple=apple)
            return output.strip()
        except CodeTimeout as error:
            if attempt == 3:
                raise UpdateError("verification-code-timeout after three SMS sends") from error
            # A late code from the first send remains eligible after a resend.
            first_challenge = first_challenge or error.started_at
    raise AssertionError("unreachable")


def check_authentication(verification=None, apple=None):
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    output = run_xcodes(["xcodes", "list", "--data-source", "apple", "--no-color"], verification, apple)
    if not output:
        raise UpdateError("Apple Developer login returned no Xcode listings")
    return "apple-authenticated"


def interactive_login(apple=None):
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    # Only this explicit command inherits the user's terminal for Apple's 2FA.
    # It still passes only the Apple variables to the installer.
    result = subprocess.run(["xcodes", "list", "--data-source", "apple", "--no-color"], env=xcodes_environment(apple), check=False)
    if result.returncode:
        raise UpdateError("authentication-required: interactive Apple login did not complete")
    return "apple-authenticated"


def ensure_components(app, components):
    child = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME") if key in os.environ}
    child["DEVELOPER_DIR"] = str(app / "Contents/Developer")
    for component in components:
        run([str(app / "Contents/Developer/usr/bin/xcodebuild"), "-downloadComponent", component], env=child, capture=False, timeout=7200)


def stage(version, build, stage_root, verification=None, components=(), apple=None):
    app = app_path(version)
    existing = matching_app(version, build)
    if existing is not None:
        ensure_components(existing, components)
        return "already-installed"
    have = installed_build(app)
    if have is not None or app.exists():
        raise UpdateError(f"{app} exists with the wrong build; refusing overwrite")
    if shutil.which("xcodes") is None:
        raise UpdateError("xcodes is missing")
    if shutil.disk_usage("/Applications").free < MIN_FREE_BYTES:
        raise UpdateError("at least 70 GiB free space is required to stage Xcode")
    stage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"xcode-{version}-", dir=stage_root) as temporary:
        directory = Path(temporary)
        # xcodes handles Apple's login and download. The archive is then expanded
        # by macOS xip, which verifies its signature, before the app is moved.
        run_xcodes(["xcodes", "download", version, "--directory", str(directory)], verification, apple)
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
        # The helper moves the bundle into a root-only quarantine, verifies its
        # signature there, runs first launch, and only then installs it, so
        # nothing the worker user can still write ever runs as root.
        root_helper("install", expanded, version, capture=False)
    if installed_build(app) != (version, build):
        raise UpdateError("installed Xcode did not retain the approved identity")
    ensure_components(app, components)
    return "staged"


def root_helper(verb, *arguments, capture=True):
    return run(["/usr/bin/sudo", "-n", HELPER_PATH, verb, *map(str, arguments)], capture=capture)


def host_is_busy():
    # A busy machine is not a safe place to change the system-wide selection.
    # A GitHub Actions job can sit between build steps with no compiler
    # running, so a live Runner.Worker counts too. That process is the child
    # `launchctl print gui/<uid>/<runner label>` would show, so this one check
    # also covers the configured runner LaunchAgent.
    process_list = run(["/bin/ps", "-axo", "comm="])
    return any(Path(line.strip()).name in BUSY_PROCESSES for line in process_list.splitlines())


def developer_app(developer):
    """The app bundle a developer directory belongs to, or None."""
    return Path(developer).parents[1] if developer.endswith(".app/Contents/Developer") else None


def activate(version, build, state_dir=None):
    app = matching_app(version, build)
    if app is None:
        raise UpdateError("approved Xcode is not installed; activation refused")
    # Checked again here: a job may have started during validation.
    if host_is_busy():
        raise UpdateError("Xcode build or runner job is active; activation deferred")
    previous = run(["/usr/bin/xcode-select", "-p"])
    root_helper("select", app)
    if not report(version, build)["selectedMatches"]:
        raise UpdateError("xcode-select did not select the approved Xcode")
    if state_dir is not None:
        if previous != str(app / "Contents/Developer"):
            record_promotion(state_dir, version, build, previous)
        else:
            # Re-activating the pin by hand is not a promotion: keep the window
            # start and the real previous app, which retire depends on.
            ensure_promotion_record(state_dir, version, build)
    return "activated"


PROMOTION_FILE = "promoted.json"


def write_private_json(path, document):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(document, output, sort_keys=True)
        output.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_promotion(state_dir, version, build, previous):
    """The start of the migration window; the fleet compares delivery runs against it."""
    write_private_json(Path(state_dir) / PROMOTION_FILE,
                       {"version": version, "build": build, "promotedAt": now_iso(), "previous": previous})


def promotion(state_dir, version, build):
    """The promotion record for this exact pin, or None."""
    try:
        record = json.loads((Path(state_dir) / PROMOTION_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != version or record.get("build") != build \
            or not isinstance(record.get("promotedAt"), str):
        return None
    return record


def ensure_promotion_record(state_dir, version, build):
    # A host that selected the pin before promotion records existed starts its
    # window when first observed: later than the real switch, so only a
    # delivery run newer than that can validate it.
    if promotion(state_dir, version, build) is None:
        record_promotion(state_dir, version, build, None)


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


def reconcile(version, build, checks, phase, stage_root, verification=None, components=(), apple=None, before=None):
    before = before or report(version, build)
    stage(version, build, stage_root, verification, components, apple)
    if before.get("selectedMatches"):
        ensure_promotion_record(stage_root, version, build)
    if phase == "stage":
        return "staged"
    if before["selectedMatches"]:
        return "already-current"
    # Do not run a long validation build beside a live job only for activation
    # to refuse afterwards; the next tick retries.
    if host_is_busy():
        return "deferred-busy"
    validate(version, build, checks)
    previous = before["selected"]
    try:
        activate(version, build, stage_root)
    except UpdateError:
        # If a switch succeeded but verification failed, restore the prior toolchain.
        if run(["/usr/bin/xcode-select", "-p"]) != previous:
            previous_app = developer_app(previous)
            if previous_app is None:
                raise UpdateError(f"cannot restore {previous}: it is not inside an Xcode app") from None
            root_helper("select", previous_app)
        raise
    return "promoted"


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def prune(version, build, retain, previous=None, window=False):
    """Keep `retain` Xcode bundles: protected ones first, then the newest others.

    Protected: the selected app, each app in `previous` (the selection before
    this reconcile, and the one recorded at promotion), and the target of an
    /Applications/Xcode.app symlink. During the migration window (`window`:
    promoted but not yet validated) at least two bundles stay, so the previous
    Xcode survives until delivery is green whatever `retain` says.
    Returns the removed paths, or None when the host is busy and nothing was
    considered, so a caller can tell "nothing to remove" from "not now".
    """
    if host_is_busy():
        return None
    current = report(version, build)
    if not current["selectedMatches"]:
        raise UpdateError("approved Xcode is not selected; pruning refused")
    if window:
        retain = max(retain, 2)
    protected = {app_path(version), developer_app(current["selected"])}
    for developer in ([previous] if isinstance(previous, str) else list(previous or [])):
        if developer:
            protected.add(developer_app(developer))
    plain = APPLICATIONS / "Xcode.app"
    if plain.is_symlink():
        protected.add(plain.resolve())
    protected = {app.resolve() for app in protected if app is not None and app.exists()}
    candidates = []
    for app in APPLICATIONS.glob("Xcode_*.app"):
        match = re.fullmatch(r"Xcode_([0-9]+(?:\.[0-9]+){1,2})\.app", app.name)
        if match is None or app.is_symlink() or app.resolve() in protected:
            continue
        try:
            identity = installed_build(app)
        except UpdateError:
            identity = None
        # Only a bundle Xcode itself identifies is ours to remove.
        if identity is not None:
            candidates.append((version_key(match.group(1)), app))
    candidates.sort(reverse=True)
    removed = []
    for _, app in candidates[max(0, retain - len(protected)):]:
        root_helper("remove", app)
        removed.append(str(app))
    return removed


def in_window(state_dir, version, build):
    record = promotion(state_dir, version, build)
    return record is None or not record.get("retiredAt")


def image_pruner(pin):
    ci = pin.ci or {}
    return ci.get("imagePruner") or str(Path(pin.paths["hostSetup"]) / "ci-vm/prune-images.mjs")


def retire(pin, state_dir):
    """Remove the previous Xcode (and unreferenced CI images) once the fleet has validated the pin."""
    version, build = pin.version, pin.build
    if host_is_busy():
        return "deferred-busy", []
    current = report(version, build)
    if not current["selectedMatches"]:
        raise UpdateError("approved Xcode is not selected; retire refused")
    record = promotion(state_dir, version, build)
    if record is None:
        raise UpdateError("no promotion record for the approved Xcode; retire refused")
    selected = developer_app(current["selected"])
    previous = developer_app(record["previous"]) if isinstance(record.get("previous"), str) else None
    if previous is not None and selected is not None and previous.resolve() == selected.resolve():
        raise UpdateError("the previous Xcode is the selected one; retire refused")
    removed = prune(version, build, pin.retain)
    if removed is None:
        # A job started after the first check: retire nothing, so the next tick
        # retries instead of stamping retiredAt over a previous app still present.
        return "deferred-busy", []
    if pin.role == "ci-image":
        pruner = image_pruner(pin)
        if not Path(pruner).is_absolute() or not os.access(pruner, os.X_OK):
            raise UpdateError("CI image pruner must be an executable absolute path")
        # The small default environment: deleting local images needs no GitHub
        # or Apple credentials. Its output is not echoed.
        run([pruner, "--apply"], timeout=600)
    write_private_json(Path(state_dir) / PROMOTION_FILE, {**record, "retiredAt": now_iso()})
    return "retired", removed


def check_xcodes(pin):
    path = shutil.which("xcodes")
    return (True, path) if path else (False, "xcodes is not on PATH")


def check_disk(pin):
    if matching_app(pin.version, pin.build) is not None:
        return True, "pinned Xcode is already installed"
    free = shutil.disk_usage("/Applications").free
    gib = free // 1024**3
    if free < MIN_FREE_BYTES:
        return False, f"{gib} GiB free on /Applications; staging needs 70 GiB"
    return True, f"{gib} GiB free on /Applications"


def helper_install_commands():
    user = os.environ.get("USER") or "<user>"
    return (f"sudo install -d -o root -g wheel -m 755 {Path(HELPER_PATH).parent}; "
            f"sudo install -o root -g wheel -m 755 {HELPER_SOURCE} {HELPER_PATH}; then add this line with "
            f"sudo visudo -f {SUDOERS_FILE}: {user} ALL=(root) NOPASSWD: {HELPER_PATH}")


def helper_status(path=None):
    return os.stat(path or HELPER_PATH)


def helper_ancestors():
    # A writable ancestor (Intel Homebrew owns /usr/local) would let the worker
    # user swap the root-owned helper out from under the sudoers rule.
    # Both the path as written in sudoers and its resolution: a symlinked
    # directory in the written path (Intel Homebrew's /usr/local/libexec) sits
    # in a parent the resolved path never visits, and a root-owned symlink into
    # a tree with a writable parent would pass a check on the written path alone.
    written = Path(HELPER_PATH)
    seen = []
    for parent in (*written.parents, *written.resolve().parents):
        if str(parent) not in seen:
            seen.append(str(parent))
    return seen


def check_sudo(pin):
    problems = []
    try:
        status = helper_status()
    except FileNotFoundError:
        problems.append(f"{HELPER_PATH} is not installed")
    else:
        if Path(HELPER_PATH).is_symlink():
            problems.append(f"{HELPER_PATH} is a symlink")
        if status.st_uid != 0:
            problems.append(f"{HELPER_PATH} is not owned by root")
        if status.st_mode & 0o022:
            problems.append(f"{HELPER_PATH} is group- or other-writable")
        if hashlib.sha256(Path(HELPER_PATH).read_bytes()).digest() != hashlib.sha256(HELPER_SOURCE.read_bytes()).digest():
            problems.append(f"{HELPER_PATH} differs from {HELPER_SOURCE.name}; reinstall it")
        for ancestor in helper_ancestors():
            parent = helper_status(ancestor)
            if parent.st_uid != 0 or parent.st_mode & 0o022:
                problems.append(f"{ancestor} is not root-owned and closed to group/other writes")
    try:
        run(["/usr/bin/sudo", "-n", "-l", HELPER_PATH])
    except UpdateError:
        problems.append(f"sudo -n does not allow {HELPER_PATH}")
    if problems:
        return False, f"{'; '.join(problems)}. Install: {helper_install_commands()}"
    return True, f"{HELPER_PATH} is root-owned, current, and allowed by sudo -n"


def check_apple(pin):
    names = (pin.apple["idVar"], pin.apple["passwordVar"])
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        return False, f"missing or empty: {', '.join(missing)}"
    return True, f"{' and '.join(names)} are set"


def check_provider(pin):
    command = os.environ.get("FACTORY_VERIFICATION_CODE_COMMAND", "")
    url = os.environ.get("FACTORY_VERIFICATION_CODE_URL", "")
    problems = []
    if not command or not Path(command).is_absolute() or not os.access(command, os.X_OK):
        problems.append("FACTORY_VERIFICATION_CODE_COMMAND is not an executable absolute path")
    if not url.startswith("https://"):
        problems.append("FACTORY_VERIFICATION_CODE_URL is not https")
    return (False, "; ".join(problems)) if problems else (True, "verification-code provider configured")


def check_paths(pin):
    problems = []
    for key, value in pin.paths.items():
        path = Path(value)
        if not path.is_dir():
            problems.append(f"{key} {value} is not a directory")
        elif key in ("factory", "consumer") and not (path / ".git").exists():
            problems.append(f"{key} {value} is not a git checkout")
    return (False, "; ".join(problems)) if problems else (True, ", ".join(f"{key}={value}" for key, value in pin.paths.items()))


def check_ci(pin):
    try:
        ci = ci_for_host(pin)
    except UpdateError as error:
        return False, str(error)
    return True, f"image {ci['image']} for {ci['repository']}"


def check_runtimes(pin):
    wanted = pin.ci["runtimes"]
    listing = json.loads(run(["/usr/bin/xcrun", "simctl", "runtime", "list", "-j"], timeout=120))
    if not isinstance(listing, dict):
        raise UpdateError("simctl runtime list returned no object")
    present = {entry.get("version") for entry in listing.values()
               if isinstance(entry, dict) and entry.get("platformIdentifier") == "com.apple.platform.iphonesimulator"
               and entry.get("state", "Ready") == "Ready"}
    missing = [runtime for runtime in wanted if runtime not in present]
    if missing:
        # The image builder copies the host's runtime disk images into the guest.
        return False, (f"iOS simulator runtime(s) missing on the host: {', '.join(missing)}. "
                       "Install with: xcodebuild -downloadPlatform iOS (add -buildVersion <version> for a runtime "
                       "other than the selected Xcode's)")
    return True, f"iOS {', '.join(wanted)} simulator runtimes present"


def check_runner(pin):
    plist = Path.home() / "Library/LaunchAgents" / f"{pin.runner_label}.plist"
    if not plist.is_file():
        return False, f"runner LaunchAgent not installed: {plist}"
    document = plistlib.loads(plist.read_bytes())
    if not isinstance(document, dict):
        return False, f"could not be determined: {plist} is not a dictionary"
    if document.get("SessionCreate"):
        # With SessionCreate the runner gets a new security session in which the
        # login keychain is locked, so codesign fails with errSecInternalComponent.
        domain = f"gui/{os.getuid()}"
        return False, (f"SessionCreate is set, which locks the login keychain for codesign. Fix: plutil -remove SessionCreate {plist}; "
                       f"launchctl bootout {domain}/{pin.runner_label}; launchctl bootstrap {domain} {plist}")
    return True, f"{plist} has no SessionCreate"


def check_runner_env(pin):
    env_file = Path(pin.runner["dir"]) / ".env"
    required = pin.runner["requiredEnv"]
    if not env_file.is_file():
        return False, f"{env_file} is missing; it must define {', '.join(required)}"
    defined = set()
    for line in env_file.read_text().splitlines():
        key, separator, _ = line.strip().partition("=")
        if separator and ENV_NAME.fullmatch(key):
            defined.add(key)
    missing = [name for name in required if name not in defined]
    if missing:
        # The stock runner installer rewrites .env and silently drops these.
        return False, (f"{env_file} does not define {', '.join(missing)}; a stock runner installer rerun drops them, "
                       "so add them back (the phone gate's morpheus credentials run needs them)")
    return True, f"{env_file} defines {', '.join(required)}"


def check_runner_groups(pin):
    organization = pin.runner["organization"]
    gh = shutil.which("gh")
    if gh is None:
        return False, "could not be determined: gh is not on PATH"
    env = {key: os.environ[key] for key in ("HOME", "PATH", "TMPDIR", "LANG", "USER", "LOGNAME", "GH_TOKEN", "GH_HOST") if key in os.environ}
    result = subprocess.run([gh, "api", f"orgs/{organization}/actions/runner-groups"], env=env, text=True,
                            capture_output=True, check=False, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode == 0:
        return True, f"runner groups of {organization} are readable"
    # Only the status is inspected; the response body is never echoed.
    if "HTTP 403" in result.stderr:
        return False, (f"403 reading {organization} runner groups: the device-runner installer's group-proof step cannot be "
                       "refreshed from this host; it needs admin:org or a browser session on the organization's runner-groups settings page")
    return False, f"could not be determined: gh api exited {result.returncode}"


def certificate_team(pem):
    result = subprocess.run(["/usr/bin/openssl", "x509", "-noout", "-subject", "-nameopt", "multiline"], input=pem,
                            text=True, capture_output=True, check=False, timeout=30, env={"PATH": "/usr/bin:/bin"})
    match = re.search(r"(?m)^\s*organizationalUnitName\s*=\s*(\S+)\s*$", result.stdout) if result.returncode == 0 else None
    return match.group(1) if match else None


def check_signing(pin):
    try:
        return _check_signing(pin)
    except (UpdateError, OSError, subprocess.TimeoutExpired, ValueError):
        return False, "could not read identities"


def _check_signing(pin):
    identity, team = pin.signing["identity"], pin.signing["team"]
    listing = run(["/usr/bin/security", "find-identity", "-v", "-p", "codesigning"])
    named = [(fingerprint, name) for fingerprint, name in re.findall(r'(?m)^\s*[0-9]+\)\s+([0-9A-F]{40})\s+"([^"]+)"', listing)
             if name.startswith(f"{identity}: ")]
    if not named:
        return False, f'no valid "{identity}" signing identity'
    # The parenthesised suffix of an Apple Development name is a personal id,
    # not the team; the team is the certificate's organizational unit.
    certificates = run(["/usr/bin/security", "find-certificate", "-a", "-Z", "-p", "-c", identity])
    pems = dict(re.findall(r"SHA-1 hash: ([0-9A-F]{40})\s*\n(-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----)", certificates, re.S))
    unknown = [fingerprint for fingerprint, _ in named if fingerprint not in pems]
    if unknown:
        return False, "could not read identities"
    teams = {fingerprint: certificate_team(pems[fingerprint]) for fingerprint, _ in named}
    if None in teams.values():
        raise UpdateError("certificate subject unreadable")
    matching = [(fingerprint, name) for fingerprint, name in named if teams[fingerprint] == team]
    if not matching:
        return False, f'no valid "{identity}" identity for team {team}'
    if len(matching) > 1:
        return False, (f'{len(matching)} valid "{identity}" identities for team {team} make the name ambiguous; '
                       f"keep one: {', '.join(fingerprint for fingerprint, _ in matching)}")
    return True, f"{matching[0][1]} {matching[0][0]}"


def check_device(pin):
    udid = pin.device_udid
    # Plain `devicectl list devices` prints CoreDevice identifiers, not UDIDs;
    # only the JSON output carries hardwareProperties.udid. It goes to a file,
    # and stdout is noise.
    with tempfile.TemporaryDirectory(prefix="factory-devicectl-") as temporary:
        output = Path(temporary) / "devices.json"
        run(["/usr/bin/xcrun", "devicectl", "list", "devices", "--json-output", str(output)], timeout=120)
        try:
            devices = json.loads(output.read_text())["result"]["devices"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise UpdateError("devicectl JSON output unreadable") from error
    if not isinstance(devices, list):
        raise UpdateError("devicectl JSON output unreadable")
    for device in devices:
        hardware = device.get("hardwareProperties") if isinstance(device, dict) else None
        if not isinstance(hardware, dict) or str(hardware.get("udid", "")).lower() != udid.lower():
            continue
        connection = device.get("connectionProperties") or {}
        name = (device.get("deviceProperties") or {}).get("name", "unnamed device")
        tunnel, pairing = connection.get("tunnelState"), connection.get("pairingState")
        detail = f"{udid} ({name}, {connection.get('transportType', 'unknown transport')}): pairing {pairing}, tunnel {tunnel}"
        # An idle wired phone reports a disconnected tunnel; it opens on demand.
        if pairing == "paired" and (tunnel == "connected" or connection.get("transportType") == "wired"):
            return True, detail
        if pairing == "paired" and tunnel == "disconnected":
            return False, f"{detail}; paired but not connected: connect and unlock the iPhone"
        return False, f"{detail}; pair and connect the iPhone"
    return False, f"{udid} is not paired to this Mac; pair and connect the iPhone"


def check_validation(pin):
    missing = [check["argv"][0] for check in pin.checks if not (Path(check["argv"][0]).is_file() and os.access(check["argv"][0], os.X_OK))]
    return (False, f"not executable: {', '.join(missing)}") if missing else (True, f"{len(pin.checks)} validation command(s) executable")


PREFLIGHT = ("xcodes", "disk", "sudo", "apple-credentials", "verification-provider", "paths")
# Checks whose failure stops Factory from acting on the host. The others
# (runner plist, runner env, org runner groups, signing, device) describe what
# the consumer's own jobs need; they are reported, and promotion validation
# fails safely on them, but they do not block installing the hourly tick.
BLOCKING = PREFLIGHT + ("ci", "runtimes", "validation")


def host_checks(pin, only=None):
    probes = [
        ("xcodes", check_xcodes, True),
        ("disk", check_disk, True),
        ("sudo", check_sudo, True),
        ("apple-credentials", check_apple, True),
        ("verification-provider", check_provider, pin.verification is not None),
        ("paths", check_paths, True),
        ("ci", check_ci, pin.role == "ci-image"),
        ("runtimes", check_runtimes, pin.role == "ci-image" and "runtimes" in (pin.ci or {})),
        ("runner", check_runner, pin.runner_label is not None),
        ("runner-env", check_runner_env, bool(pin.runner and pin.runner.get("dir") and pin.runner.get("requiredEnv"))),
        ("runner-groups", check_runner_groups, bool(pin.runner and pin.runner.get("organization"))),
        ("signing", check_signing, pin.signing is not None),
        ("device", check_device, pin.device_udid is not None),
        ("validation", check_validation, True),
    ]
    results = []
    for name, probe, applies in probes:
        if not applies or (only is not None and name not in only):
            continue
        try:
            ok, detail = probe(pin)
        except UpdateError as error:
            ok, detail = False, f"could not be determined: {error}"
        except Exception as error:  # A check reports; it never aborts the others.
            ok, detail = False, f"could not be determined: {type(error).__name__}"
        results.append({"name": name, "ok": bool(ok), "blocking": name in BLOCKING, "detail": detail})
    return results


def preflight(pin):
    failed = [check["name"] for check in host_checks(pin, PREFLIGHT) if not check["ok"]]
    if failed:
        raise UpdateError("preflight: " + "; ".join(failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("auth-check", "auth-login", "status", "check", "stage", "validate", "activate", "reconcile", "image-stage", "retire"))
    parser.add_argument("--manifest")
    parser.add_argument("--host")
    parser.add_argument("--state-dir", default=str(Path.home() / ".local/state/morpheus-factory/updates"))
    parser.add_argument("--json", action="store_true", help="check: print JSON (the default output is already JSON)")
    args = parser.parse_args()
    if args.command in ("auth-check", "auth-login"):
        verification, apple = None, None
        if args.manifest or args.host:
            if not args.manifest or not args.host or not HOST.fullmatch(args.host):
                raise UpdateError(f"{args.command} requires both --manifest and --host when either is supplied")
            pin = pin_for_host(args.manifest, args.host)
            verification, apple = pin.verification, pin.apple
        result = check_authentication(verification, apple) if args.command == "auth-check" else interactive_login(apple)
        print(json.dumps({"result": result}))
        return 0
    if not args.host or not HOST.fullmatch(args.host):
        raise UpdateError("host id must use lowercase letters, digits, and hyphens")
    if not args.manifest:
        raise UpdateError("--manifest is required")
    pin = pin_for_host(args.manifest, args.host)
    version, build, checks, phase, verification, components, apple = (
        pin.version, pin.build, pin.checks, pin.phase, pin.verification, pin.components, pin.apple)
    if args.command == "status":
        record = promotion(args.state_dir, version, build)
        print(json.dumps({**report(version, build), "promotedAt": record and record["promotedAt"],
                          "retiredAt": record and record.get("retiredAt")}, indent=2))
        return 0
    if args.command == "check":
        results = host_checks(pin)
        ok = all(check["ok"] for check in results)
        print(json.dumps({"ok": ok, "checks": results}, indent=2))
        return 0 if ok else 1
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (state_dir / "controller.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UpdateError("another update is in progress") from error
        if args.command in ("stage", "image-stage", "reconcile"):
            # Refuse before a multi-GB download rather than after it.
            preflight(pin)
        extra = {}
        if args.command == "image-stage":
            ci = ci_for_host(pin)
            result = stage_ci_image(version, build, state_dir, verification, components, ci, apple)
        elif args.command == "stage":
            result = stage(version, build, state_dir, verification, components, apple)
        elif args.command == "validate":
            result = validate(version, build, checks)
        elif args.command == "reconcile":
            before = report(version, build)
            result = reconcile(version, build, checks, phase, state_dir, verification, components, apple, before=before)
            if result in ("promoted", "already-current"):
                record = promotion(state_dir, version, build)
                extra["removed"] = prune(version, build, pin.retain, [before["selected"], record and record.get("previous")],
                                         window=in_window(state_dir, version, build)) or []
        elif args.command == "retire":
            result, extra["removed"] = retire(pin, state_dir)
        else:
            result = activate(version, build, state_dir)
        print(json.dumps({"result": result, "host": args.host, "xcode": {"version": version, "build": build}, **extra}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (UpdateError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"update-controller: {error}", file=sys.stderr)
        sys.exit(1)
