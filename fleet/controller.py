#!/usr/bin/env python3
"""Reconcile an approved Xcode pin across host image, consumer, and host selection.

The manifest on Factory main is the authority. Each tick does one bounded step;
the next tick starts again from observed repository and host state. Consumer
changes are deterministic draft PRs; a fresh, time-boxed agent only finishes a
PR that cannot merge by itself, at most AGENT_RUN_CAP times per pin.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
BUILD = re.compile(r"[A-Za-z0-9]+\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
ROLES = ("ci-image", "phone-gate")
AGENT_RUN_CAP = 3
AGENT_TIMEOUT_SECONDS = 7200
DEFAULT_ASSERTION = "apps/ios/scripts/ci/assert-xcode-build.sh"
WORKFLOW_DIR = ".github/workflows/"
# Values are captured whole, so a templated or stray value fails the pin check
# instead of being skipped.
XCODE_VERSION_VALUE = re.compile(r"(?m)^[\s-]*xcode-version:\s*['\"]?([^'\"\s#]+)")
RUNNER_LINE = re.compile(r"(?m)^[\s-]*(?:runner|runs-on):[^\n]*")
RUNNER_LABEL = re.compile(r"(?<![\w-])xcode-([A-Za-z0-9._]+)")
# A version token ends where neither a word character nor ".<digit>" follows,
# so 27.2 does not match inside 27.21 or 27.2.1.
VERSION_END = r"(?![\w]|\.\d)"


class FleetError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    version: str
    build: str
    phase: str

    @property
    def key(self) -> str:
        return f"{self.version}-{self.build}"


@dataclass(frozen=True)
class Consumer:
    repository: str = "darwin-health/evo"
    assertion: str = DEFAULT_ASSERTION
    pin_paths: tuple[str, ...] = (WORKFLOW_DIR + "*.yml", DEFAULT_ASSERTION)


@dataclass(frozen=True)
class HostConfig:
    pin: Pin
    role: str
    has_ci: bool
    consumer: Consumer = field(default_factory=Consumer)
    paths: dict = field(default_factory=dict)


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or ".." in Path(value).parts:
        raise FleetError("consumer paths must be relative and stay inside the repository")
    return value


def read_consumer(data: dict) -> Consumer:
    raw = data.get("consumer")
    if raw is None:
        return Consumer()
    if not isinstance(raw, dict):
        raise FleetError("invalid consumer manifest entry")
    default = Consumer()
    repository = raw.get("repository", default.repository)
    if not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        raise FleetError("consumer repository must be owner/name")
    assertion = _relative_path(raw.get("assertion", default.assertion))
    pin_paths = raw.get("pinPaths", list(default.pin_paths))
    if not isinstance(pin_paths, list) or not pin_paths:
        raise FleetError("consumer pinPaths must be a non-empty list")
    return Consumer(repository, assertion, tuple(_relative_path(path) for path in pin_paths))


def read_host(manifest: Path, host: str) -> HostConfig:
    data = json.loads(manifest.read_text())
    if data.get("schemaVersion") != 1:
        raise FleetError("unsupported Factory update manifest")
    entry = data.get("hosts", {}).get(host, {})
    xcode = entry.get("xcode", {})
    version, build, phase = xcode.get("version"), xcode.get("build"), xcode.get("phase", "promote")
    if not isinstance(version, str) or not VERSION.fullmatch(version) or not isinstance(build, str) or not BUILD.fullmatch(build) or phase not in ("stage", "promote"):
        raise FleetError("invalid exact Xcode pin or phase")
    has_ci = isinstance(entry.get("ci"), dict)
    role = entry.get("role", "ci-image" if has_ci else "phone-gate")
    if role not in ROLES or (role == "ci-image") != has_ci:
        raise FleetError("host role must be ci-image with a ci block, or phone-gate without one")
    paths = entry.get("paths", {})
    if not isinstance(paths, dict) or any(not isinstance(value, str) or not value.startswith("/") for value in paths.values()):
        raise FleetError("host paths must be absolute")
    return HostConfig(Pin(version, build, phase), role, has_ci, read_consumer(data), dict(paths))


def read_pin(manifest: Path, host: str) -> Pin:
    return read_host(manifest, host).pin


def command(argv: list[str], *, cwd: Path | None = None, input_text: str | None = None, env: dict | None = None, timeout: int = 600) -> str:
    try:
        result = subprocess.run(argv, cwd=cwd, input=input_text, env=env, text=True, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise FleetError(f"command timed out: {Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''}") from error
    if result.returncode:
        raise FleetError(f"command failed ({result.returncode}): {Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''}")
    return result.stdout.strip()


def git_file(repo: Path, ref: str, path: str) -> str:
    return command(["git", "-C", str(repo), "show", f"{ref}:{path}"])


def consumer_has_pin(root: Path, pin: Pin, consumer: Consumer) -> bool:
    """Inspect merged consumer main; a PR branch alone never advances the fleet."""
    command(["git", "-C", str(root), "fetch", "origin", "main"])
    listing = command(["git", "-C", str(root), "ls-tree", "--name-only", "origin/main", WORKFLOW_DIR])
    pinned_somewhere = False
    for path in (line.strip() for line in listing.splitlines()):
        if not path.endswith(".yml"):
            continue
        body = git_file(root, "origin/main", path)
        versions = XCODE_VERSION_VALUE.findall(body)
        if any(version != pin.version for version in versions):
            return False
        labels = [label for line in RUNNER_LINE.findall(body) for label in RUNNER_LABEL.findall(line)]
        if any(label != pin.version for label in labels):
            return False
        pinned_somewhere = pinned_somewhere or bool(versions)
    # An empty match set is not a pass: nothing pinned is not correctly pinned.
    if not pinned_somewhere:
        return False
    try:
        assertion = git_file(root, "origin/main", consumer.assertion)
    except FleetError:
        return False
    return f"Xcode {pin.version}" in assertion and f"Build version {pin.build}" in assertion


def assertion_pin(assertion: str) -> tuple[str, str]:
    version = re.search(r"\bXcode ([0-9]+(?:\.[0-9]+){1,2})" + VERSION_END, assertion)
    build = re.search(r"\bBuild version ([A-Za-z0-9]+)\b", assertion)
    if not version or not build:
        raise FleetError("cannot read the current Xcode version and build from the consumer assertion")
    return version.group(1), build.group(1)


def substitute_pin(text: str, old: tuple[str, str], new: Pin) -> str:
    old_version, old_build = (re.escape(part) for part in old)
    text = re.sub(r"(xcode-version:\s*['\"]?)" + old_version + VERSION_END, lambda m: m.group(1) + new.version, text)
    text = re.sub(r"(?<![\w-])xcode-" + old_version + VERSION_END, "xcode-" + new.version, text)
    text = re.sub(r"\bXcode_" + old_version + r"\.app", f"Xcode_{new.version}.app", text)
    text = re.sub(r"\bXcode " + old_version + VERSION_END, "Xcode " + new.version, text)
    text = re.sub(r"\bBuild version " + old_build + r"\b", "Build version " + new.build, text)
    return re.sub(r"\b" + old_build + r"\b", new.build, text)


def rewrite_pin_files(worktree: Path, tracked: set[str], patterns: tuple[str, ...], old: tuple[str, str], new: Pin) -> list[str]:
    changed = []
    candidates = sorted({path.relative_to(worktree).as_posix() for pattern in patterns for path in worktree.glob(pattern) if path.is_file()})
    for relative in candidates:
        if relative not in tracked:
            continue
        path = worktree / relative
        before = path.read_text()
        after = substitute_pin(before, old, new)
        if after != before:
            path.write_text(after)
            changed.append(relative)
    if not changed:
        raise FleetError("consumer pin substitution changed nothing")
    return changed


def pin_branch(pin: Pin) -> str:
    return f"factory/xcode-{pin.version}-{pin.build}"


def open_pin_pr(root: Path, pin: Pin, consumer: Consumer) -> str:
    """Create the substitution branch from origin/main and push it."""
    branch = pin_branch(pin)
    old = assertion_pin(git_file(root, "origin/main", consumer.assertion))
    worktree = Path(tempfile.mkdtemp(prefix="factory-pin-"))
    try:
        command(["git", "-C", str(root), "worktree", "add", "--detach", str(worktree), "origin/main"])
        tracked = set(command(["git", "-C", str(worktree), "ls-files"]).splitlines())
        changed = rewrite_pin_files(worktree, tracked, consumer.pin_paths, old, pin)
        command(["git", "-C", str(worktree), "checkout", "-B", branch])
        command(["git", "-C", str(worktree), "add", "--", *changed])
        command(["git", "-C", str(worktree), "-c", "user.name=morpheus-factory", "-c", "user.email=noreply@github.com",
                 "commit", "-m", f"Pin iOS CI to Xcode {pin.version} (build {pin.build})"])
        command(["git", "-C", str(worktree), "push", "-u", "origin", branch])
    finally:
        try:
            command(["git", "-C", str(root), "worktree", "remove", "--force", str(worktree)])
        except FleetError:
            pass
        shutil.rmtree(worktree, ignore_errors=True)
    return "\n".join(f"- `{path}`" for path in changed)


def ensure_consumer_pr(root: Path, pin: Pin, consumer: Consumer) -> tuple[str, bool]:
    """Return (PR URL, opened this tick). Never rewrites an existing branch or PR."""
    branch = pin_branch(pin)
    listed = json.loads(command(["gh", "pr", "list", "--repo", consumer.repository, "--head", branch, "--state", "open",
                                 "--json", "number,url,isDraft,mergeStateStatus"]) or "[]")
    if listed:
        return listed[0]["url"], False
    if command(["git", "-C", str(root), "ls-remote", "--heads", "origin", branch]):
        files = "See the branch diff; the branch already existed when this PR was opened."
    else:
        files = open_pin_pr(root, pin, consumer)
    body = (f"Factory pin substitution for the approved Xcode {pin.version} (build {pin.build}) from "
            "morpheus-factory `worker/updates.json`. It rewrites the exact version, runner label and build "
            f"in the configured pin paths; nothing else changes.\n\nFiles changed:\n{files}\n\n"
            "🤖 Generated with Factory\n")
    output = command(["gh", "pr", "create", "--repo", consumer.repository, "--draft", "--head", branch, "--base", "main",
                      "--title", f"Pin iOS CI to Xcode {pin.version} (build {pin.build})", "--body", body])
    return output.splitlines()[-1].strip(), True


def host_status(factory: Path, manifest: Path, host: str) -> dict:
    raw = command([sys.executable, str(factory / "worker/update_controller.py"), "status", "--host", host, "--manifest", str(manifest)])
    status = json.loads(raw)
    if not isinstance(status, dict) or not isinstance(status.get("installedMatches"), bool) or not isinstance(status.get("selectedMatches"), bool):
        raise FleetError("invalid host update status")
    return status


def host_action(factory: Path, credentials_project: Path, manifest: Path, host: str, action: str) -> None:
    if action not in ("stage", "image-stage", "reconcile"):
        raise FleetError("invalid host action")
    # The credentials companion injects Apple login only into this child.
    # Image staging also needs GitHub auth to fetch CI runner/action packages;
    # the worker scopes its own builder/verifier/activator children separately.
    excluded = () if action == "image-stage" else (
        "GH_TOKEN", "FACTORY_GH_TOKEN", "GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    command(["morpheus", "credentials", "run", "--", sys.executable,
             str(factory / "worker/update_controller.py"), action,
             "--host", host, "--manifest", str(manifest)], cwd=credentials_project, env=env,
            timeout=14400 if action in ("stage", "image-stage") else 7200)


def sync_host_tools(host_setup: Path) -> None:
    if command(["git", "-C", str(host_setup), "branch", "--show-current"]) != "main":
        raise FleetError("agent-host-setup is not on main")
    if command(["git", "-C", str(host_setup), "status", "--porcelain"]):
        raise FleetError("agent-host-setup is dirty; host tool update deferred")
    command(["git", "-C", str(host_setup), "fetch", "origin", "main"])
    command(["git", "-C", str(host_setup), "merge", "--ff-only", "origin/main"])


def private_state(path: Path, pin: Pin) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = json.loads(path.read_text()) if path.exists() else {}
    runs = state.get("agentRuns") if state.get("pin") == pin.key else None
    if not isinstance(runs, list) or not all(isinstance(run, str) for run in runs):
        runs = []
    return {"pin": pin.key, "agentRuns": runs}


def save_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(state, output, sort_keys=True)
        output.write("\n")
    os.replace(temp, path)


def agent_prompt(pin: Pin, pr_url: str, consumer: Consumer) -> str:
    return (
        f"Finish the existing consumer pin PR {pr_url} in {consumer.repository}. "
        f"It pins iOS CI to Xcode {pin.version}, build {pin.build}; do not change that pin or relax exact version/build checks. "
        "Read the repository's AGENTS.md and follow its rules. "
        "Finish this PR: run the focused tests, obtain the independent review record if the repository requires one, "
        "mark it ready, let CI pass and merge it under normal rules. "
        "Never push protected main. Do not open another PR. Keep secrets out of files and output."
    )


def run_agent(pin: Pin, pr_url: str, consumer: Consumer, factory: Path, root: Path) -> None:
    """One fresh, time-boxed agent run. No session is persisted or resumed."""
    argv = ["codex", "exec", "--json", "--approve-for-me", "-C", str(root), "--add-dir", str(factory), "--worktree", "-"]
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
        watchdog = threading.Timer(AGENT_TIMEOUT_SECONDS, lambda: process.poll() is None and process.kill())
        watchdog.daemon = True
        watchdog.start()
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(agent_prompt(pin, pr_url, consumer))
            process.stdin.close()
            completed = False
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    completed = True
            exit_code = process.wait()
        finally:
            watchdog.cancel()
    if exit_code or not completed:
        raise FleetError("consumer pin agent did not complete")


def advance_consumer(config: HostConfig, factory: Path, root: Path, state_path: Path) -> str | None:
    """Return a waiting result, or None once merged consumer main carries the pin."""
    pin, consumer = config.pin, config.consumer
    if consumer_has_pin(root, pin, consumer):
        return None
    # Only the image host drives the shared PR; others would duplicate agent runs.
    if not config.has_ci:
        return "waiting-consumer-merge"
    url, opened = ensure_consumer_pr(root, pin, consumer)
    if opened:
        return "waiting-consumer-pr"
    state = private_state(state_path, pin)
    if len(state["agentRuns"]) >= AGENT_RUN_CAP:
        return f"waiting-human: consumer pin PR {url} needs attention"
    # Record before starting, so a crashed or killed run still counts.
    state["agentRuns"].append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    save_state(state_path, state)
    run_agent(pin, url, consumer, factory, root)
    return None if consumer_has_pin(root, pin, consumer) else "waiting-consumer-merge"


def reconcile(factory: Path, consumer_root: Path, host_setup: Path, manifest: Path, host: str, state_dir: Path, credentials_project: Path | None = None) -> str:
    config = read_host(manifest, host)
    credentials_project = credentials_project or consumer_root
    status = host_status(factory, manifest, host)
    if not status["installedMatches"]:
        host_action(factory, credentials_project, manifest, host, "stage")
        status = host_status(factory, manifest, host)
        if not status["installedMatches"]:
            return "waiting-host-stage"
    if config.has_ci:
        sync_host_tools(host_setup)
        # The host-side command verifies the exact guest build and activates the
        # repo-specific runner pool as one idempotent transaction.
        host_action(factory, credentials_project, manifest, host, "image-stage")
    waiting = advance_consumer(config, factory, consumer_root, state_dir / "fleet.json")
    if waiting:
        return waiting
    if config.pin.phase == "stage":
        return "held-at-stage"
    host_action(factory, credentials_project, manifest, host, "reconcile")
    status = host_status(factory, manifest, host)
    if not status["selectedMatches"]:
        return "waiting-host-promotion"
    return "host-converged"


def resolve_paths(args: argparse.Namespace, config: HostConfig) -> tuple[Path, Path, Path]:
    """CLI flags win, then manifest host paths, then the historical defaults."""
    def pick(flag: Path | None, key: str, fallback: Path) -> Path:
        return (flag or (Path(config.paths[key]) if key in config.paths else fallback)).resolve()
    consumer = pick(args.consumer_root, "consumer", Path.home() / "code/evo")
    host_setup = pick(args.host_setup_root, "hostSetup", Path.home() / "code/agent-host-setup")
    credentials = pick(args.credentials_project, "credentialsProject", consumer)
    return consumer, host_setup, credentials


def consumer_pinned(root: Path, config: HostConfig) -> bool | None:
    if not root.is_dir():
        return None
    try:
        return consumer_has_pin(root, config.pin, config.consumer)
    except (FleetError, OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "reconcile"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--factory-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--consumer-root", "--evo-root", dest="consumer_root", type=Path,
                        help="consumer checkout; defaults to the host's manifest paths.consumer, then ~/code/evo")
    parser.add_argument("--credentials-project", type=Path, help="host-scoped Morpheus credentials project; defaults to paths.credentialsProject, then the consumer checkout")
    parser.add_argument("--host-setup-root", type=Path, help="defaults to paths.hostSetup, then ~/code/agent-host-setup")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/morpheus-factory/fleet")
    args = parser.parse_args()
    factory = args.factory_root.resolve()
    manifest = factory / "worker/updates.json"
    config = read_host(manifest, args.host)
    consumer_root, host_setup, credentials_project = resolve_paths(args, config)
    if args.command == "status":
        print(json.dumps({"pin": config.pin.key, "phase": config.pin.phase, "role": config.role,
                          "consumerPinned": consumer_pinned(consumer_root, config),
                          "host": host_status(factory, manifest, args.host)}, sort_keys=True))
        return 0
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / "fleet.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FleetError("another fleet reconciliation is running") from error
        result = reconcile(factory, consumer_root, host_setup, manifest, args.host, args.state_dir, credentials_project)
    print(json.dumps({"pin": config.pin.key, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FleetError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"fleet-controller: {error}", file=sys.stderr)
        raise SystemExit(1)
