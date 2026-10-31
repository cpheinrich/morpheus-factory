# Design

Status: v0, 2026-10-28. This records the decisions behind the first version so a
later reader can tell judgement from accident.

## The problem

Three things are conflated in a single-wrapper-per-machine design:

| Axis | Examples | Should be portable? |
|---|---|---|
| Subscription | Claude Max, ChatGPT/Codex Pro, SuperGrok | Yes, independent of machine |
| Host | Mac Studio, Mac mini, MacBook Pro | No |
| Worker | provider CLI + model + effort + permission + workspace | Bound at run time |

Today's pool binds all three: a profile is `{provider, account, host,
workspace}` and the subscription's login lives on that host. That makes "one
subscription on several machines" and "several subscriptions on one machine"
awkward, and it makes a new machine a re-authentication chore.

Factory splits the decision in two:

```
Paperclip            what runs, and when
   │
factory              where, and on whose capacity      ← this repo
   │   run spec
runner               executes one turn
   │
broker               request → subscription account
```

Paperclip keeps scheduling. The broker keeps credentials and account selection.
Factory is the seam between them, and owns the only durable artifact: the run
spec.

## Why not a custom Paperclip adapter

Paperclip's adapter answers *how one turn is executed* — which CLI, how it is
spawned, how its stream is folded, how a session resumes. Factory answers *where
and on whose capacity*. Those are orthogonal.

A single `morpheus-factory` adapter would force one of two bad outcomes:

- reimplement every provider's CLI driver inside the adapter, duplicating the one
  part Paperclip already does well and that breaks whenever a CLI ships; or
- hide provider identity from the board, so every run looks the same and nobody
  can see which model or permission actually ran.

So the factory registers **one agent per profile** under the existing `process`
adapter, which is already qualified in Morpheus. One binary, many registrations,
no forked adapter contract, and the board keeps per-profile observability.

If Paperclip later needs factory to own the inbound answer channel — the current
co-location constraint on the process adapter — that becomes a second entry point
over the same core, not a rewrite of the selection logic.

## Placement

Placement is pure: request + catalog in, placement or refusal out. Configuration
order is the operator's preference order, so the same inputs always produce the
same host.

Rules:

1. A profile with `hosts` may run only on those hosts. Absent means any.
2. An operator pin is honoured, but never widens eligibility.
3. An unhealthy host is refused, not silently skipped, when it was pinned.
4. "No eligible host" and "every eligible host is down" are distinct refusals:
   the first is a configuration error, the second is a wait.
5. A pinned workspace must sit under one of the host's roots, compared on path
   segment boundaries — `/srv/work/application` is not inside `/srv/work/app`.

## Admission

Admission is separate from placement because the remedies differ: a refusal is a
requeue, a placement failure is an edit.

Gates, in order: broker reachable → group known → a healthy account exists →
quota windows readable → below `stopAtPercent` → a concurrency slot free.

Two boundaries are deliberate and tested:

- **exactly at `stopAtPercent` refuses.** The next turn would cross it, and a
  requeue is cheaper than a rate limit that strands every worker in the group.
- **`inFlight === maxConcurrency` refuses.** A free slot is one that can still
  accept a request.

An unreadable percentage, an empty window set, or a missing group is
`quota-unknown`, never "zero usage". When `requireKnownQuota` is false those
admit, which is only appropriate for `native` deployments where the host's own
quota command remains the execution-time gate. **Concurrency is never waived**:
the slot cap is a hard safety limit and is enforced even when quota is unknown,
so a relaxed quota policy cannot silently admit past it.

## The broker port

```ts
interface BrokerPort {
  readonly id: string;
  snapshot(): Promise<BrokerSnapshot>;   // fail closed, never throw
  auth(profile: Profile): Auth;          // a reference, never a secret
}
```

`CapacityBroker` reads one fleet-wide document:

```
GET {baseUrl}/factory/v1/capacity
{ "observedAt": "<iso8601>",
  "groups": { "<group>": { "inFlight": 0, "maxConcurrency": 5,
                           "healthyAccounts": 2,
                           "windows": [{ "id": "5h", "usedPercent": 12,
                                         "resetsAt": "<iso8601>" }] } } }
```

This is intentionally **not** a sub2api client. The gateway's admin payloads are
version-specific and shaped for its own UI; a broker upgrade must not be able to
break placement. A small adapter publishes this document from the gateway — or
from anything else that can answer the same question.

The capacity origin and the gateway origin are distinct fields
(`baseUrl` vs `gatewayBaseUrl`). A run spec's `auth.baseUrl` is always the
gateway origin, never the capacity endpoint, so a CLI can never be pointed at
the wrong host when the adapter and the gateway live apart.

`NativeBroker` returns no groups. It exists so a broker outage degrades the
fleet instead of stopping it.

## Credential rules

The earlier "no staged credentials" rule was written to prevent raw provider
OAuth being copied onto workers. It is restated here precisely:

1. **Raw provider OAuth never leaves the broker.** Refresh tokens live in one
   database, on one host, backed up as credential material.
2. **Workers receive references**, resolved at spawn time: `env:NAME` or
   `secret:path`. A run spec carries the reference, never the value.
3. **One broker owns a subscription.** A host that also holds a native login for
   a brokered account will rotate the same refresh chain and invalidate the
   broker. Native login is permitted only on hosts explicitly marked
   `nativeLogin`, and only for accounts the broker does not hold.
4. **Staging a gateway key is allowed; staging a subscription token is not.**
   The former is revocable and scoped; the latter is the account.

## Degraded mode

`degradedMode: "native"` permits a run to proceed on a host's own login when the
broker cannot be reached **and** that host opted in. `"refuse"` stops and
requeues.

The spec records which happened: `auth.mode` and `quota.source`. A run that
executed on native capacity is never reported as broker-verified.

Degraded mode does not cover `quota-unknown`. Unproven capacity is a refusal, not
an outage — otherwise the gate quietly disappears whenever a broker misbehaves.

## Risks this design does not remove

- **Emulation is an arms race.** A subscription gateway works by making a
  proxied request look first-party. That is observed behaviour, not a contract,
  and it can break without notice. Mitigation is a daily canary per provider and
  a fast, deliberate upgrade path — not pinning alone, which also pins the
  breakage.
- **The broker is a single point of failure.** Moving it to a cloud host
  relocates it. Degraded mode is the actual mitigation; a second region is not,
  because the refresh chain lives in one database.
- **Terms of service.** Routing subscription traffic through a gateway may
  violate provider terms. That is an operator decision, not a technical one.
- **The database is the credential.** Losing it means re-authenticating every
  subscription.

## Open questions for the spike

1. Do Claude Code, Codex and Grok CLIs complete a real coding session through a
   gateway — tools, streaming, websockets, session affinity?
2. Does the native runner dissolve the process adapter's controller/coordinator
   co-location constraint, and therefore permit deeper agent hierarchies?
3. Which per-account usage can be read reliably enough to feed the capacity
   document, at what latency?
4. Does a broker upgrade preserve the credential chain, and how is that tested
   before rollout?

## What would change this design

- The spike showing a CLI cannot complete a session through the gateway: fall
  back to native login per host and keep factory for placement only.
- Forking the gateway more than once a quarter: that is a signal to own the
  layer, not to keep patching it.
- A provider shipping a documented first-party delegation mechanism: the
  emulation risk disappears and the broker becomes a plain router.

## Host updates for the stateless Evo delivery pipeline — 2026-10-30

Mandate, recorded for a successor session. Chris delegated this outcome in the
Claude session "morpheus-factory worker setup" with CTO authority, no hard token
budget, and the MacBook Air as the test host (it may register as a runner and
change its Xcode). The brief: review [issue #9](https://github.com/cpheinrich/morpheus-factory/issues/9)
from the Evo CD session, form an independent view, implement the improvements,
test them on the Air, and merge. Authority covers this repository, the Air, and a
temporary runner registration; it does not cover the MacBook Pro tonight (an Evo
delivery run is using its selected Xcode), `device-runner/` in agent-host-setup
(another session is changing it), or any secret.

**Acceptance.** (1) `fleet/controller.py` judges Evo's pin without naming the
workflows Evo is deleting, and no hourly tick can start an unbounded agent loop.
(2) The two agent steps are deterministic pull requests; an agent runs only
when a mechanical PR cannot merge on its own, fresh and time-boxed, with a cap
per pin. (3) A host entry is portable: paths, Apple credential names and the
credentials project are per-host manifest fields, and `check` refuses before any
download when sudo rights, credentials, disk, the runner LaunchAgent, signing
identities or the paired phone are wrong. (4) `activate` defers while a runner
job is live. (5) Promotion keeps the selected Xcode plus one previous and removes
older `Xcode_<version>.app` bundles. (6) The MacBook Pro has a phone-gate host
entry whose validation is a signed device build. (7) Everything above is covered
by unit tests, and the Air run of `setup.sh`, `check` and the runner install is
recorded with its actual output.

**Decisions.**

- *The `phase` gate is now an optional hold, not a step an agent flips.* The
  controller already refuses to switch a host before the image is verified, the
  consumer's main carries the pin and the host validation passes; a mechanical
  phase flip after those gates added a PR and an agent run and no judgment.
  `phase: "stage"` still holds a host at staging when an operator wants that.
- *Consumer pin PRs are substitutions, not prompts.* The current pin is read
  from Evo's assertion script on main; every workflow and script under the
  declared paths has the old version and build replaced by the new ones, and
  the PR is opened as a draft under Evo's normal gates. A fresh `codex exec`
  carries it through tests, review and merge only when the draft is still open
  on a later tick, at most three times per pin; after that the tick reports and
  spends nothing.
- *Unused simulator runtimes are not deleted.* Runtimes are shared by every
  Xcode on the host and by CI's pinned destination OS; removing one can break a
  job that never asked for an Xcode change. Only `Xcode_<version>.app` bundles
  beyond the retained two are removed.
- *Generic persistent runners get a Factory-owned LaunchAgent.* The stock
  `svc.sh` plist sets `SessionCreate`, which locks the login keychain in the
  runner's session and breaks `codesign` with `errSecInternalComponent`
  (proven on the Pro on 2026-10-30). Factory writes its own plist without it.
  Evo's restricted device runner keeps its installer in agent-host-setup; Factory
  only checks its plist.
- *`xcodes` is installed from its pinned, checksummed release binary.* The
  Homebrew tap compiles it from source and fails on macOS 27 (observed on the
  Air; XcodesOrg/homebrew-made#4). One audited version and checksum in
  `worker/setup/tools.sh`, the same shape as asccli in Morpheus.

**Air run, 2026-10-30.** `worker/setup.sh` completed (xcodes 2.1.0 installed;
GarageBand and iMovie left in place because `sudo -n` is unavailable);
`update_controller.py check --host christophers-macbook-air` and
`install_updates.py --check` reported exactly the three real gaps (passwordless
sudo, Apple developer credentials, the phone disconnected) and passed xcodes,
disk, paths, the single TAML3DQJTR signing identity and the validation command;
`fleet/controller.py status` read `consumerPinned: true` from live Evo main;
`worker/runner.sh install` registered `factory-air-test` on darwin-health/evo
with only the `factory-air` label, GitHub listed it online, the plist had no
`SessionCreate`, and `remove` deregistered it. Evo's signed `EvoStagingDeviceGate`
build-for-testing passed on the Air with the connected iPhone 17 Pro Max
(7 min 51 s); a second run after the phone disconnected failed on the missing
destination, which is what the `device` check exists to catch early.
