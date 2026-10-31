#!/usr/bin/env python3
"""Build the freshly fetched Evo main in an isolated, disposable worktree."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}$")


def command(argv, *, cwd=None, env=None):
    if env is None:
        env = {key: value for key, value in os.environ.items() if key != "DEVELOPER_DIR"}
    return subprocess.run(argv, cwd=cwd, env=env, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def validate(repo: Path, device_gate: str = "") -> str:
    if device_gate and not UDID.fullmatch(device_gate):
        raise ValueError("device gate requires a hardware UDID")
    developer = os.environ.get("DEVELOPER_DIR", "")
    if not Path(developer).is_absolute() or not Path(developer, "usr/bin/xcodebuild").is_file():
        raise ValueError("DEVELOPER_DIR must select an installed Xcode app")
    repo = repo.resolve()
    sha = command(["git", "-C", str(repo), "rev-parse", "--verify", "origin/main^{commit}"])
    scratch = Path(tempfile.mkdtemp(prefix="factory-evo-main-"))
    checkout = scratch / "evo"
    added = False
    try:
        command(["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout), sha])
        added = True
        script = checkout / "apps/ios/scripts/dev.sh"
        if not script.is_file():
            raise RuntimeError("Evo main is missing its iOS build script")
        # The host updater already limited this environment and checked the
        # exact Xcode identity; the build sees only that selected toolchain.
        # A phone-gate host proves the toolchain with a signed build for its
        # connected iPhone, the same build its delivery gate runs.
        action = ["build-device-gate", device_gate] if device_gate else ["build"]
        subprocess.run([str(script), *action], cwd=checkout / "apps/ios", check=True)
        return sha
    finally:
        try:
            if added:
                command(["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)])
        finally:
            shutil.rmtree(scratch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--device-gate", metavar="UDID", default="", help="run a signed device-gate build for this iPhone")
    args = parser.parse_args()
    if args.device_gate and not UDID.fullmatch(args.device_gate):
        parser.error("--device-gate requires a hardware UDID")
    print(f"validated Evo main {validate(args.repo, args.device_gate)[:12]}")


if __name__ == "__main__":
    main()
