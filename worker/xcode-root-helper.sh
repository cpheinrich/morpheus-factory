#!/bin/bash
# The only command morpheus-factory runs as root. Installed root-owned:
#   sudo install -d -o root -g wheel -m 755 /usr/local/libexec
#   sudo install -o root -g wheel -m 755 worker/xcode-root-helper.sh /usr/local/libexec/morpheus-factory-xcode
# with one sudoers line in /etc/sudoers.d/morpheus-factory:
#   <user> ALL=(root) NOPASSWD: /usr/local/libexec/morpheus-factory-xcode
#
# Bare mv/rm/xcode-select/xcodebuild rules are root-equivalent: sudoers globs
# match the whole argument string, and /Applications is admin-writable, so a
# planted bundle's xcodebuild would run as root. This helper accepts one verb
# and its arguments, checks them, and only executes code from a bundle it has
# first moved out of the worker user's reach and verified there.
set -euo pipefail
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

APPLICATIONS=/Applications
QUARANTINE=/var/root/morpheus-factory-xcode
XCODE_SELECT=/usr/bin/xcode-select
SPCTL=/usr/sbin/spctl
CODESIGN=/usr/bin/codesign
RM=/bin/rm
MV=/bin/mv
CP=/bin/cp
MKDIR=/bin/mkdir
CHOWN=/usr/sbin/chown
CHMOD=/bin/chmod
STAT=/usr/bin/stat
ENV=/usr/bin/env
# Only Apple's own Xcode may run as root: a valid signature alone would admit any
# notarized Developer ID bundle named Xcode.app. This is Xcode's designated requirement.
REQUIREMENT='identifier "com.apple.dt.Xcode" and anchor apple'

usage() {
  echo "usage: morpheus-factory-xcode install <expanded Xcode.app> <version> | select <app> | remove /Applications/Xcode_<version>.app" >&2
  exit 2
}
refuse() { echo "morpheus-factory-xcode: $1" >&2; exit 1; }
VERSION_RE='^[0-9]+(\.[0-9]+){1,2}$'

[[ $# -ge 1 ]] || usage
verb="$1"

if [[ "$verb" == install ]]; then
  [[ $# -eq 3 ]] || usage
  src="$2" version="$3"
  [[ "$version" =~ $VERSION_RE ]] || refuse "not an Xcode version: $version"
  [[ "$src" == /* ]] || refuse "source must be an absolute path: $src"
  [[ "${src##*/}" == Xcode.app ]] || refuse "source must be an expanded Xcode.app: $src"
  [[ "$src" != "$APPLICATIONS"/* && "$src" != *"/../"* && "$src" != *"/./"* ]] || refuse "source must not be under $APPLICATIONS: $src"
  [[ -d "$src" && ! -L "$src" ]] || refuse "source is not a directory (or is a symlink): $src"
  parent="$(cd -P -- "${src%/*}" && pwd)" || refuse "cannot resolve the source directory: $src"
  [[ "$parent" != "$APPLICATIONS" && "$parent" != "$APPLICATIONS"/* ]] || refuse "source must not be under $APPLICATIONS: $src"
  quarantined="$QUARANTINE/Xcode_$version.app"
  target="$APPLICATIONS/Xcode_$version.app"
  [[ ! -e "$target" && ! -L "$target" ]] || refuse "$target already exists"
  [[ ! -e "$quarantined" && ! -L "$quarantined" ]] || refuse "$quarantined already exists; inspect it, then sudo rm -rf $quarantined"
  # Take the bundle out of the worker user's reach before verifying or running
  # anything in it: a check on a user-writable bundle can be raced. Clone it
  # (APFS clonefile) rather than move it, so a write handle a user process
  # still holds on the original cannot reach the copy's bytes. The source is
  # never deleted as root; the controller's temporary directory removes it.
  "$MKDIR" -p -m 700 "$QUARANTINE"
  "$CP" -Rc "$src" "$quarantined" || refuse "could not copy $src into $QUARANTINE"
  [[ -d "$quarantined" && ! -L "$quarantined" ]] || refuse "clone of $src is not a directory; inspect and sudo rm -rf $quarantined"
  trap 'echo "morpheus-factory-xcode: install failed; the bundle is left in $quarantined (remove with sudo rm -rf $quarantined)" >&2' ERR
  "$CHOWN" -R root:wheel "$quarantined"
  "$CHMOD" -R go-w "$quarantined"
  "$CHMOD" -R -N "$quarantined"
  "$CODESIGN" --verify --deep --strict -R="$REQUIREMENT" "$quarantined" >/dev/null 2>&1 || refuse "codesign rejected $quarantined as Apple's Xcode; it is left there (sudo rm -rf $quarantined)"
  "$SPCTL" --assess --type execute "$quarantined" >/dev/null 2>&1 || refuse "spctl rejected $quarantined; it is left there (sudo rm -rf $quarantined)"
  # Root runs the bundle's xcodebuild with a clean environment: nothing the
  # worker user exported (HOME included) reaches it.
  xcodebuild=("$ENV" -i HOME=/var/root PATH="$PATH" "$quarantined/Contents/Developer/usr/bin/xcodebuild")
  reported="$("${xcodebuild[@]}" -version)" || refuse "xcodebuild -version failed in $quarantined"
  [[ "${reported%%$'\n'*}" == "Xcode $version" ]] || refuse "$quarantined is not Xcode $version; it is left there (sudo rm -rf $quarantined)"
  "${xcodebuild[@]}" -license accept
  "${xcodebuild[@]}" -runFirstLaunch
  [[ ! -e "$target" && ! -L "$target" ]] || refuse "$target appeared during install; the bundle is left in $quarantined"
  # -h: never follow a symlink that appeared at the target. If a directory
  # appeared there instead, mv moved the bundle inside it; say so and stop.
  "$MV" -h "$quarantined" "$target"
  [[ -d "$target/Contents" && ! -e "$target/Xcode_$version.app" ]] || refuse "$target was replaced during install; the bundle is at $target/Xcode_$version.app"
  exit 0
fi

[[ $# -eq 2 ]] || usage
app="$2"
name="${app#"$APPLICATIONS"/}"
[[ "$app" == "$APPLICATIONS/$name" ]] || refuse "path must be directly inside $APPLICATIONS"
case "$verb" in
  select) [[ "$name" =~ ^Xcode(_[0-9]+(\.[0-9]+){1,2})?\.app$ ]] || refuse "not an Xcode bundle path: $app" ;;
  remove) [[ "$name" =~ ^Xcode_[0-9]+(\.[0-9]+){1,2}\.app$ ]] || refuse "not a versioned Xcode bundle path: $app" ;;
  *) usage ;;
esac
[[ -d "$app" && ! -L "$app" ]] || refuse "not a directory (or a symlink): $app"

identity() { "$STAT" -L -f '%d:%i' "$1"; }

case "$verb" in
  select)
    # Runs only Apple's xcode-select as root; the bundle's own code does not run
    # here, but a later root process using an xcrun shim would, so only Apple's
    # Xcode may be selected. Nothing on a worker host should run developer tools as root.
    "$CODESIGN" --verify --deep --strict -R="$REQUIREMENT" "$app" >/dev/null 2>&1 || refuse "not Apple's Xcode: $app"
    "$SPCTL" --assess --type execute "$app" >/dev/null 2>&1 || refuse "not an Apple-signed app: $app"
    "$XCODE_SELECT" --switch "$app/Contents/Developer" ;;
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
