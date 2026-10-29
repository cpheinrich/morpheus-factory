#!/bin/bash
set -euo pipefail
if ((WORKER_CHECK_ONLY)); then
  plist="$HOME/Library/LaunchAgents/com.morpheus-factory.development-cleanup.plist"
  if [[ -f "$plist" ]]; then echo "Storage watchdog installed"; else echo "Storage watchdog missing"; fi
  exit 0
fi
MORPHEUS_WORKER_CODE_ROOT="$WORKER_CODE_ROOT" bash "$FACTORY_ROOT/scripts/install-cleanup.sh"
