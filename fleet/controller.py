#!/usr/bin/env python3
"""Reconcile an approved Xcode pin across host image, consumer, and host selection.

The manifest on Factory main is the authority. Each tick does one bounded step;
the next tick resumes from observed repository and host state. Agent work happens
in isolated worktrees and must pass the repositories' normal PR gates.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}\Z")
BUILD = re.compile(r"[A-Za-z0-9]+\Z")
EVO_WORKFLOWS = {
    ".github/workflows/ios-ci.yml": 1,
    ".github/workflows/ios-full-verification.yml": 1,
    ".github/workflows/ios-nightly-build.yml": 2,
    ".github/workflows/ios-production-release.yml": 2,
}


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


def read_pin(manifest: Path, host: str) -> Pin:
    data = json.loads(manifest.read_text())
    if data.get("schemaVersion") != 1:
        raise FleetError("unsupported Factory update manifest")
    xcode = data.get("hosts", {}).get(host, {}).get("xcode", {})
    version, build, phase = xcode.get("version"), xcode.get("build"), xcode.get("phase", "stage")
    if not isinstance(version, str) or not VERSION.fullmatch(version) or not isinstance(build, str) or not BUILD.fullmatch(build) or phase not in ("stage", "promote"):
        raise FleetError("invalid exact Xcode pin or phase")
    return Pin(version, build, phase)


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


def evo_has_pin(evo: Path, pin: Pin) -> bool:
    """Inspect merged Evo main; a PR branch alone never advances the fleet."""
    command(["git", "-C", str(evo), "fetch", "origin", "main"])
    for path, expected_count in EVO_WORKFLOWS.items():
        try:
            body = git_file(evo, "origin/main", path)
        except FleetError:
            return False
        versions = re.findall(r"(?m)^\s*xcode-version:\s*['\"]?([0-9]+(?:\.[0-9]+){1,2})", body)
        if len(versions) != expected_count or any(version != pin.version for version in versions):
            return False
        if f"xcode-{pin.version}" not in body:
            return False
        if path.endswith(("ios-nightly-build.yml", "ios-production-release.yml")) and "assert-xcode-build.sh --require-final" not in body:
            return False
    try:
        assertion = git_file(evo, "origin/main", "apps/ios/scripts/ci/assert-xcode-build.sh")
        prep = git_file(evo, "origin/main", "apps/ios/scripts/ci/prepare-tests.sh")
    except FleetError:
        return False
    return f"Xcode {pin.version}" in assertion and f"Build version {pin.build}" in assertion and "assert-xcode-build.sh" in prep


def host_status(factory: Path, manifest: Path, host: str) -> dict:
    raw = command([sys.executable, str(factory / "worker/update_controller.py"), "status", "--host", host, "--manifest", str(manifest)])
    status = json.loads(raw)
    if not isinstance(status, dict) or not isinstance(status.get("installedMatches"), bool) or not isinstance(status.get("selectedMatches"), bool):
        raise FleetError("invalid host update status")
    return status


def host_action(factory: Path, evo: Path, manifest: Path, host: str, action: str) -> None:
    if action not in ("stage", "image-stage", "reconcile"):
        raise FleetError("invalid host action")
    # The credentials companion injects Apple login only into this child. The
    # GitHub token used by the fleet PR agent is excluded from that child.
    excluded = ("GH_TOKEN", "FACTORY_GH_TOKEN", "GIT_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")
    env = {key: value for key, value in os.environ.items() if key not in excluded}
    command(["morpheus", "credentials", "run", "--", sys.executable,
             str(factory / "worker/update_controller.py"), action,
             "--host", host, "--manifest", str(manifest)], cwd=evo, env=env,
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
    if path.exists():
        state = json.loads(path.read_text())
    else:
        state = {}
    return state if state.get("pin") == pin.key else {"pin": pin.key, "sessions": {}}


def save_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(state, output, sort_keys=True)
        output.write("\n")
    os.replace(temp, path)


def agent_prompt(step: str, pin: Pin, factory: Path, evo: Path, host_setup: Path) -> str:
    common = (
        f"Approved Xcode pin: version {pin.version}, build {pin.build}. "
        f"Factory: {factory}; Evo: {evo}; agent-host-setup: {host_setup}. "
        "Read each repository's AGENTS.md. Work in isolated branches/worktrees, never push protected main. "
        "Keep secrets and SMS codes out of files, arguments, output, and PRs. "
        "Finish focused tests, independent review when required, CI and merge under normal repository rules. "
        "Do not relax exact version/build checks. Be idempotent: continue an existing pin PR instead of opening a duplicate. "
    )
    if step == "consumer":
        return common + (
            "Update Evo iOS CI, full verification, nightly and production workflows to the approved pin and xcode-version runner pool; "
            "update the exact-build assertion and its tests. Preserve the CD V2 MacBook Pro lane until that host has a verified image. "
            "Run Evo PR CI on the new image, then merge the reviewed PR. Report the merged commit and remaining host gaps."
        )
    return common + (
        "After Evo main contains and CI has verified the exact pin, change Factory's host manifest phase from stage to promote. "
        "Run Factory controller tests, open and merge the reviewed promotion PR. Do not switch Xcode directly in this task; "
        "the host updater will reconcile the merged manifest with validation and rollback."
    )


def run_agent(step: str, pin: Pin, factory: Path, evo: Path, host_setup: Path, state: dict, state_path: Path) -> None:
    prompt = agent_prompt(step, pin, factory, evo, host_setup)
    session = state.setdefault("sessions", {}).get(step)
    base = ["codex", "exec", "--json", "--approve-for-me", "-C", str(factory), "--add-dir", str(evo), "--add-dir", str(host_setup)]
    argv = base + (["resume", session, "-"] if session else ["--worktree", "-"])
    with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as process:
        watchdog = threading.Timer(7200, lambda: process.poll() is None and process.kill())
        watchdog.daemon = True
        watchdog.start()
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            completed = False
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                    state["sessions"][step] = event["thread_id"]
                    save_state(state_path, state)
                if event.get("type") == "turn.completed":
                    completed = True
            exit_code = process.wait()
        finally:
            watchdog.cancel()
    if exit_code or not completed:
        raise FleetError(f"{step} agent did not complete; session retained for the next tick")


def reconcile(factory: Path, evo: Path, host_setup: Path, manifest: Path, host: str, state_dir: Path) -> str:
    pin = read_pin(manifest, host)
    state_path = state_dir / "fleet.json"
    state = private_state(state_path, pin)
    status = host_status(factory, manifest, host)
    if not status["installedMatches"]:
        host_action(factory, evo, manifest, host, "stage")
        status = host_status(factory, manifest, host)
        if not status["installedMatches"]:
            return "waiting-host-stage"
    sync_host_tools(host_setup)
    # The host-side command verifies the exact guest build and activates the
    # repo-specific runner pool as one idempotent transaction.
    host_action(factory, evo, manifest, host, "image-stage")
    if not evo_has_pin(evo, pin):
        run_agent("consumer", pin, factory, evo, host_setup, state, state_path)
        if not evo_has_pin(evo, pin):
            return "waiting-consumer-merge"
    if pin.phase != "promote":
        run_agent("promote", pin, factory, evo, host_setup, state, state_path)
        return "waiting-promotion-merge"
    host_action(factory, evo, manifest, host, "reconcile")
    status = host_status(factory, manifest, host)
    if not status["selectedMatches"]:
        return "waiting-host-promotion"
    return "converged"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("status", "reconcile"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--factory-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evo-root", type=Path, default=Path.home() / "code/evo")
    parser.add_argument("--host-setup-root", type=Path, default=Path.home() / "code/agent-host-setup")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/morpheus-factory/fleet")
    args = parser.parse_args()
    factory = args.factory_root.resolve()
    manifest = factory / "worker/updates.json"
    pin = read_pin(manifest, args.host)
    if args.command == "status":
        print(json.dumps({"pin": pin.key, "phase": pin.phase, "host": host_status(factory, manifest, args.host)}, sort_keys=True))
        return 0
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state_dir / "fleet.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FleetError("another fleet reconciliation is running") from error
        result = reconcile(factory, args.evo_root.resolve(), args.host_setup_root.resolve(), manifest, args.host, args.state_dir)
    print(json.dumps({"pin": pin.key, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FleetError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"fleet-controller: {error}", file=sys.stderr)
        raise SystemExit(1)
