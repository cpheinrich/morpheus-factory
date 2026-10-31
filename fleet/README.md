# Fleet Xcode updates

`controller.py` carries the exact `version`/`build` pin in `worker/updates.json`
across a host, its CI image and the consumer repository. The hourly runner
fast-forwards Factory main and calls it through `with_github.py`. The controller
holds its own lock; each tick re-reads repository and host state and does one
bounded step.

## Hosts

| Host | `role` | What reconcile does |
|---|---|---|
| Zoe's Mac mini | `ci-image` (has a `ci` block) | Stages Xcode, fast-forwards `agent-host-setup`, runs `image-stage` to build/verify the `xcode-27.2` Tart image and activate the runner pool that runs Evo's simulator tests and uploads, then promotes. |
| MacBook Pro | `phone-gate` (no `ci` block) | Physical-iPhone gate host. Builds with the host's selected Xcode: stages and promotes only, no image and no host-tool sync. |

`role` defaults to `ci-image` when the entry has `ci`, otherwise `phone-gate`;
a `ci-image` role without `ci` (or the reverse) is refused.

## Order

1. Stage the approved app through the host's credentials companion.
2. `ci-image` hosts only: sync host tools and run `image-stage`.
3. Require **merged consumer main** to carry the pin: every `xcode-version:` and
   every `xcode-<X>` runner label in `.github/workflows/*.yml` equals the pin,
   at least one workflow sets `xcode-version:`, and the assertion script names
   `Xcode <version>` and `Build version <build>`. No workflow names are listed.
4. If `phase` is `"stage"`, hold (`held-at-stage`). `"promote"` or absent goes on.
5. Run the host reconciler (validate with `DEVELOPER_DIR`, switch `xcode-select`,
   roll back a failed switch) and report `host-converged` once it is selected.

## Consumer pin PR

When consumer main is not pinned, the controller opens a deterministic **draft**
PR on branch `factory/xcode-<version>-<build>`: in a temporary detached worktree
of `origin/main` it reads the old version/build from the assertion script and
rewrites them in the tracked files matching `consumer.pinPaths`
(`xcode-version`, `xcode-<v>` labels, `Xcode_<v>.app`, `Xcode <v>`,
`Build version <b>`, bare `<b>`). An unreadable assertion or a substitution that
changes nothing is an error, not agent work. An existing branch or open PR is
reused, never rewritten. Only `ci-image` hosts open or drive this PR; a `phone-gate`
host with an unpinned consumer just returns `waiting-consumer-merge`.

On a later tick, if that PR has still not merged, one **fresh** `codex exec`
run (no resume, killed after 2 h) is asked to finish it under the consumer's
normal review and CI rules. At most **3 runs per pin**; after that the result is
`waiting-human: consumer pin PR <url> needs attention`. No agent pushes protected
main or opens a second PR.

Results: `waiting-host-stage`, `waiting-consumer-pr`, `waiting-consumer-merge`,
`waiting-human: …`, `held-at-stage`, `waiting-host-promotion`, `host-converged`.

## Manifest fields

```json
"consumer": {
  "repository": "darwin-health/evo",
  "assertion": "apps/ios/scripts/ci/assert-xcode-build.sh",
  "pinPaths": [".github/workflows/*.yml", "apps/ios/scripts/ci/assert-xcode-build.sh"]
}
```

Optional and top-level; those are the defaults. Paths must be relative with no
`..`. Each host may set `paths.consumer`, `paths.hostSetup` and
`paths.credentialsProject` (absolute); `--consumer-root` (alias `--evo-root`),
`--host-setup-root` and `--credentials-project` override them.

## Credentials and state

`with_github.py` passes only GitHub auth (via a temporary askpass helper) and
ordinary process variables to the controller; Apple credentials and SMS tokens
stay in the host credentials companion and never reach the PR or the agent.
Non-image host actions also drop the GitHub variables. `codex exec` uses its
existing local login.

```sh
python3 fleet/controller.py status --host zoe-mac-mini
```

prints the pin, phase, role, host status and `consumerPinned` (`null` when it
cannot be determined). Logs: `~/Library/Logs/morpheus-factory/host-updates.log`.
State: `~/.local/state/morpheus-factory/fleet/fleet.json`, mode 0600, shaped
`{"pin": "<version>-<build>", "agentRuns": [<ISO timestamps>]}`; it resets on a
new pin and holds no credentials.
