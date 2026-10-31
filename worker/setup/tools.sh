#!/bin/bash
set -euo pipefail
if ((WORKER_CHECK_ONLY)); then
  for tool in git brew node pnpm gh xcrun python3 xcodes; do
    if command -v "$tool" >/dev/null 2>&1; then echo "$tool ready"; else echo "$tool missing"; fi
  done
  exit 0
fi

command -v brew >/dev/null 2>&1 || {
  echo "Homebrew is required; install it and rerun worker/setup.sh" >&2
  exit 1
}
for tool in git node pnpm gh; do
  if ! command -v "$tool" >/dev/null 2>&1; then brew install "$tool"; fi
done
# xcodes is not in homebrew-core, and its tap builds from source, which fails on
# macOS 27 (XcodesOrg/homebrew-made#4). Install the publisher's notarized release
# binary instead, pinned by version and checksum like asccli in Morpheus.
if ! command -v xcodes >/dev/null 2>&1; then
  xcodes_version="2.1.0"
  xcodes_sha256="f1519afe934a513e85dd9b32fc872394becbbb6a41db15d9ac3926a09a891888"
  downloads="$HOME/.cache/morpheus-factory/xcodes"
  mkdir -p "$downloads" "$HOME/.local/bin"
  gh release download "$xcodes_version" --repo XcodesOrg/xcodes --pattern xcodes.zip -D "$downloads" --clobber
  echo "$xcodes_sha256  $downloads/xcodes.zip" | shasum -a 256 --check
  unzip -o -q "$downloads/xcodes.zip" -d "$downloads/unpacked"
  install -m 755 "$downloads/unpacked/xcodes" "$HOME/.local/bin/xcodes"
  "$HOME/.local/bin/xcodes" version >/dev/null
  echo "Installed xcodes $xcodes_version to $HOME/.local/bin/xcodes"
fi
# python3 comes with the Apple command-line tools; every controller uses it.
command -v python3 >/dev/null 2>&1 || { echo "python3 missing; reinstall the command-line tools (xcode-select --install)" >&2; exit 1; }
node_major="$(node -p 'process.versions.node.split(".")[0]')"
((node_major >= 22)) || { echo "Node 22 or newer required" >&2; exit 1; }
echo "Git, Node, pnpm, gh, python3 and xcodes ready"
