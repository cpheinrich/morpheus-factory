#!/bin/bash
# Install the reviewed Morpheus CLI used for guarded task-worktree cleanup.
set -euo pipefail

if command -v morpheus >/dev/null 2>&1; then
  echo "Morpheus CLI ready: $(command -v morpheus)"
  exit 0
fi
if ((WORKER_CHECK_ONLY)); then
  echo "Morpheus CLI missing"
  exit 0
fi

checkout="$WORKER_CODE_ROOT/morpheus"
[[ -d "$checkout/.git" ]] || { echo "Morpheus checkout missing: $checkout" >&2; exit 1; }
(
  cd "$checkout"
  pnpm install --frozen-lockfile
  pnpm compile
  node dist/cli/index.js self install
)
command -v morpheus >/dev/null 2>&1 || { echo "Morpheus CLI install did not put morpheus on PATH" >&2; exit 1; }
echo "Morpheus CLI installed"
