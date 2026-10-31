#!/bin/bash
set -euo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo "macOS required" >&2; exit 1; }
[[ "$(id -u)" != 0 ]] || { echo "Run as the worker's login user" >&2; exit 1; }
xcode-select -p >/dev/null 2>&1 || {
  echo "Apple command-line tools are required (xcode-select --install)" >&2
  exit 1
}
echo "macOS and command-line tools ready"
if ((WORKER_CHECK_ONLY)); then
  # Report only: the sudoers rule needs a password, so the operator adds it once.
  if sudo -n true 2>/dev/null; then
    echo "sudo -n: ready"
  else
    echo "sudo -n: password required — Xcode staging and activation need passwordless sudo for exact commands; see docs/host-updates.md"
  fi
fi
