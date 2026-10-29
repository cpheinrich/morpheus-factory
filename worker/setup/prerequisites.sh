#!/bin/bash
set -euo pipefail
[[ "$(uname -s)" == Darwin ]] || { echo "macOS required" >&2; exit 1; }
[[ "$(id -u)" != 0 ]] || { echo "Run as the worker's login user" >&2; exit 1; }
xcode-select -p >/dev/null 2>&1 || {
  echo "Apple command-line tools are required (xcode-select --install)" >&2
  exit 1
}
echo "macOS and command-line tools ready"
