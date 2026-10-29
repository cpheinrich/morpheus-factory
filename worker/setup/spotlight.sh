#!/bin/bash
# Keep high-churn worker data out of Spotlight's file index.
set -euo pipefail

roots=(
  "$WORKER_CODE_ROOT"
  "$HOME/.morpheus-worktrees"
  "$HOME/.codex"
  "$HOME/.openclaw"
  "$HOME/.hermes"
  "$HOME/.tart"
  "$HOME/ci-vm"
  "$HOME/.cache"
  "$HOME/.npm"
  "$HOME/Library/Caches"
  "$HOME/Library/Developer"
  "$HOME/Library/pnpm"
  "$HOME/cowork"
  "$TMPDIR"
)

for root in "${roots[@]}"; do
  [[ -d "$root" && ! -L "$root" ]] || continue
  if [[ -e "$root/.git" ]]; then
    echo "Repository root needs a parent exclusion: $root" >&2
    continue
  fi
  marker="$root/.metadata_never_index"
  if [[ -e "$marker" ]]; then
    echo "Spotlight exclusion present: $root"
  elif ((WORKER_CHECK_ONLY)); then
    echo "Spotlight exclusion missing: $root"
  else
    touch "$marker"
    echo "Excluded from Spotlight: $root"
  fi
done
