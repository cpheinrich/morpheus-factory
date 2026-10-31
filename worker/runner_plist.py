#!/usr/bin/env python3
"""Render the Factory-owned LaunchAgent for a persistent GitHub Actions runner.

The stock `svc.sh install` sets SessionCreate, which gives the runner a fresh
security session where the login keychain is locked and `codesign` fails with
errSecInternalComponent. Factory writes this plist instead, so the key can
never come back on reinstall.
"""
import argparse
import os
import plistlib
import re
import sys

LABEL_PREFIX = "com.morpheus-factory.runner."
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
RESERVED_NAMES = {"evo"}  # ~/actions-runner-evo belongs to agent-host-setup.
RUNNER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def validate_name(name):
    if not NAME_PATTERN.match(name) or ".." in name:
        raise ValueError(f"invalid runner name: {name!r}")
    if name in RESERVED_NAMES:
        raise ValueError(f"runner name {name!r} is reserved")
    return name


def _absolute(label, path):
    if not os.path.isabs(path) or ".." in path.split("/"):
        raise ValueError(f"{label} must be an absolute path without '..': {path!r}")
    return path.rstrip("/") or "/"


def label_for(name):
    return LABEL_PREFIX + validate_name(name)


def render(name, runner_dir, log_dir):
    label = label_for(name)
    runner_dir = _absolute("runner dir", runner_dir)
    logs = f"{_absolute('log dir', log_dir)}/{label}"
    # No SessionCreate: the runner must share the GUI login session's keychain.
    return {
        "Label": label,
        "ProgramArguments": [f"{runner_dir}/runsvc.sh"],
        "WorkingDirectory": runner_dir,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": RUNNER_PATH, "ACTIONS_RUNNER_SVC": "1"},
        "StandardOutPath": f"{logs}/stdout.log",
        "StandardErrorPath": f"{logs}/stderr.log",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--runner-dir", required=True)
    parser.add_argument("--log-dir", required=True)
    args = parser.parse_args(argv)
    try:
        plist = render(args.name, args.runner_dir, args.log_dir)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    sys.stdout.buffer.write(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    return 0


if __name__ == "__main__":
    sys.exit(main())
