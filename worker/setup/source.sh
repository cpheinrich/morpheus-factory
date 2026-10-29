#!/bin/bash
set -euo pipefail
checkout="$WORKER_CODE_ROOT/morpheus"
if ((WORKER_CHECK_ONLY)); then
  if [[ -d "$checkout/.git" ]]; then echo "Morpheus checkout present: $checkout"; else echo "Morpheus checkout missing: $checkout"; fi
  exit 0
fi

mkdir -p "$WORKER_CODE_ROOT"
if [[ ! -e "$checkout" ]]; then
  git clone https://github.com/cpheinrich/morpheus.git "$checkout"
elif [[ ! -d "$checkout/.git" ]]; then
  echo "$checkout exists but is not a Git checkout" >&2
  exit 1
fi
echo "Morpheus checkout ready: $checkout"
