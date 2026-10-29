#!/bin/bash
set -euo pipefail
if ((WORKER_CHECK_ONLY)); then
  for tool in git brew node pnpm gh xcrun; do
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
node_major="$(node -p 'process.versions.node.split(".")[0]')"
((node_major >= 22)) || { echo "Node 22 or newer required" >&2; exit 1; }
echo "Git, Node, pnpm and gh ready"
