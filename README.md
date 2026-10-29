# morpheus-factory

Placement and subscription control plane for a Morpheus worker fleet.

Paperclip decides **what** runs. Factory decides **where** and **on whose
capacity** — which machine, which workspace, which subscription group, and
whether that group can take another turn right now. It emits one immutable run
spec, and the existing runner executes it.

This repository is the factory only. Paperclip integration, the pool runner,
quota reads and closeout export stay in `cpheinrich/morpheus`.

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

Deliberately **not** here yet: SSH execution, provider process management,
streaming, session resume, Paperclip API calls. Those exist and are qualified in
`cpheinrich/morpheus` (`scripts/pool-runner.mjs`, `pool-remote.mjs`). Rewriting
them here would duplicate the one part that is already proven.

## The two decisions in code

**A run spec never contains a secret.** `auth.keyRef` is `env:NAME` or
`secret:path`; the runner resolves it at spawn time. `parseConfig` rejects a
profile whose `keyRef` looks like a key rather than a reference.

**The broker is a port, not a dependency.** `src/brokers/port.ts` is two
methods: `snapshot()` and `auth()`. `CapacityBroker` reads one stable JSON
document; `NativeBroker` is the degraded path. Adding a gateway means adding one
file.

## Paperclip integration

Factory does **not** introduce a new Paperclip adapter type.

Paperclip's adapters answer "how is one turn executed". Factory answers "where,
on whose capacity". They are orthogonal, and collapsing them would either lose
per-provider execution or lose placement.

Instead: keep the `process` adapter — already qualified in Morpheus — and
register **one agent per profile**, each running the factory entry point. The
board then shows `claude-max`, `codex-pro`, `grok-fast` as distinct workers with
their real model and permission, instead of one opaque `morpheus-factory` actor.
One binary, many registrations, no forked adapter contract.

Full reasoning: [`docs/design.md`](docs/design.md).

## Commands

```sh
pnpm install
pnpm typecheck
pnpm test
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
