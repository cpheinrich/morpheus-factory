# Fleet Xcode updates

`controller.py` is the single entry point for the reviewed `worker/updates.json`
pin. The existing hourly runner fast-forwards Factory main, then calls it through
`with_github.py`. One LaunchAgent and one protected-main fetch serve the full
sequence; the fleet controller has its own lock and a private 0600 session record.

For each exact `version`/`build` pin, reconciliation proceeds in order:

1. Stage the approved app through the Evo credentials companion. A new pin is
   not selected at this step, even if its manifest phase says `promote`.
2. Fast-forward a clean `agent-host-setup` main checkout, then ask `image-stage`
   to build or verify the exact Tart guest image and activate
   Evo's version-specific runner pool. It checks the guest build, not its name.
3. Inspect **merged Evo main** for the four iOS workflow pins and exact-build
   assertion. If it differs, run a bounded Codex task in an isolated Factory
   worktree to finish Evo's tests, independent review, CI, PR and merge. Resume
   its session on the next tick if the merged code is still behind.
4. When Evo main is pinned, run a second task to merge Factory's `stage` →
   `promote` manifest PR, if needed. Only then call the host reconciler to
   validate with `DEVELOPER_DIR`, switch `xcode-select`, and roll back a failed
   switch. An accidentally early `promote` phase cannot bypass the image/CI gate.
5. Report convergence only after the host selects the approved build. The
   image and pool checks repeat on subsequent ticks, using the host-side
   verified receipt for a cheap unchanged-image check.

No task directly pushes protected main. A successful agent exit alone is not
evidence of rollout; the controller checks merged files and host state on every
tick. If a task or CI is delayed, the next tick resumes its persisted session.
The controller deliberately leaves the MacBook Pro-only CD V2 lane on its current
pin until that host can build and verify an image. A fleet report must call that
host out as pending rather than claiming every Evo lane has converged.

The GitHub wrapper retrieves the selected account's existing `gh` credential
without printing it, builds a temporary Git askpass helper, and passes only
GitHub auth plus ordinary process variables to the fleet task. The image
builder receives GitHub auth to fetch runner assets; worker child environments
exclude Apple login and SMS provider values. Apple credentials and SMS tokens
are never inherited by the PR agent. `codex exec`
uses its existing local login and automatic approval review under the workspace
sandbox; it does not need an OpenAI API key in this LaunchAgent.

After the host runner integration is merged, check status with:

```sh
python3 fleet/controller.py status --host zoe-mac-mini
```

The hourly job's result and errors are in
`~/Library/Logs/morpheus-factory/host-updates.log`. The fleet session record is
`~/.local/state/morpheus-factory/fleet/fleet.json`; it contains a Codex session
ID and pin only, not credentials or verification codes. On a new pin the old
session is discarded. A controller error leaves the selected Xcode and old
runner image available for rollback.
