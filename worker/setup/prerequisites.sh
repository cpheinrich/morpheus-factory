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
  # Report only: installing the root helper needs a password, so the operator does it once.
  helper=/usr/local/libexec/morpheus-factory-xcode
  if [[ -x "$helper" ]] && sudo -n -l "$helper" >/dev/null 2>&1; then
    echo "root helper: $helper installed and allowed"
  else
    echo "root helper: missing or not allowed — Xcode activation and first launch need it; see docs/host-updates.md"
  fi
fi
