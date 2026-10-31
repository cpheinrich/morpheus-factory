#!/usr/bin/env bash
#
# route.sh — route a Claude Code / Codex invocation to sub2api or native,
# based on the subscription's current usage.
#
# The rule: use the sub2api subscription first while it has quota headroom,
# fall back to the host's native login once it is near exhausted. This is the
# "use up the pooled subscription before your own" rule, in its simplest form.
#
# Usage:
#   route.sh claude [claude args...]
#   route.sh codex  [codex args...]
#
# Configuration (environment, with defaults):
#   SUB2API_ADMIN_URL        admin origin            (default http://localhost:8080)
#   SUB2API_ADMIN_EMAIL      admin login             (default admin@sub2api.local)
#   SUB2API_ADMIN_PASSWORD   admin password          (required)
#   SUB2API_GATEWAY_URL      gateway origin for CLIs (default = SUB2API_ADMIN_URL)
#   SUB2API_CLAUDE_KEY       sk- key, Anthropic group
#   SUB2API_CODEX_KEY        sk- key, OpenAI group
#   SUB2API_THRESHOLD        stop using sub2api at or above this % (default 80)
#
# Fails closed: if usage cannot be read, the invocation runs native rather than
# guessing that the pooled subscription has capacity.

set -euo pipefail

PROVIDER="${1:-}"
shift || true

if [[ "$PROVIDER" != "claude" && "$PROVIDER" != "codex" ]]; then
  echo "usage: $0 <claude|codex> [args...]" >&2
  exit 2
fi

ADMIN_URL="${SUB2API_ADMIN_URL:-http://localhost:8080}"
ADMIN_EMAIL="${SUB2API_ADMIN_EMAIL:-admin@sub2api.local}"
ADMIN_PASSWORD="${SUB2API_ADMIN_PASSWORD:-}"
GATEWAY_URL="${SUB2API_GATEWAY_URL:-$ADMIN_URL}"
THRESHOLD="${SUB2API_THRESHOLD:-80}"

# --- read the account's usage from the admin API ---------------------------
# Prints the used-percent of the most relevant window, or "unknown".
read_usage() {
  local token accounts_json
  token="$(curl -fsS -X POST "$ADMIN_URL/api/v1/auth/login" \
    -H 'content-type: application/json' \
    -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["access_token"])')" || return 1

  accounts_json="$(curl -fsS "$ADMIN_URL/api/v1/admin/accounts" \
    -H "Authorization: Bearer $token")" || return 1

  python3 -c '
import sys, json
provider = sys.argv[1]
data = json.load(sys.stdin)
result = "unknown"
for a in data.get("data", {}).get("items", []):
    if provider == "claude" and a.get("platform") == "anthropic":
        util = (a.get("extra") or {}).get("session_window_utilization")
        if util is not None:
            result = str(int(round(float(util) * 100)))
            break
    if provider == "codex" and a.get("platform") == "openai":
        extra = a.get("extra") or {}
        pct = extra.get("codex_7d_used_percent", extra.get("codex_5h_used_percent"))
        if pct is not None:
            result = str(int(pct))
            break
print(result)
' "$PROVIDER" <<< "$accounts_json"
}

if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "route.sh: SUB2API_ADMIN_PASSWORD is not set; running native" >&2
  exec "$PROVIDER" "$@"
fi

USED="$(read_usage)" || USED="unknown"

# --- decide ---------------------------------------------------------------
if [[ "$USED" == "unknown" ]]; then
  echo "route.sh: usage unknown; running native" >&2
  exec "$PROVIDER" "$@"
fi

if (( USED >= THRESHOLD )); then
  echo "route.sh: ${PROVIDER} at ${USED}% (>= ${THRESHOLD}%); running native" >&2
  exec "$PROVIDER" "$@"
fi

# --- route to sub2api -----------------------------------------------------
echo "route.sh: ${PROVIDER} at ${USED}% (< ${THRESHOLD}%); routing to sub2api" >&2

case "$PROVIDER" in
  claude)
    export ANTHROPIC_BASE_URL="$GATEWAY_URL"
    export ANTHROPIC_AUTH_TOKEN="${SUB2API_CLAUDE_KEY:?SUB2API_CLAUDE_KEY is required to route claude}"
    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
    exec claude "$@"
    ;;
  codex)
    export OPENAI_API_KEY="${SUB2API_CODEX_KEY:?SUB2API_CODEX_KEY is required to route codex}"
    exec codex \
      -c "model_providers.OpenAI.name=OpenAI" \
      -c "model_providers.OpenAI.base_url=${GATEWAY_URL}/v1" \
      -c "model_providers.OpenAI.wire_api=responses" \
      -c "model_providers.OpenAI.requires_openai_auth=true" \
      -c "model_providers.OpenAI.model_catalog_url=${GATEWAY_URL}/v1/models" \
      "$@"
    ;;
esac
