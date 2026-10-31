#!/usr/bin/env bash
#
# Write Claude Code + Codex CLI config pointing at a sub2api gateway.
#
# Usage:
#   scripts/configure-client.sh <BASE> <CLAUDE_KEY> <CODEX_KEY>
#
#   BASE        gateway origin, e.g. http://zoe-mac-mini.tail7476d9.ts.net:8080
#   CLAUDE_KEY  the sk- key for the Anthropic group
#   CODEX_KEY   the sk- key for the OpenAI group
#
# Writes:
#   ~/.claude/settings.json   (Claude Code env)
#   ~/.codex/config.toml      (Codex CLI provider config)
#   ~/.codex/auth.json        (Codex CLI key)
#
# The keys are secrets: this script writes them to the client's own config
# files and never prints them. Do not commit those files.

set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <BASE> <CLAUDE_KEY> <CODEX_KEY>" >&2
  exit 2
fi

BASE="${1%/}"          # strip trailing slash
CLAUDE_KEY="$2"
CODEX_KEY="$3"

# Codex model: gpt-5.5 is retired (2026-10-14); gpt-6.1-sol is verified.
CODEX_MODEL="${CODEX_MODEL:-gpt-6.1-sol}"

mkdir -p ~/.claude ~/.codex

# --- Claude Code -----------------------------------------------------------
cat > ~/.claude/settings.json <<EOF
{
  "env": {
    "ANTHROPIC_BASE_URL": "${BASE}",
    "ANTHROPIC_AUTH_TOKEN": "${CLAUDE_KEY}",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
EOF

# --- Codex CLI -------------------------------------------------------------
cat > ~/.codex/config.toml <<EOF
model_provider = "OpenAI"
model = "${CODEX_MODEL}"
review_model = "${CODEX_MODEL}"
disable_response_storage = true
network_access = "enabled"

[model_providers.OpenAI]
name = "OpenAI"
base_url = "${BASE}/v1"
model_catalog_url = "${BASE}/v1/models"
wire_api = "responses"
requires_openai_auth = true

[features]
api_key_model_discovery = true
goals = true
EOF

cat > ~/.codex/auth.json <<EOF
{
  "OPENAI_API_KEY": "${CODEX_KEY}"
}
EOF

chmod 600 ~/.codex/auth.json

echo "wrote ~/.claude/settings.json, ~/.codex/config.toml, ~/.codex/auth.json"
echo "base URL: ${BASE}"
