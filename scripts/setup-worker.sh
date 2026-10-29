#!/bin/bash
# Compatibility entry point; the worker bootstrap is implemented under worker/.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$root/worker/setup.sh" "$@"
