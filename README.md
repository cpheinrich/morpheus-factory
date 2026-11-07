# morpheus-factory

Placement and subscription control plane for a Morpheus worker fleet.

The controller decides **what** runs. Factory decides **where** and **on whose
capacity** — which machine, which workspace, which subscription group, and
whether that group can take another turn right now. It emits one immutable run
spec, and the existing runner executes it.

This repository is the factory only. The runner, quota reads and closeout export
stay in `cpheinrich/morpheus`.

## Why this is separate

Two reasons, both structural:

1. **The broker is a replaceable dependency.** Factory owns a capacity contract,
   not a sub2api client. Swapping sub2api for another gateway — or for
   host-native logins — is an adapter change.
2. **The blast radius is different.** This repo will eventually describe
   credential custody for every subscription you own. Keeping it out of the
   project-scaffolding repo keeps that surface small.

## Scope of this version

Resolves a request into a run spec, or refuses it with a reason:

```sh
pnpm factory plan --task MO-26-10-28-00.00.00 --profile claude-max --config examples/factory.example.json
```

```json
{
  "ok": true,
  "spec": {
    "task": "MO-26-10-28-00.00.00",
    "profile": "claude-max",
    "provider": "claude",
    "host": "studio",
    "transport": "ssh",
    "target": "studio.local",
    "workspace": "/Users/chrisheinrich/code",
    "model": "claude-sonnet-5",
    "effort": "high",
    "permission": "acceptEdits",
    "auth": {
      "mode": "broker",
      "baseUrl": "https://factory-broker.internal",
      "keyRef": "env:MORPHEUS_FACTORY_KEY_CLAUDE_MAX"
    },
    "quota": { "source": "https://factory-broker.internal", "observedAt": "…", "windows": [] },
    "generatedAt": "…"
  }
}
```

SSH execution, provider process management, streaming and session resume stay in
Morpheus. Morpheus carries a source copy of this package under
`plugins/factory/src/host/`; changing this repository does not update that copy.

## The two decisions in code

**A run spec never contains a secret.** `auth.keyRef` is `env:NAME` or
`secret:path`; the runner resolves it at spawn time. `parseConfig` rejects a
profile whose `keyRef` looks like a key rather than a reference.

**The broker is a port, not a dependency.** `src/brokers/port.ts` is two
methods: `snapshot()` and `auth()`. `CapacityBroker` reads one stable JSON
document; `NativeBroker` is the degraded path. Adding a gateway means adding one
file.

## Controller and runner boundary

The controller supplies a task identifier and profile. Factory resolves host,
workspace and capacity into a run spec, or returns a refusal. The runner owns
provider execution, streaming and session resume; callers retain per-profile
model and permission information from the spec.

See [`docs/design.md`](docs/design.md) for the contract and placement rules.

## Consumption and review

This private package is built locally with `pnpm build`; it has no package
publication or service deployment workflow in this repository. Morpheus's host
package is a source import, not a live dependency on this checkout, and must be
updated and reviewed separately. Standalone worker setup builds this checkout;
the hourly host updater fast-forwards a clean `main` checkout before reconciling
the reviewed Xcode manifest. Keep changes behind independent review and the
checks below before merge. Do not run host reconciliation to validate a source
or documentation change.

## Commands

```sh
pnpm install
pnpm typecheck
pnpm test
pnpm build
python3 -m unittest discover -s tests -p 'test_*.py'
pnpm factory profiles --config examples/factory.example.json
pnpm factory plan --task t1 --profile codex-pro --host mini --config examples/factory.example.json
```

Exit codes follow the pool's convention: `0` resolved, `75` refused and
requeueable, `78` configuration or usage.

## Configuration

See [`examples/factory.example.json`](examples/factory.example.json). The real
`factory.json` is git-ignored: a filled-in catalog names live hosts and broker
groups, and should live with the operator, not in the repo.

| Field | Meaning |
|---|---|
| `broker.kind` | `sub2api` (capacity contract) or `native` (no broker) |
| `broker.baseUrl` | Adapter origin that publishes capacity; https except loopback |
| `broker.gatewayBaseUrl` | Origin workers point their CLI at; defaults to `baseUrl` |
| `degradedMode` | `native` — fall back per host; `refuse` — stop and requeue |
| `policy.stopAtPercent` | Refuse at or above this usage on any window |
| `policy.requireKnownQuota` | Refuse when capacity cannot be proven |
| `hosts[].nativeLogin` | Whether this host may run without the broker |
| `hosts[].workspaces` | Absolute roots this host may hand out |
| `profiles[].hosts` | Restrict a profile to specific hosts; absent means any |
| `profiles[].keyRef` | `env:NAME` or `secret:path`, never a key |

## Status

v0. Placement, admission, the broker port, configuration validation and the
degraded path are implemented and tested. Execution still lives in Morpheus.

## Mac worker preparation

Run `bash worker/setup.sh` from a clone on a new Mac. Its
[setup stages](worker/README.md) remove unused Apple apps, install local tool prerequisites, build factory,
check out Morpheus, exclude development roots from Spotlight, and install the
[hourly storage watchdog](docs/development-cache.md), including 30-day Trash retention,
guarded old-worktree cleanup, installed-app installer removal and a 5 GiB SwiftPM cache threshold.
The watchdog starts pressure cleanup below the greater of 50 GiB or 15% free space.
Use `--check` to inspect
the host without changing it. Hosts that serve CI/CD register a persistent
Actions runner with `worker/runner.sh`, whose Factory-owned LaunchAgent never
sets `SessionCreate` (the stock service plist does, and it locks the signing
keychain). This is the first stage of the single-command
worker setup: fleet activation still needs broker configuration, provider CLIs
and the Morpheus execution adapter.

## Host updates

[`docs/host-updates.md`](docs/host-updates.md) describes the per-host Xcode
reconciler. It pins exact version and build, stages the approved app beside the
current one, and validates before changing `xcode-select`. The
[`fleet controller`](fleet/README.md) carries a reviewed pin through the Tart
image, a deterministic draft PR on Evo, and host promotion; a fresh, capped
agent runs only when that PR cannot merge by itself. Once Evo's delivery workflow
is green after a promotion, the host retires its previous Xcode and CI image, so a
host keeps one Xcode in steady state. `update_controller.py check`
refuses before any download when sudo rights, credentials, disk, a runner
LaunchAgent, signing identities or the paired phone are wrong. The hourly
LaunchAgent fast-forwards a clean Factory checkout before reconciling the
reviewed manifest.
