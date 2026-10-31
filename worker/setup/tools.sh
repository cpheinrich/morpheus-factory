#!/bin/bash
set -euo pipefail
if ((WORKER_CHECK_ONLY)); then
  for tool in git brew node pnpm gh xcrun python3 xcodes; do
    if command -v "$tool" >/dev/null 2>&1; then echo "$tool ready"; else echo "$tool missing"; fi
  done
  exit 0
fi

command -v brew >/dev/null 2>&1 || {
  echo "Homebrew is required; install it and rerun worker/setup.sh" >&2
  exit 1
}
for tool in git node pnpm gh; do
  if ! command -v "$tool" >/dev/null 2>&1; then brew install "$tool"; fi
done
# xcodes is not in homebrew-core; the Xcode update controller needs it.
if ! command -v xcodes >/dev/null 2>&1; then
  brew tap xcodesorg/made
  brew install xcodesorg/made/xcodes
fi
# python3 comes with the Apple command-line tools; every controller uses it.
command -v python3 >/dev/null 2>&1 || { echo "python3 missing; reinstall the command-line tools (xcode-select --install)" >&2; exit 1; }
node_major="$(node -p 'process.versions.node.split(".")[0]')"
((node_major >= 22)) || { echo "Node 22 or newer required" >&2; exit 1; }
echo "Git, Node, pnpm, gh, python3 and xcodes ready"
