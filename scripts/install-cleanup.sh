#!/bin/bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bin_dir="$HOME/.local/bin"
agent_dir="$HOME/Library/LaunchAgents"
log_dir="$HOME/Library/Logs/morpheus-factory"
config_dir="$HOME/.config/morpheus-factory"
code_root="${MORPHEUS_WORKER_CODE_ROOT:-$HOME/code}"
python_bin="${MORPHEUS_CLEANUP_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  if [[ -x "$HOME/.local/bin/python3.11" ]]; then
    python_bin="$HOME/.local/bin/python3.11"
  else
    python_bin="/usr/bin/python3"
  fi
fi
label="com.morpheus-factory.development-cleanup"
plist="$agent_dir/$label.plist"

if [[ "$(uname -s)" != Darwin ]]; then
  echo "macOS required" >&2
  exit 1
fi
[[ "$code_root" == /* ]] || { echo "Worker code root must be absolute" >&2; exit 2; }
[[ "$python_bin" == /* && -x "$python_bin" ]] || { echo "Cleanup Python must be an executable absolute path" >&2; exit 2; }
mkdir -p "$bin_dir" "$agent_dir" "$log_dir" "$config_dir"
printf '%s\n' "$code_root" > "$config_dir/code-root"
install -m 755 "$repo_dir/scripts/cleanup-development-cache.py" "$bin_dir/morpheus-development-cleanup"
install -m 755 "$repo_dir/scripts/cleanup-old-worktrees.py" "$bin_dir/cleanup-old-worktrees.py"
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$python_bin</string>
    <string>$bin_dir/morpheus-development-cleanup</string>
    <string>--watch</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$bin_dir:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>WorkingDirectory</key><string>$HOME</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$log_dir/cleanup.log</string>
  <key>StandardErrorPath</key><string>$log_dir/cleanup.err.log</string>
</dict></plist>
EOF
plutil -lint "$plist"
launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Installed hourly storage watchdog: $plist (Python: $python_bin)"
