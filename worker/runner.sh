#!/bin/bash
# Install, check or remove a persistent GitHub Actions runner whose LaunchAgent
# Factory owns. The stock svc.sh plist sets SessionCreate, which locks the login
# keychain inside the runner's session and makes codesign fail with
# errSecInternalComponent; worker/runner_plist.py never writes that key.
#
#   runner.sh install --url https://github.com/<owner>[/<repo>] --name <name> \
#     --labels <a,b> [--runnergroup <group>] [--work _work] --token-stdin
#   runner.sh check   [--name <name>]
#   runner.sh remove  --name <name> --token-stdin
set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
RUNNER_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
DOWNLOADS="$HOME/.cache/morpheus-factory/runner"
LABEL_PREFIX="com.morpheus-factory.runner."
uid="$(id -u)"

die() { echo "$1" >&2; exit "${2:-1}"; }
usage() { sed -n '7,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

(($#)) || usage
command="$1"; shift
url="" name="" labels="" group="" work="_work" token_stdin=0
while (($#)); do
  case "$1" in
    --url|--name|--labels|--runnergroup|--work)
      [[ $# -ge 2 ]] || die "Missing value for $1" 2
      case "$1" in
        --url) url="$2" ;; --name) name="$2" ;; --labels) labels="$2" ;;
        --runnergroup) group="$2" ;; --work) work="$2" ;;
      esac
      shift 2 ;;
    --token-stdin) token_stdin=1; shift ;;
    *) die "Unknown argument: $1" 2 ;;
  esac
done

[[ "$(uname -s)" == Darwin ]] || die "macOS required"
[[ "$uid" != 0 ]] || die "Run as the worker's login user, not root"

validate_name() {
  [[ -n "$name" ]] || die "--name is required" 2
  [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ && "$name" != *..* ]] || die "Invalid runner name: $name" 2
  [[ "$name" != evo ]] || die "Runner name 'evo' is reserved for agent-host-setup (~/actions-runner-evo)" 2
  runner_dir="$HOME/actions-runner-$name"
  label="$LABEL_PREFIX$name"
  plist="$AGENTS_DIR/$label.plist"
}

require_gui_session() {
  launchctl print "gui/$uid" >/dev/null 2>&1 || die "No GUI login session for uid $uid; log in at the console and rerun"
}

refuse_busy() {
  if pgrep -f "$runner_dir/bin/Runner.Worker" >/dev/null 2>&1; then
    die "Runner $name is running a job (Runner.Worker); retry when it is idle" 75
  fi
}

read_token() {
  ((token_stdin)) || die "Pass the registration token on stdin with --token-stdin" 2
  [[ ! -t 0 ]] || die "--token-stdin refuses a terminal; pipe the token in" 2
  IFS= read -r token || true
  [[ "$token" =~ ^[A-Za-z0-9_]{20,200}$ ]] || { unset token; die "Token on stdin is not a runner token" 2; }
}

# Prints the registered URL and name from .runner (which may start with a BOM).
registration() {
  python3 -I - "$1/.runner" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8-sig") as handle:
    data = json.load(handle)
print(data.get("gitHubUrl", ""))
print(data.get("agentName", ""))
PY
}

api_path() {
  local path="${url#https://github.com/}"
  path="${path%/}"
  if [[ "$path" == */* ]]; then echo "repos/$path"; else echo "orgs/$path"; fi
}

download_runner() {
  [[ "$(uname -m)" == arm64 ]] || die "Only Apple silicon (arm64) runners are supported; this host is $(uname -m)"
  command -v gh >/dev/null 2>&1 || die "gh is required to download the runner (worker/setup.sh installs it)"
  mkdir -p "$DOWNLOADS"
  local runner_tag tarball body checksum
  runner_tag="$(gh release view --repo actions/runner --json tagName --jq .tagName)"
  tarball="actions-runner-osx-arm64-${runner_tag#v}.tar.gz"
  gh release download "$runner_tag" --repo actions/runner --pattern "$tarball" -D "$DOWNLOADS" --clobber
  body="$(gh release view "$runner_tag" --repo actions/runner --json body --jq .body)"
  checksum="$(printf '%s\n' "$body" | sed -n 's/.*<!-- BEGIN SHA osx-arm64 -->\([a-f0-9]\{64\}\)<!-- END SHA osx-arm64 -->.*/\1/p' | head -1 || true)"
  [[ -n "$checksum" ]] || checksum="$(printf '%s\n' "$body" | grep -A1 "$tarball" | grep -oE '[a-f0-9]{64}' | head -1 || true)"
  [[ -n "$checksum" ]] || die "No published SHA-256 for $tarball in the $runner_tag release notes"
  echo "$checksum  $DOWNLOADS/$tarball" | shasum -a 256 --check
  mkdir -p "$runner_dir"
  tar -xzf "$DOWNLOADS/$tarball" -C "$runner_dir"
  echo "Runner $runner_tag unpacked into $runner_dir"
}

bootstrap_agent() {
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  launchctl enable "gui/$uid/$label"
  local attempt
  for attempt in 1 2 3 4 5; do
    if launchctl bootstrap "gui/$uid" "$plist" 2>/dev/null; then break; fi
    ((attempt < 5)) || die "launchctl bootstrap failed after five attempts: $plist"
    sleep 2
  done
  launchctl print "gui/$uid/$label" >/dev/null 2>&1 || die "LaunchAgent $label is not loaded after bootstrap"
  echo "LaunchAgent $label loaded"
}

install() {
  validate_name
  [[ "$url" =~ ^https://github\.com/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?/?$ ]] || die "--url must be https://github.com/<owner>[/<repo>]" 2
  [[ -n "$labels" && "$labels" =~ ^[A-Za-z0-9_.,-]+$ ]] || die "--labels must be a comma-separated list of [A-Za-z0-9_.-]" 2
  [[ -z "$group" || "$group" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Invalid --runnergroup: $group" 2
  [[ "$work" =~ ^[A-Za-z0-9_.-]+$ && "$work" != *..* ]] || die "--work must be a simple directory name" 2
  require_gui_session
  refuse_busy
  [[ ! -f "$runner_dir/.service" ]] || die "$runner_dir has a stock svc.sh service; run ./svc.sh uninstall there first"
  read_token

  [[ -x "$runner_dir/config.sh" ]] || download_runner
  if [[ -f "$runner_dir/.runner" ]]; then
    local registered=() reg_url reg_name
    while IFS= read -r line; do registered+=("$line"); done < <(registration "$runner_dir")
    reg_url="${registered[0]:-}" reg_name="${registered[1]:-}"
    if [[ "${reg_url%/}" != "${url%/}" || "$reg_name" != "$name" ]]; then
      unset token
      die "$runner_dir is registered as '$reg_name' at $reg_url, not '$name' at $url; remove it first"
    fi
    echo "Runner $name already registered at $reg_url"
  else
    local args=(--unattended --url "$url" --name "$name" --labels "$labels" --no-default-labels --work "$work")
    [[ -z "$group" ]] || args+=(--runnergroup "$group")
    # The token travels in the environment, never in argv.
    (cd "$runner_dir" && ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh "${args[@]}")
  fi
  unset token

  printf 'PATH=%s\nLANG=en_US.UTF-8\n' "$RUNNER_PATH" > "$runner_dir/.env"
  printf '%s\n' "$RUNNER_PATH" > "$runner_dir/.path"
  cp "$runner_dir/bin/runsvc.sh" "$runner_dir/runsvc.sh"
  chmod u+x "$runner_dir/runsvc.sh"

  mkdir -p "$AGENTS_DIR" "$LOG_DIR/$label"
  local staged="$plist.tmp"
  python3 -I "$SCRIPT_DIR/runner_plist.py" --name "$name" --runner-dir "$runner_dir" --log-dir "$LOG_DIR" > "$staged"
  plutil -lint "$staged" >/dev/null || { rm -f "$staged"; die "Generated plist failed plutil -lint"; }
  mv "$staged" "$plist"
  bootstrap_agent
  echo "Verify on GitHub: gh api $(api_path)/actions/runners --jq '.runners[] | select(.name == \"$name\") | {name, status, busy, labels: [.labels[].name]}'"
}

check() {
  local failed=0 found=0 dir runner reg_url plist_state
  if [[ -n "$name" ]]; then validate_name; fi
  for dir in "$HOME"/actions-runner-*; do
    [[ -d "$dir" ]] || continue
    runner="${dir#"$HOME"/actions-runner-}"
    [[ -z "$name" || "$runner" == "$name" ]] || continue
    found=1
    reg_url="not registered"
    if [[ -f "$dir/.runner" ]]; then reg_url="$(registration "$dir" | head -1)"; fi
    if [[ -f "$AGENTS_DIR/$LABEL_PREFIX$runner.plist" ]]; then
      if launchctl print "gui/$uid/$LABEL_PREFIX$runner" >/dev/null 2>&1; then plist_state="Factory plist loaded"; else plist_state="Factory plist not loaded"; fi
    else
      plist_state="no Factory plist"
    fi
    echo "runner $runner: $reg_url; $plist_state"
    if [[ -n "$name" && ! -f "$dir/.runner" ]]; then failed=1; fi
  done
  if [[ -n "$name" && $found == 0 ]]; then echo "runner $name: missing ($runner_dir)"; failed=1; fi

  local agent agent_label session
  for agent in "$AGENTS_DIR"/*.plist; do
    [[ -f "$agent" ]] || continue
    agent_label="$(plutil -extract Label raw -o - "$agent" 2>/dev/null || true)"
    [[ "$agent_label" == actions.runner.* || "$agent_label" == "$LABEL_PREFIX"* ]] || continue
    session="$(plutil -extract SessionCreate raw -o - "$agent" 2>/dev/null || true)"
    if [[ "$session" == true ]]; then
      echo "SessionCreate set: $agent — remove it: plutil -remove SessionCreate $agent; launchctl bootout gui/$uid/$agent_label; launchctl bootstrap gui/$uid $agent"
      failed=1
    else
      echo "SessionCreate absent: $agent"
    fi
  done
  return "$failed"
}

remove() {
  validate_name
  [[ -d "$runner_dir" ]] || die "No runner directory: $runner_dir"
  require_gui_session
  refuse_busy
  read_token
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  rm -f "$plist"
  echo "LaunchAgent $label removed"
  (cd "$runner_dir" && ACTIONS_RUNNER_INPUT_TOKEN="$token" ./config.sh remove)
  unset token
  echo "Runner $name deregistered; $runner_dir left in place"
}

case "$command" in
  install) install ;;
  check) check ;;
  remove) remove ;;
  *) usage ;;
esac
