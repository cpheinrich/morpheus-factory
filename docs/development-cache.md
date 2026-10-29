# Developer storage and cleanup policy

Audit on the `zoe` Mac, 2026-10-29. Disk availability before cleanup was 64 GiB.
These are measured directory sizes, so shared APFS blocks and snapshots mean their
sum is not an exact promise of free space.

| Data | Measured | Pressure cleanup policy |
| --- | ---: | --- |
| CoreSimulator device data | 29 GiB | Reclaim only named, shutdown, expired and unowned QA/job devices. |
| Installed iOS simulator runtime | 16 GiB | Keep. Xcode needs it for active iOS work. |
| npm `_cacache` and old `_npx` installs | 5.6 / 2.0 GiB | Clear npm cache; remove npx installs idle 14 days. |
| pnpm store and metadata cache | 3.4 / 0.8 GiB | Prune unreferenced packages; retain metadata cache. |
| uv cache | 3.1 GiB | `uv cache prune`, which retains used entries. |
| Codebase Memory index | 3.0 GiB | Keep; it is an active code index. |
| Playwright browsers | 2.1 GiB | Keep; they are installed test dependencies. |
| Codex data | 5.2 GiB | About 2.4 GiB installed runtimes, 1.4 GiB managed worktrees, 0.4 GiB session history, and 0.4 GiB plugins. Keep these; only explicit app caches (about 54 MiB) are pressure-cleanup candidates while Codex is closed. |
| SwiftPM cache | 1.5 GiB | Keep unless it exceeds 5 GiB during storage pressure; then use SwiftPM's purge command while builds are idle. |
| Xcode DerivedData | 713 MiB | Remove directories idle 30 days, only with no `xcodebuild`. |
| Homebrew downloads | 274 MiB | `brew cleanup --prune=30`. |
| pip cache | 106 MiB | Purge when pip is present. |
| Browser and app caches | about 1.3 GiB for Brave alone | Keep; browser sessions are in use. |
| Colima VM and Docker images | 3.1 GiB / 0.6 GiB | Keep the running VM; only 66 MiB of images were reclaimable. |
| Tart golden iOS CI image | 52 GiB | Keep; `ci-xcode-26.6` is the configured image for `macos-vm` jobs. |
| Tart per-repo cache disks | 31 GiB | Keep active repo/slot disks; Evo uses 21 GiB, Lakina 4.3 GiB, Kairos 5.2 GiB. |
| MorpheusCD runtime/state | 6.3 GiB | Keep; includes durable CD data and checkouts, not classified as cache. |
| Claude app support | 12 GiB | Keep the 11.3 GiB VM bundle and session data. About 0.6 GiB of named browser, code and GPU caches can be cleared under pressure while Claude is closed. Claude CLI data in `~/.claude` is only 16 MiB. |
| Spotlight index | 69 GiB | Reset once with `mdutil -E` after a 64 GiB index file was found; indexing re-enabled. Do not delete its files in the watchdog. |
| Apple consumer apps and audio content | about 8.5 GiB | iMovie, GarageBand, Keynote, Pages and Numbers, plus orphaned GarageBand/Logic/Apple Loops content, removed; worker setup removes them on new hosts. |
| Downloads installers | 5+ GiB | Removed Xcode archives and installers for installed apps. Keep unrelated downloads. |
| `/private/tmp` build data | 7.7 GiB | Recent Evo DerivedData and SwiftPM packages; leave until the owning work finishes. |
| Morpheus/OpenClaw worktrees | about 15 GiB | Review through each owner's worktree cleanup command, not a generic age rule. |
| OpenClaw workspace and temp | 19 GiB | Includes working checkouts and recent temporary jobs; no automatic deletion without ownership metadata. |
| Large media files | under 1 GiB | One 0.41 GiB macOS aerial wallpaper video remained after GarageBand lesson content was removed. No large personal media collection found. |

The deeper audit found about 69 GiB hidden under the root-owned Spotlight store.
On this Mac, `sudo -n mdutil -E /System/Volumes/Data` reclaimed it, and
`sudo -n mdutil -i on /System/Volumes/Data` restored indexing. Free space rose
from 64 GiB before the first cleanup to about 177 GiB after the index reset,
installer removal and unused app removal. Rebuilding may consume some space.
Spotlight has no supported size limit. Erasing its store is a reset, not a
permanent prune: if the same content is indexed again, the store can regrow.
For a development host, exclude code checkouts, package stores, Tart images and
CI cache directories through System Settings → Spotlight → Search Privacy,
while retaining normal application and document search. A targeted source and
workflow search in Morpheus, Evo, Lakina, factory and host setup found no calls
to `mdfind` or `mdls`. The watchdog only checks the index after free space
falls below its reserve, and only resets it when the store exceeds 5 GiB and
earlier cleanup stages have not restored the target. It uses `mdutil -E` and
restores the volume's previous indexing state; it never removes index files
directly.

The script is dry-run by default; `--apply` performs a full manual cleanup
regardless of free space. An explicit manual run clears all Xcode DerivedData
when no build is running. For named shutdown QA and job simulators, it uses the
device directory's age; a CoreSimulator runtime update can touch every `data`
directory without making those devices newly used. The hourly watchdog keeps
the more conservative `data` directory age check and the 30-day DerivedData
cutoff. Both modes retain booted devices, active leases and supervisors.
The `--watch` mode runs hourly. It removes home Trash entries older than 30
days even when free space is healthy, and leaves caches alone while free space
is above the reserve. The Trash cutoff uses the later of creation time and
inode change time, so recently moved or modified entries are retained; Finder
metadata is left alone. Its floor is the greater of 50 GiB or 10% of the data volume; once
below that floor, it runs eligible cleanup stages in order until free space is
30 GiB above the floor. It starts with old installers, unowned simulators and build outputs;
SwiftPM is purged only when its global cache exceeds 5 GiB and no Swift or
Xcode build is running. Its repository cache uses `swift package purge-cache`;
the same stage clears its downloaded artifacts and compiled manifests, which
that command leaves behind. Package-store pruning, closed-agent-app cache removal,
guarded old-worktree removal and an oversized Spotlight
index reset are later stages. The job requires the
user's macOS login session, and writes logs under
`~/Library/Logs/morpheus-factory/`.
It skips a package manager's cache while that tool is active.
The Claude and Codex stage removes only contents of explicitly named cache
directories, and only while the corresponding app is closed. It leaves VM
bundles, installed runtimes, session history, browser profiles, plugin installs
and managed worktrees to their owners.
Installer cleanup only recognizes Xcode `.xip` files and known `.dmg` or app
installers in Downloads that are at least 30 days old. Non-Xcode installers
also require the app to be present in Applications.

Old Morpheus task worktrees are a late pressure-cleanup stage. It scans known
repository roots for linked task worktrees at least 14 days old, clears Git
registrations for directories already missing, and delegates live-directory
removal to `morpheus pm cleanup <ID>`. That command requires exact merged-PR and
trunk evidence, clean files, no active process, no lock, and no conflicting
session binding. A refusal preserves the worktree; the watchdog never uses
`--release-stale-sessions` or `git worktree remove --force`. Codex-managed
worktrees are left to Codex. Refused candidates are retried no more than once
per day while disk pressure continues. The current Mac has many old worktrees
with ignored build outputs or persistent session bindings, so age alone cannot
reclaim the full 15 GiB of legacy worktree directories.
The October 29 audit pruned stale Git registrations whose directories were
already missing and removed one clean, proven-merged `cpheinrich.com` task
worktree (PR #198). Other sampled candidates were retained for active task
status, ignored local data, or persistent agent bindings.

Worker setup places `.metadata_never_index` markers in existing source,
worktree, VM and cache roots. On this Mac the same development roots were also
added to Spotlight Search Privacy through System Settings. The markers are
macOS Spotlight's folder-level exclusion mechanism and need to be reapplied
if a temporary directory is recreated.

## Simulator ownership

Morpheus's `.github/actions/ios-simulator` names each job device `Morpheus CI
<UUID>`, saves that name in `GITHUB_STATE` before creation, and its always-run
post action deletes the device and XCTest clones from both simulator sets. Hard
runner termination can skip that post action. Evo's local test wrapper similarly
names disposable devices `Evo Test <UUID>` and deletes them in `finally`.
Lakina's local wrapper does the same with `Lakina Test <UUID>`. Lakina PR CI
uses GitHub-hosted `macos-26`; its nightly release defaults to the persistent
`lakina-ios-mac-mini` runner in Zoe's GUI session, so orphaned `Morpheus CI`
devices may be visible to this host's cleanup job.

The shared Morpheus QA preview has a different lifetime: `Evo QA <checkout-key>`
maps to `~/Library/Caches/med.evo.preview/<checkout-key>/session.json` and a
launchd supervisor. The cleanup script protects a device while its matching
lease is unexpired or that supervisor remains registered. It also protects
booted devices and any device referenced by a live process. For CI/CD jobs it
waits two days after the device directory's last change and skips all job
devices while `xcodebuild` is active. It excludes the persistent warmed CI
template and normal user devices.

Evo's PR, nightly, full verification and production workflows normally use the
`macos-vm` runner label. `agent-host-setup/ci-vm` starts a short-lived Tart VM
per job and deletes the VM afterward. Its sparse cache disks persist by
repository and slot to retain DerivedData and SwiftPM checkouts; the golden
image persists across jobs. Those host files are owned by the dispatcher, not
CoreSimulator, and this cleanup script does not remove them. An old cache disk
must be reset through the CI host's operating procedure after confirming its
slot is idle; an age check alone would race the dispatcher.

Future factory workers can register a device lease with
`morpheus-development-cleanup --lease-udid UUID --lease-minutes 720`, renew it
while their agent session uses the device, and release it with `--release-udid
UUID`. Leases live in `~/.local/state/morpheus-factory/simulator-leases`.
An unreadable lease is treated as ownership, so cleanup fails safe. A lease must
be renewed before it expires; a dormant chat with no lease or process cannot
be discovered from CoreSimulator alone.

## Installation

```sh
bash scripts/install-cleanup.sh
python3 scripts/cleanup-development-cache.py       # preview
python3 scripts/cleanup-development-cache.py --apply
python3 scripts/cleanup-development-cache.py --watch # pressure-based run
```

The installer copies the script to `~/.local/bin` and creates
`~/Library/LaunchAgents/com.morpheus-factory.development-cleanup.plist`. Rerun it
after updating the script. Installed runtimes, repository checkouts, derived
archives, caches containing credential state, and CD state are deliberately
outside its deletion policy.

macOS may deny a background launchd job access to `~/.Trash` even when the
same script works in Terminal. Grant Full Disk Access to the Python interpreter
named in the installer's output through System Settings → Privacy & Security →
Full Disk Access, then kickstart the job and check its exit status. The installer
uses `~/.local/bin/python3.11` when present, otherwise `/usr/bin/python3`;
`MORPHEUS_CLEANUP_PYTHON` selects another absolute interpreter path. On the
audited Mac, the existing `python3.11` permission was verified with a launchd
probe. Sudo alone did not provide access from launchd.
