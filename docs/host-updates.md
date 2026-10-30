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
allowlisted environment. `xcodes` may still require a two-factor code; this
headless controller then reports `authentication-required` and retains the
current Xcode. The Apple Account is not an App Store Connect API key. Never put
the password or verification code in the manifest, a command argument, or logs.

After merging a reviewed manifest, install the hourly reconciler from the
Factory main checkout:

```sh
python3 worker/install_updates.py --check --host zoe-mac-mini --credentials-project /Users/zoe/code/evo
python3 worker/install_updates.py --host zoe-mac-mini --credentials-project /Users/zoe/code/evo
```

It fetches and fast-forwards Factory main only when the checkout is clean and
on `main`, then invokes `reconcile` through the Evo credentials wrapper. No
secret appears in its plist. It logs to
`~/Library/Logs/morpheus-factory/host-updates.log` and repeats hourly. A pin
change on protected main is therefore picked up by the next successful tick.

The controller checks for 70 GiB free, one update at a time, an existing app
with a conflicting build, the downloaded app's exact version/build, and macOS
security assessment. It refuses to switch while `xcodebuild` or
`swift-frontend` is active. If post-switch verification fails it restores the
previous selected developer directory. An Xcode update can require a new
simulator runtime; the validation command must cover the consumer's actual
build destination. The Tart image and consumer workflow pins still require a
separate coordinated rollout using `agent-host-setup/ci-vm/build-image.sh`.

The initial manifest pins this host's already selected 26.6 and has only the
`stage` phase. The LaunchAgent can run without changing Xcode. Add a new pin in
`stage`, build and test the corresponding CI image, then change the pin's phase
to `promote`. The `reconcile` command is safe to repeat. A future fleet-level
controller must connect these per-host state transitions to the Tart image and
consumer workflow pin changes before declaring the fleet converged.
