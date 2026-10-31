# Host update controller

`worker/update_controller.py` reconciles one Mac's Xcode with an exact version
and build pinned in the reviewed manifest `worker/updates.json`. It never infers
"latest" and never edits a consumer's CI pins. `stage` downloads and installs a
side-by-side `/Applications/Xcode_<version>.app`; promotion runs the host's
declared validation with `DEVELOPER_DIR` set to that app, then switches the
command-line default through the root helper (below). Validation commands
are `argv` arrays, never shell text, and execute on the host, so the manifest
must come from protected main.

## Host kinds

Each host entry has a `role`:

- **`ci-image`** (default when a `ci` block is present) — the host builds and
  activates a Tart CI image for the pinned Xcode (see `image-stage` below). Zoe's
  Mac mini is this kind.
- **`phone-gate`** (default otherwise) — the host has a physical iPhone and runs
  a consumer's device gate with its *selected* Xcode. It has no image. Its
  validation is a signed device build: `validate_evo_main.py --device-gate <UDID>`
  runs Evo's `apps/ios/scripts/dev.sh build-device-gate <UDID>` in a detached
  worktree of freshly fetched `origin/main`. The MacBook Pro
  (`christophers-macbook-pro`) is Evo's delivery phone gate; the MacBook Air is a
  second phone-gate host. The shipped binary is built on 27.2 CI VMs, so both are
  pinned to Xcode 27.2 (27B52) and held at `phase: "stage"` until each host is
  prepared and `check` passes.

**Do not reconcile the Pro while an Evo delivery run is in progress.** Activation
defers while a runner job is live (below), but staging and validation still
compete with the gate for the machine and the phone.

## Manifest fields

All fields beyond `xcode` and `validation` are optional; `schemaVersion` stays 1.

| Field | Meaning | Default |
|---|---|---|
| `role` | `ci-image` or `phone-gate` | `ci-image` if `ci` is present, else `phone-gate` |
| `xcode.version`, `xcode.build` | exact pin | required |
| `xcode.components` | components such as `MetalToolchain`, re-requested every run | `[]` |
| `xcode.phase` | `stage` holds the host after installing; `promote` validates and switches | absent means `promote` |
| `xcode.retain` | Xcode bundles to keep in total, including the selected one (integer ≥ 1) | `2` |
| `paths.factory`, `paths.consumer`, `paths.hostSetup`, `paths.credentialsProject` | absolute directories on this host | Factory checkout, `~/code/evo`, `~/code/agent-host-setup` (ci-image only), the consumer |
| `apple.idVar`, `apple.passwordVar` | **names** of the environment variables holding the Apple Account login | `APPLE_ID`, `APPLE_ID_PASSWORD` |
| `verification` | `purpose` and `account` for the SMS code provider | none |
| `ci` | image builder, verifier, activator, repository, image and label (ci-image only) | none |
| `runner.label` | the GitHub Actions runner LaunchAgent label to check | none |
| `runner.dir` | absolute runner directory | none |
| `runner.requiredEnv` | variable names the runner's `.env` must define | `[]` |
| `runner.organization` | GitHub organization whose runner groups must be readable | none |
| `signing.identity`, `signing.team` | code-signing identity name and 10-character team id | none |
| `device.udid` | hardware UDID of the gate iPhone | none |

A hold is always explicit: a host stays staged only while its entry says
`"phase": "stage"`. Lifting it is a reviewed manifest change.

## `check`

```sh
python3 worker/update_controller.py check --host <host> --manifest worker/updates.json --json
```

prints `{"ok": bool, "checks": [{"name", "ok", "detail"}]}` and exits 1 when any
check fails. A check that cannot determine its answer reports `ok: false` with
"could not be determined", never a pass. Checks, in order:

1. `xcodes` — on `PATH`.
2. `disk` — at least 70 GiB free on `/Applications` unless the pinned app is
   already installed.
3. `sudo` — the root helper is installed at `/usr/local/libexec/morpheus-factory-xcode`,
   owned by root, not group- or other-writable, byte-identical (sha256) to
   `worker/xcode-root-helper.sh`, and `sudo -n -l` allows it. The detail prints
   the install commands (see [Root helper](#root-helper)). A changed checksum
   means the helper was updated in the repository and must be reinstalled.
4. `apple-credentials` — both configured variables are set and nonempty. Only the
   names are printed.
5. `verification-provider` (hosts with `verification`) —
   `FACTORY_VERIFICATION_CODE_COMMAND` is an executable absolute path and
   `FACTORY_VERIFICATION_CODE_URL` is `https`.
6. `paths` — each directory exists; `factory` and `consumer` are git checkouts.
7. `ci` (ci-image) — builder, verifier, activator and `tart` are executable.
8. `runner` (with `runner.label`) — `~/Library/LaunchAgents/<label>.plist` exists
   and has no true `SessionCreate`. With `SessionCreate` the runner gets a new
   security session in which the login keychain is locked, and `codesign` fails
   with `errSecInternalComponent`; the stock `svc.sh` re-adds it. Fix:
   `plutil -remove SessionCreate <plist>`, then
   `launchctl bootout gui/<uid>/<label>` and `launchctl bootstrap gui/<uid> <plist>`.
9. `runner-env` (with `runner.dir` and `runner.requiredEnv`) — `<dir>/.env`
   defines every named key. On the Pro the phone gate's `morpheus credentials run`
   needs `MORPHEUS_CREDENTIALS_REPO` there, and a stock runner installer rerun
   silently drops it.
10. `runner-groups` (with `runner.organization`) —
    `gh api orgs/<org>/actions/runner-groups` succeeds. A 403 means the
    device-runner installer's group-proof step cannot be refreshed from this host:
    it needs a token with `admin:org` or a browser session on the organization's
    runner-groups settings page.
11. `signing` (with `signing`) — exactly one valid code-signing identity named
    `<identity>: …` whose certificate's organizational unit is the team. (The
    parenthesised suffix of an Apple Development name is a personal id, not the
    team.) Two matching identities make `CODE_SIGN_IDENTITY="Apple Development"`
    ambiguous; the detail lists both SHA-1 fingerprints so one can be removed.
12. `device` (with `device.udid`) — `xcrun devicectl list devices` lists the UDID
    as `connected` or `available`; `unavailable`, `connecting`, absent or a command
    error fails.
13. `validation` — each validation executable exists and is executable.

## Preflight

`stage`, `image-stage` and `reconcile` run the install checks first (`xcodes`,
`disk`, `sudo`, `apple-credentials`, `verification-provider` when configured,
`paths`) and refuse with `preflight: <failed names>` before any download, so a
missing root helper or credential no longer surfaces after a multi-GB fetch. Run
`check` for the details.

## Root helper

Only one command runs as root: `worker/xcode-root-helper.sh`, installed once by
the operator as a root-owned copy and allowed by a single sudoers line:

```sh
sudo install -o root -g wheel -m 755 worker/xcode-root-helper.sh /usr/local/libexec/morpheus-factory-xcode
sudo visudo -f /etc/sudoers.d/morpheus-factory
#   <user> ALL=(root) NOPASSWD: /usr/local/libexec/morpheus-factory-xcode
```

Rerun the `install` line whenever the helper changes; `check` compares checksums
and reports a stale copy.

Why a helper rather than sudoers rules for the individual commands: every such
rule was root-equivalent. A bare `/bin/mv` allows any move as root. Sudoers
matches `*` against the whole concatenated argument string, so
`/bin/rm -rf -- /Applications/Xcode_*.app` also matches extra arguments, and
`xcode-select --switch *` accepts any directory. A rule naming
`/Applications/Xcode_<v>.app/.../xcodebuild` lets anyone who can write
`/Applications` (every admin) plant a bundle at that path and run it as root.

The helper takes exactly one verb and one app path (anything else exits 2) and
refuses with exit 1 and a one-line reason before acting:

| Verb | Accepts | Does |
|---|---|---|
| `select <app>` | `/Applications/Xcode.app` or `/Applications/Xcode_<version>.app` | `xcode-select --switch <app>/Contents/Developer` |
| `first-launch <app>` | `/Applications/Xcode_<version>.app` | `xcodebuild -license accept`, then `-runFirstLaunch` |
| `remove <app>` | `/Applications/Xcode_<version>.app` | `rm -rf -- <app>` |

The path must be a real directory, not a symlink. `select` and `first-launch`
require `spctl --assess --type execute` to pass, so only an Apple-signed bundle
runs as root. `remove` refuses the app `xcode-select -p` points into and the
target of an `/Applications/Xcode.app` symlink. The helper calls its tools by
absolute path and never relies on `PATH`.

Moving the expanded app into `/Applications` needs no root because the folder is
admin-writable; the login user must be an administrator, and `stage` refuses
with that message if the move fails.

## Credentials

The credential wrapper exposes the host's configured Apple variables (Zoe and the
Pro use `ZOE_APPLE_ID` / `ZOE_APPLE_ID_PASSWORD` for the shared developer account;
other hosts default to `APPLE_ID` / `APPLE_ID_PASSWORD`). The controller passes
only those two values, renamed to the `XCODES_*` variables, to the `xcodes`
downloader. Every other child receives a small allowlisted environment. The Apple
Account is not an App Store Connect API key. Never put the password or
verification code in the manifest, a command argument, or logs; the manifest
holds variable names only.

```sh
python3 worker/update_controller.py status --host zoe-mac-mini --manifest worker/updates.json
cd <paths.credentialsProject>
morpheus credentials run -- python3 /path/to/factory/worker/update_controller.py auth-check \
  --host zoe-mac-mini --manifest /path/to/factory/worker/updates.json
morpheus credentials run -- python3 /path/to/factory/worker/update_controller.py reconcile \
  --host zoe-mac-mini --manifest /path/to/factory/worker/updates.json
```

For unattended SMS challenges, a host may set `FACTORY_VERIFICATION_CODE_COMMAND`
to an absolute executable path. Factory launches that executable directly, with
no shell, and sends one JSON request on stdin:

```json
{"purpose":"apple-developer","account":"zoe-apple-developer","challengeStartedAt":0,"timeoutSeconds":120}
```

The provider returns `{"code":"123456"}` on stdout, exits `75` when no matching
code arrives in time, or exits nonzero for a configuration/service failure.
Factory never logs the provider's stdout or stderr. Its child environment contains
only ordinary process variables and, if set, `FACTORY_VERIFICATION_CODE_URL` and
`FACTORY_VERIFICATION_CODE_TOKEN`. It does not receive Apple credentials or the
rest of the credential companion. The public manifest contains the policy names
`purpose` and `account`, never the provider token. Providers must apply their own
caller, account, freshness and single-use checks.
If Apple lists more than one trusted phone, set
`FACTORY_VERIFICATION_PHONE_INDEX` to the reviewed 1-based index; the controller
refuses to guess. With one trusted phone, it selects that phone automatically.

`xcodes` has a one-shot SMS prompt. If a code does not arrive, Factory restarts
its login flow to request a new SMS after 120 seconds, then after another 120
seconds; it waits 180 seconds after the third send. The provider keeps the
original challenge start time on retries so a late earlier SMS can be considered.
The resulting timeout is explicit. An Apple rate limit, locked challenge or
rejected code stops the run rather than causing more sends. Staging errors leave
the selected Xcode unchanged. `auth-login` remains available for an interactive
recovery, but must not run from launchd.

## Installing the hourly runner

After merging a reviewed manifest, check and install from the Factory main
checkout. `--credentials-project` defaults to the host's
`paths.credentialsProject`:

```sh
python3 worker/install_updates.py --check --host zoe-mac-mini --github-user cpheinrich \
  --provider-command /absolute/path/to/code-provider --provider-url https://broker.tailnet.ts.net
python3 worker/install_updates.py --host zoe-mac-mini --github-user cpheinrich \
  --provider-command /absolute/path/to/code-provider --provider-url https://broker.tailnet.ts.net
```

`--check` runs the controller's `check` inside the credentials project, the way
the runner will, prints one line per check and exits nonzero when any fails.
Installing refuses on the same failure. The runner fetches and fast-forwards
Factory main only when the checkout is clean and on `main`, then invokes the
[fleet coordinator](../fleet/README.md) through a GitHub-only credential wrapper;
`update_runner.sh` also falls back to the manifest's `paths.credentialsProject`
when started without `--credentials-project`. No secret appears in its plist;
the GitHub account login is a nonsecret installer option. It logs to
`~/Library/Logs/morpheus-factory/host-updates.log` and repeats hourly, so a pin
change on protected main is picked up by the next successful tick.
The URL and executable path are not secrets and are passed by the installer;
`FACTORY_VERIFICATION_CODE_TOKEN` must come from the chosen host's credentials
project. Give each host a distinct token and credentials source. Reusing the
same shared companion on all hosts would expose every host's token to every
other host, defeating independent revocation. The router admin credential is
needed only on the SMS reader host, never on Factory hosts.

## Safety

The controller allows one update at a time, refuses an existing app with a
conflicting build, checks the downloaded app's exact version/build and macOS
security assessment, and validates before switching. **A busy host defers**:
while `xcodebuild`, `swift-frontend` or a GitHub Actions `Runner.Worker` is
running, `reconcile` returns `deferred-busy` *before* the validation build (so the
Pro does not run a device build beside a live delivery job), `activate` checks
again right before switching, and pruning does nothing. A job can sit between
build steps with no compiler running, and switching then would change the
toolchain mid-job. An idle `Runner.Listener` does not defer. If post-switch
verification fails the controller selects the previous app again through the
helper; a previous selection outside an Xcode app (such as the Command Line
Tools) cannot be restored and is reported. An Xcode update can require a new simulator runtime; the
validation command must cover the consumer's actual build destination.

**Pruning.** After a successful promotion, and on every `already-current`
reconcile on an idle host, the controller keeps `xcode.retain` Xcode bundles in
total (default 2). It always keeps the selected app, the app selected before this
reconcile (the rollback), and the target of an `/Applications/Xcode.app` symlink;
remaining slots go to the newest other `/Applications/Xcode_<version>.app`
bundles, compared numerically. It removes the rest with the helper's `remove`,
listing them under `removed` in the result. It only considers
`Xcode_<version>.app` bundles whose `xcodebuild -version` it can read, and never
removes `/Applications/Xcode.app`. Simulator runtimes are never deleted.

## CI images (ci-image hosts)

The `ci` block supplies a host-local image builder, verifier, pool activator,
repository, image and runner label. The fleet coordinator calls
`python3 worker/update_controller.py image-stage --host <host> --manifest worker/updates.json`
before opening consumer pin PRs. It stages the exact app, builds a missing Tart
image, boots a disposable clone to verify its exact Xcode version and build,
then activates that repository's version-specific image pool. A 0600 receipt in
`~/.local/state/morpheus-factory/updates/ci-images/` binds this proof to the
Tart image's disk/config/NVRAM file identities; ordinary reconciles use the
receipt and a cheap sealed-image check. Replacement or mutation forces a new
guest verification. Other repositories retain their current image and cache.
The consumer workflow still moves by reviewed PR, and the host pin moves to
`promote` only after the consumer's CI run passes.

Zoe's entry pins 27.2 build 27B52 in `promote` phase and declares
`MetalToolchain`, which Evo's iOS build needs. The reconciler asks Xcode to
download each declared component even when the app is already installed, so a
component interrupted by an outage is retried. Promotion validates a detached
worktree at freshly fetched Evo `origin/main`, leaving the primary checkout
untouched. Once the approved version is selected, hourly reconciles repair
components and prune but skip the full rebuild. The `reconcile` command is safe
to repeat.
