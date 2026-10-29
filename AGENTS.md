# morpheus-factory — agent instructions

Read this before changing anything. `docs/design.md` holds the reasoning; this
file holds the rules.

## What this repo is

Placement and subscription control plane for a Morpheus worker fleet. Paperclip
decides what runs; factory decides where and on whose capacity, and emits one
immutable run spec.

It is **not** the runner. Execution, streaming, session resume, quota reads and
Paperclip API calls live in `cpheinrich/morpheus`. Do not reimplement them here.

## Layout

| Path | What |
|---|---|
| `src/types.ts` | Vocabulary: profile, host, request, spec, refusal |
| `src/config.ts` | Validation; issues returned as data, never thrown |
| `src/place.ts` | Pure host/workspace selection |
| `src/admit.ts` | Quota and concurrency gates |
| `src/plan.ts` | Placement → capacity → spec |
| `src/brokers/` | The swap seam: `port.ts`, `http.ts`, `native.ts` |
| `src/cli.ts` | `plan`, `profiles`, `hosts` |
| `tests/` | Vitest, mirroring `src/` |
| `docs/design.md` | Decisions, risks, open questions |

## Commands

```sh
pnpm install
pnpm typecheck
pnpm test
pnpm build
```

## Rules

- **No secrets, ever.** Configuration and run specs carry references
  (`env:NAME`, `secret:path`). If a change would put a key, token or raw
  credential into a file, a spec or a log line, it is wrong. `parseConfig`
  rejects a `keyRef` that looks like a key; keep that guard.
- **Own the contract, not the software.** Broker-specific schemas stop at
  `src/brokers/`. Adding a gateway means adding one adapter file, not editing
  placement or admission.
- **Fail closed.** An unreadable capacity document, a bad percentage or a missing
  group is `quota-unknown`, never "no usage".
- **Refusals are data.** Return `{ ok: false, refusal }`; do not throw for
  expected states. Authority failures follow the pool's exit codes: `75` refused
  and requeueable, `78` configuration.
- **Test the guard at its boundary.** `usedPercent === stopAtPercent` and
  `inFlight === maxConcurrency` both refuse. Keep those tests when editing.
- **Placement stays pure.** No I/O in `place.ts`; the same inputs must always
  produce the same host.
- **Do not add a Paperclip adapter type.** Factory registers under the existing
  `process` adapter, one agent per profile. See `docs/design.md`.

## Consuming from Morpheus

Morpheus depends on this package; this package depends on nothing from Morpheus.
That direction is deliberate — it keeps the credential surface out of the
scaffolding repo.
