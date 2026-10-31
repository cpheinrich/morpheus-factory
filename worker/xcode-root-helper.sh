#!/bin/bash
# The only command morpheus-factory runs as root. Installed root-owned:
#   sudo install -o root -g wheel -m 755 worker/xcode-root-helper.sh /usr/local/libexec/morpheus-factory-xcode
# with one sudoers line in /etc/sudoers.d/morpheus-factory:
#   <user> ALL=(root) NOPASSWD: /usr/local/libexec/morpheus-factory-xcode
#
# Bare mv/rm/xcode-select/xcodebuild rules are root-equivalent: sudoers globs
# match the whole argument string, and /Applications is admin-writable, so a
# planted bundle's xcodebuild would run as root. This helper accepts one verb
# and one Xcode bundle path, checks both, and runs only Apple-signed bundles.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

APPLICATIONS=/Applications
XCODE_SELECT=/usr/bin/xcode-select
SPCTL=/usr/sbin/spctl
RM=/bin/rm
STAT=/usr/bin/stat

usage() { echo "usage: morpheus-factory-xcode select|first-launch|remove /Applications/Xcode_<version>.app" >&2; exit 2; }
refuse() { echo "morpheus-factory-xcode: $1" >&2; exit 1; }

[[ $# -eq 2 ]] || usage
verb="$1" app="$2"
name="${app#"$APPLICATIONS"/}"
[[ "$app" == "$APPLICATIONS/$name" ]] || refuse "path must be directly inside $APPLICATIONS"
case "$verb" in
  select) [[ "$name" =~ ^Xcode(_[0-9]+(\.[0-9]+){1,2})?\.app$ ]] || refuse "not an Xcode bundle path: $app" ;;
  first-launch|remove) [[ "$name" =~ ^Xcode_[0-9]+(\.[0-9]+){1,2}\.app$ ]] || refuse "not a versioned Xcode bundle path: $app" ;;
  *) usage ;;
esac
[[ -d "$app" && ! -L "$app" ]] || refuse "not a directory (or a symlink): $app"

identity() { "$STAT" -L -f '%d:%i' "$1"; }

case "$verb" in
  select|first-launch)
    "$SPCTL" --assess --type execute "$app" >/dev/null 2>&1 || refuse "not an Apple-signed app: $app" ;;
esac

case "$verb" in
  select)
    "$XCODE_SELECT" --switch "$app/Contents/Developer" ;;
  first-launch)
    "$app/Contents/Developer/usr/bin/xcodebuild" -license accept
    "$app/Contents/Developer/usr/bin/xcodebuild" -runFirstLaunch ;;
  remove)
    target="$(identity "$app")" || refuse "cannot identify $app"
    selected="$("$XCODE_SELECT" -p 2>/dev/null)" || refuse "cannot read the selected Xcode"
    selected_app="${selected%/Contents/Developer}"
    if [[ -e "$selected_app" && "$(identity "$selected_app")" == "$target" ]]; then
      refuse "refusing to remove the selected Xcode: $app"
    fi
    if [[ -L "$APPLICATIONS/Xcode.app" && -e "$APPLICATIONS/Xcode.app" && "$(identity "$APPLICATIONS/Xcode.app")" == "$target" ]]; then
      refuse "refusing to remove the target of $APPLICATIONS/Xcode.app: $app"
    fi
    "$RM" -rf -- "$app" ;;
esac
