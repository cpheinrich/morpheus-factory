#!/bin/bash
# Install the one command Factory runs as root, once per host. The operator
# running setup is asked for their password here; nothing else in setup needs
# root, and the hourly tick never prompts (it uses sudo -n for this path only).
set -euo pipefail
helper=/usr/local/libexec/morpheus-factory-xcode
source_script="$FACTORY_ROOT/worker/xcode-root-helper.sh"
sudoers=/etc/sudoers.d/morpheus-factory
user="$(id -un)"

status() {
  # "ready" only when the installed copy is root-owned, byte-identical to the
  # checkout and allowed without a password; anything else names what is off.
  [[ -f "$helper" ]] || { echo "missing"; return; }
  [[ "$(stat -f '%u' "$helper")" == 0 ]] || { echo "not root-owned"; return; }
  [[ "$(shasum -a 256 < "$helper")" == "$(shasum -a 256 < "$source_script")" ]] || { echo "stale (differs from worker/xcode-root-helper.sh)"; return; }
  sudo -n -l "$helper" >/dev/null 2>&1 || { echo "not allowed by sudoers"; return; }
  echo "ready"
}

state="$(status)"
if [[ "$state" == ready ]]; then
  echo "Root helper ready: $helper"
  exit 0
fi
if ((WORKER_CHECK_ONLY)); then
  echo "Root helper $state; setup will install it (asks for your password once)"
  exit 0
fi
if [[ ! -t 0 ]]; then
  echo "Root helper $state; rerun worker/setup.sh from a terminal to install it (sudo asks for a password)" >&2
  exit 1
fi
echo "Root helper $state; installing $helper (sudo will ask for your password)"
sudo install -d -o root -g wheel -m 755 "$(dirname "$helper")"
sudo install -o root -g wheel -m 755 "$source_script" "$helper"
printf '%s ALL=(root) NOPASSWD: %s\n' "$user" "$helper" | sudo tee "$sudoers" >/dev/null
sudo chmod 440 "$sudoers"
sudo visudo -c -f "$sudoers" >/dev/null
[[ "$(status)" == ready ]] || { echo "Root helper still $(status) after install" >&2; exit 1; }
echo "Root helper installed: $helper, allowed for $user by $sudoers"
