#!/bin/bash
# Remove Apple consumer apps that a dedicated factory worker does not use.
set -euo pipefail

apps=(
  'iMovie:com.apple.iMovieApp'
  'GarageBand:com.apple.garageband10'
  'Keynote:com.apple.iWork.Keynote'
  'Pages:com.apple.iWork.Pages'
  'Numbers:com.apple.iWork.Numbers'
)

# Removal needs passwordless sudo. Without it the host still works; report and move on.
can_sudo=1
if ! ((WORKER_CHECK_ONLY)) && ! sudo -n true 2>/dev/null; then
  can_sudo=0
  echo "sudo -n is unavailable; unused Apple apps are left in place (see docs/host-updates.md for the sudoers line)"
fi

for entry in "${apps[@]}"; do
  name="${entry%%:*}"
  expected_id="${entry#*:}"
  path="/Applications/$name.app"
  if [[ ! -e "$path" ]]; then
    echo "$name already absent"
    continue
  fi
  actual_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$path/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "$actual_id" != "$expected_id" ]]; then
    echo "Unexpected bundle at $path ($actual_id); refusing removal" >&2
    exit 1
  fi
  if ((WORKER_CHECK_ONLY)); then
    echo "$name installed; setup will remove it"
  elif ((!can_sudo)); then
    echo "$name left in place"
  else
    sudo -n /bin/rm -rf -- "$path"
    [[ ! -e "$path" ]] || { echo "Failed to remove $path" >&2; exit 1; }
    echo "Removed $name"
  fi
done

# GarageBand leaves shared lesson and instrument downloads behind. Keep them if
# an Apple audio workstation that can use them is still installed.
if [[ ! -d '/Applications/GarageBand.app' && ! -d '/Applications/Logic Pro.app' && ! -d '/Applications/MainStage.app' ]]; then
  for path in '/Library/Application Support/GarageBand' '/Library/Application Support/Logic' '/Library/Audio/Apple Loops'; do
    [[ -e "$path" ]] || continue
    if ((WORKER_CHECK_ONLY)) || ((!can_sudo)); then
      echo "Unused audio content present: $path"
    else
      sudo -n /bin/rm -rf -- "$path"
      [[ ! -e "$path" ]] || { echo "Failed to remove $path" >&2; exit 1; }
      echo "Removed unused audio content: $path"
    fi
  done
fi
