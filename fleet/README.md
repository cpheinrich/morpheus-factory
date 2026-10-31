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

## Hold

A host entry with `"hold": "<reason>"` makes the tick return `held: <reason>`
before any action: no staging, no image activation, no consumer PR, no switch.
It is the reviewed way to pause a rollout (the 27.2 rollout is held since
2026-10-30 while Evo returns to 26.6). `status` shows the hold.

## Order

1. Stage the approved app through the host's credentials companion.
2. `ci-image` hosts only: sync host tools and run `image-stage`.
3. If `phase` is `"stage"`, hold (`held-at-stage`) before touching the consumer:
   a held pin opens no consumer PR and spends no agent runs. `"promote"` or
   absent goes on.
4. Require **merged consumer main** to carry the pin: every `xcode-version:` and
   every `xcode-<X>` runner label in `.github/workflows/*.yml` equals the pin,
   at least one workflow sets `xcode-version:`, and every `Xcode <x>`,
   `Xcode\ <x>` and `Build version <b>` token in the assertion script equals
   the pin (at least one of each; 27.21 is not 27.2). No workflow names are listed;
   `consumer.ignorePaths` names lanes the consumer keeps on an older Xcode on
   purpose (Evo's `cd-v2-*.yml` until its deletion), which are neither judged
   nor rewritten. A host that already selects the pin reports `host-converged`
   without touching the consumer: the gate protects the switch, not the past.
5. Run the host reconciler (validate with `DEVELOPER_DIR`, switch `xcode-select`,
   roll back a failed switch) and report `host-converged` once it is selected.

## Consumer pin PR

When consumer main is not pinned, the controller opens a deterministic **draft**
PR on branch `factory/xcode-<version>-<build>`: in a temporary detached worktree
of `origin/main` it reads the old version/build from the assertion script and
rewrites them in the tracked files matching `consumer.pinPaths`
(`xcode-version`, `xcode-<v>` labels, `Xcode_<v>.app`, `Xcode <v>`, `Xcode\ <v>`,
`Build version <b>`, bare `<b>`). An unreadable assertion, a substitution that
changes nothing, or a rewritten file that still contains the old version or
build in any form is an error, not agent work. An existing branch or open PR is
reused, never rewritten. A closed, unmerged PR for the branch is an operator's
rejection while its branch exists: the result is `waiting-human: consumer pin
PR <url> was closed; delete branch <branch> to retry`, and no new PR is opened.
If the newest PR merged but main no longer carries the pin and its branch still
exists (a revert), the result is `waiting-human: … merged but main no longer
carries the pin; delete branch <branch> to retry`. Deleting the branch is the
retry in both cases: GitHub keeps listing the old PR, so the branch, not the PR
list, decides. An operator who means to reject a pin keeps the branch. Only `ci-image` hosts open or drive this PR; a `phone-gate`
host with an unpinned consumer just returns `waiting-consumer-merge`.

On a later tick, if that PR has still not merged, one **fresh** `codex exec`
run (no resume; its whole process group is killed after 2 h) is asked to finish it under the consumer's
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
  "pinPaths": [".github/workflows/*.yml", "apps/ios/scripts/ci/assert-xcode-build.sh"],
  "ignorePaths": [".github/workflows/cd-v2-*.yml"],
  "exceptions": [{"path": ".github/workflows/ios-delivery.yml", "version": "26.6", "reason": "…"}]
}
```

`exceptions` declares a version the consumer runs on purpose beside the pin in
one path (a production lane held on an older Xcode while a regression is
triaged). Such a value is not judged, and substitution never touches it because
only the previous pin's tokens are rewritten; it never stands in for the pin
itself, and the same version in any other path still fails. Each entry needs a
reason; remove it when the lane returns to the pin.

Optional and top-level; those are the defaults except `ignorePaths`, which
defaults to none and is set in the reviewed manifest while Evo's CD V2 lane
still exists. Paths must be relative with no
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
