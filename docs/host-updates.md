# Host update controller

`worker/update_controller.py` is the first host-side reconciler. It handles an
exact Xcode version and build on one Mac. It does not infer "latest" or change
Evo's CI pins. The reviewed JSON manifest is the authority. `stage` downloads
and installs a side-by-side `/Applications/Xcode_<version>.app`; `promote` runs
the declared validation with `DEVELOPER_DIR` set to that app before switching
the command-line default. The prior Xcode stays installed for rollback.

The initial reviewed manifest is `worker/updates.json`; copy its host entry
when adding another Mac. The `phase` defaults to `stage` and
must be changed to `promote` in a reviewed commit after the canary and CI image
are ready. Validation commands are `argv` arrays, never shell text. The manifest
must come from a trusted branch because those commands execute on the host.

```sh
python3 worker/update_controller.py status --host zoe-mac-mini --manifest worker/updates.json
cd /Users/zoe/code/evo
morpheus credentials run -- python3 /path/to/factory/worker/update_controller.py auth-check
morpheus credentials run -- python3 /path/to/factory/worker/update_controller.py reconcile \
  --host zoe-mac-mini --manifest /path/to/factory/worker/updates.json
```

The credential wrapper exposes `ZOE_APPLE_ID` and `ZOE_APPLE_ID_PASSWORD` to
the controller. It passes only those two values, renamed to the `XCODES_*`
variables, to the `xcodes` downloader. Every other child receives a small
allowlisted environment. The Apple Account is not an App Store Connect API key.
Never put the password or verification code in the manifest, a command argument,
or logs.

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
the selected Xcode unchanged. A missing provider is reported as an authentication
requirement when Apple's cached session expires. `auth-login` remains available
for an interactive recovery, but must not run from launchd.

After merging a reviewed manifest, install the hourly reconciler from the
Factory main checkout:

```sh
python3 worker/install_updates.py --check --host zoe-mac-mini --credentials-project /Users/zoe/code/evo \
  --provider-command /absolute/path/to/code-provider --provider-url https://broker.tailnet.ts.net
python3 worker/install_updates.py --host zoe-mac-mini --credentials-project /Users/zoe/code/evo \
  --provider-command /absolute/path/to/code-provider --provider-url https://broker.tailnet.ts.net
```

It fetches and fast-forwards Factory main only when the checkout is clean and
on `main`, then invokes `reconcile` through the Evo credentials wrapper. No
secret appears in its plist. It logs to
`~/Library/Logs/morpheus-factory/host-updates.log` and repeats hourly. A pin
change on protected main is therefore picked up by the next successful tick.
The URL and executable path are not secrets and are passed by the installer;
`FACTORY_VERIFICATION_CODE_TOKEN` must come from the chosen host's credentials
project. Give each host a distinct token and credentials source. Reusing the
same shared companion on all hosts would expose every host's token to every
other host, defeating independent revocation. The router admin credential is
needed only on the SMS reader host, never on Factory hosts.

The controller checks for 70 GiB free, one update at a time, an existing app
with a conflicting build, the downloaded app's exact version/build, and macOS
security assessment. It refuses to switch while `xcodebuild` or
`swift-frontend` is active. If post-switch verification fails it restores the
previous selected developer directory. An Xcode update can require a new
simulator runtime; the validation command must cover the consumer's actual
build destination. The Tart image and consumer workflow pins still require a
separate coordinated rollout using `agent-host-setup/ci-vm/build-image.sh`.

The reviewed manifest now pins 27.2 build 27B52 in `stage` phase and declares
`MetalToolchain`, which Evo's iOS build needs. The reconciler asks Xcode to
download each declared component even when the app is already installed, so a
component interrupted by an outage is retried. The current 26.6 selection
remains unchanged while staging and validation are completed.
Build and test the corresponding CI image before changing the phase to `promote`.
The `reconcile` command is safe to repeat. A future fleet-level
controller must connect these per-host state transitions to the Tart image and
consumer workflow pin changes before declaring the fleet converged.
