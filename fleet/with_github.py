#!/usr/bin/env python3
"""Pass only a GitHub token to a fleet command, without printing or storing it."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.user):
        parser.error("invalid GitHub user")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("missing command")
    token_result = subprocess.run(["gh", "auth", "token", "-u", args.user], text=True, capture_output=True, check=False)
    token = token_result.stdout.strip()
    if token_result.returncode or not token:
        print("fleet-github: GitHub credential is unavailable", file=sys.stderr)
        return 78
    with tempfile.TemporaryDirectory(prefix="factory-gh-") as directory:
        askpass = Path(directory) / "askpass.sh"
        askpass.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token;; *Password*) printf "%s\\n" "$FACTORY_GH_TOKEN";; esac\n')
        askpass.chmod(0o700)
        allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TERM", "CODEX_HOME", "GH_CONFIG_DIR")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update(GH_TOKEN=token, FACTORY_GH_TOKEN=token,
                   GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0",
                   GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0="")
        return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
