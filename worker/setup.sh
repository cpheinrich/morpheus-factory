#!/bin/bash
# First-stage, rerunnable macOS factory worker bootstrap.
set -euo pipefail

FACTORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKER_CODE_ROOT="$HOME/code"
WORKER_CHECK_ONLY=0
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
while (($#)); do
  case "$1" in
    --check) WORKER_CHECK_ONLY=1; shift ;;
    --code-root) [[ $# -ge 2 ]] || { echo "Missing value for --code-root" >&2; exit 2; }; WORKER_CODE_ROOT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$WORKER_CODE_ROOT" == /* ]] || { echo "--code-root must be an absolute path" >&2; exit 2; }
[[ -d "$FACTORY_ROOT/.git" ]] || { echo "Run from a morpheus-factory clone" >&2; exit 1; }
export FACTORY_ROOT WORKER_CODE_ROOT WORKER_CHECK_ONLY

for stage in prerequisites apps tools source morpheus spotlight factory cleanup; do
  echo "== $stage"
  bash "$FACTORY_ROOT/worker/setup/$stage.sh"
done

if ((WORKER_CHECK_ONLY)); then
  echo "Check complete; no changes made."
else
  echo "Local factory prerequisites are ready."
  echo "Fleet activation still needs a factory.json catalog, broker access, provider CLIs,"
  echo "and the Morpheus execution adapter. No provider credentials are copied by this script."
fi
