#!/bin/bash
# Scheduled entry point. Fetches only protected main, then reconciles its pin.
set -euo pipefail

host="" manifest="" credentials_project="" provider_command="" provider_url=""
while (($#)); do
  case "$1" in
    --host) host="$2"; shift 2 ;;
    --manifest) manifest="$2"; shift 2 ;;
    --credentials-project) credentials_project="$2"; shift 2 ;;
    --provider-command) provider_command="$2"; shift 2 ;;
    --provider-url) provider_url="$2"; shift 2 ;;
    *) echo "unknown argument" >&2; exit 2 ;;
  esac
done
[[ -n "$host" && -n "$manifest" && -n "$credentials_project" ]] || { echo "missing update runner input" >&2; exit 2; }
[[ -z "$provider_command" && -z "$provider_url" || -n "$provider_command" && -n "$provider_url" ]] || { echo "provider command and URL must be supplied together" >&2; exit 2; }
if [[ -n "$provider_command" ]]; then
  [[ "$provider_command" == /* && -x "$provider_command" && "$provider_url" == https://* ]] || { echo "invalid verification provider configuration" >&2; exit 78; }
  export FACTORY_VERIFICATION_CODE_COMMAND="$provider_command"
  export FACTORY_VERIFICATION_CODE_URL="$provider_url"
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "$manifest" == "$root"/* ]] || { echo "manifest must be in this Factory checkout" >&2; exit 2; }
[[ "$(git -C "$root" branch --show-current)" == main ]] || { echo "Factory checkout is not on main; update deferred" >&2; exit 75; }
[[ -z "$(git -C "$root" status --porcelain)" ]] || { echo "Factory checkout is dirty; update deferred" >&2; exit 75; }
GIT_TERMINAL_PROMPT=0 git -C "$root" -c credential.helper= fetch origin main
git -C "$root" merge --ff-only origin/main
[[ -f "$manifest" ]] || { echo "manifest is missing" >&2; exit 78; }
cd "$credentials_project"
exec morpheus credentials run -- python3 "$root/worker/update_controller.py" reconcile --host "$host" --manifest "$manifest"
