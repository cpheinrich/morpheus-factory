# Worker bootstrap

Run `bash worker/setup.sh` from a factory clone on the Mac that will host workers.
The entry point runs the scripts in `worker/setup/` in order. Each stage is
rerunnable, and `--check` reports local readiness without changing the host.
`--code-root /absolute/path` chooses where the Morpheus checkout lives.

| Stage | Responsibility |
| --- | --- |
| `prerequisites.sh` | Check macOS, login user and Apple command-line tools; `--check` also reports whether `sudo -n` works, which Xcode staging and activation need for their exact commands (the operator adds that sudoers rule once; see `docs/host-updates.md`). |
| `apps.sh` | Remove iMovie, GarageBand, Keynote, Pages and Numbers after verifying their Apple bundle IDs; remove orphaned GarageBand audio content when no compatible app remains. |
| `tools.sh` | Install Git, Node, pnpm, gh and `xcodes` through Homebrew when missing (`xcodes` comes from the `xcodesorg/made` tap, not homebrew-core); require `python3` from the command-line tools. |
| `source.sh` | Clone Morpheus without resetting an existing checkout. |
| `morpheus.sh` | Install the copied Morpheus CLI when missing; the watchdog uses its guarded worktree cleanup. |
| `spotlight.sh` | Exclude existing source, worktree, VM and cache roots from Spotlight indexing. |
| `factory.sh` | Install locked dependencies and build the factory package. |
| `cleanup.sh` | Install the hourly storage watchdog launchd job. |

This prepares the host locally. Fleet activation will need separate stages for
the factory catalog, broker connectivity, provider CLIs and Morpheus's runner.
Those stages should keep credentials out of this repository and should not
start a worker unless their readiness checks pass.

## Persistent Actions runners

A host that serves CI/CD registers a GitHub Actions runner with `worker/runner.sh`.
The registration token is read from stdin only, never from a terminal or argv:

```sh
gh api -X POST repos/<owner>/<repo>/actions/runners/registration-token --jq .token |
  bash worker/runner.sh install --url https://github.com/<owner>/<repo> --name <name> --labels <a,b> --token-stdin
bash worker/runner.sh check [--name <name>]
gh api -X POST repos/<owner>/<repo>/actions/runners/remove-token --jq .token |
  bash worker/runner.sh remove --name <name> --token-stdin
```

`install` downloads the latest `osx-arm64` runner into `~/actions-runner-<name>` (SHA-256
checked against the release notes), registers it once with `--no-default-labels`, and loads
a Factory-owned LaunchAgent, `com.morpheus-factory.runner.<name>`. The runner carries exactly
the `--labels` given; list `self-hosted` explicitly if a workflow needs it, so that a job
asking only for `self-hosted` cannot land on a test host. The name `evo` is reserved for
agent-host-setup. Factory writes the plist instead of `svc.sh install` because the stock plist
sets `SessionCreate`, which locks the login keychain inside the runner's session so `codesign`
fails with `errSecInternalComponent`; `svc.sh` re-adds the key on every reinstall. `check`
needs no token and fails when any runner LaunchAgent sets `SessionCreate`, printing the fix.
`install` and `remove` refuse while the runner is running a job; `remove` leaves the directory.

## Xcode version reconciliation

The reviewed `worker/updates.json` pins the host's Xcode version and build.
`worker/update_controller.py` reports drift, stages a side-by-side Xcode, and
promotes it only after declared validation passes. `worker/install_updates.py`
installs an hourly LaunchAgent after the checkout is on clean `main`. See
[`docs/host-updates.md`](../docs/host-updates.md) for the phase and credential
contract. This does not replace the separate CI VM image rollout.
