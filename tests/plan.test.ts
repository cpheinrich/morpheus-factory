import { describe, expect, it } from "vitest";
import { plan } from "../src/plan.js";
import { envSuffix } from "../src/brokers/http.js";
import type { Auth, BrokerPort } from "../src/brokers/port.js";
import type { BrokerSnapshot, Profile } from "../src/types.js";
import { claudeMax, codexPro, makeConfig, makeSnapshot } from "./fixtures.js";

const now = () => new Date("2026-10-28T12:34:56.000Z");

class StubBroker implements BrokerPort {
  readonly id = "stub";
  #snapshot: BrokerSnapshot;
  constructor(snapshot: BrokerSnapshot) {
    this.#snapshot = snapshot;
  }
  async snapshot(): Promise<BrokerSnapshot> {
    return this.#snapshot;
  }
  auth(profile: Profile): Auth {
    return { mode: "broker", baseUrl: "https://broker.example.test", keyRef: profile.keyRef ?? `env:MORPHEUS_FACTORY_KEY_${envSuffix(profile.id)}` };
  }
}

const config = makeConfig();

describe("plan", () => {
  it("resolves a run into a broker-authenticated spec", async () => {
    const result = await plan({ task: "MO-1", profile: "claude-max" }, config, new StubBroker(makeSnapshot()), { now });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.spec).toMatchObject({
      task: "MO-1",
      profile: "claude-max",
      provider: "claude",
      host: "studio",
      transport: "ssh",
      target: "studio.local",
      workspace: "/srv/work/app",
      model: "claude-sonnet-5",
      permission: "acceptEdits",
      auth: { mode: "broker", baseUrl: "https://broker.example.test", keyRef: "env:MORPHEUS_FACTORY_KEY_CLAUDE_MAX" },
      generatedAt: "2026-10-28T12:34:56.000Z",
    });
    expect(result.spec.quota.windows).toEqual([{ id: "5h", usedPercent: 20, resetsAt: "2026-10-28T17:00:00.000Z" }]);
  });

  it("honours a configured key reference", async () => {
    const result = await plan({ task: "MO-2", profile: "codex-pro", host: "mini" }, config, new StubBroker(makeSnapshot()), { now });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.spec.auth).toEqual({ mode: "broker", baseUrl: "https://broker.example.test", keyRef: "secret:factory/codex-pro" });
  });

  it("carries no secret-looking material in the spec", async () => {
    const result = await plan({ task: "MO-3", profile: "codex-pro", host: "mini" }, config, new StubBroker(makeSnapshot()), { now });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    const serialized = JSON.stringify(result.spec);
    expect(serialized).not.toMatch(/sk-(ant|proj)?-|at-[A-Za-z0-9]{16,}|Bearer /);
    if (result.spec.auth.mode === "broker") expect(result.spec.auth.keyRef).toMatch(/^(env|secret):/);
  });

  it("refuses an unknown profile", async () => {
    const result = await plan({ task: "MO-4", profile: "nope" }, config, new StubBroker(makeSnapshot()), { now });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("unknown-profile");
  });

  it("falls back to native login when the broker is down and the host allows it", async () => {
    const degraded = makeConfig({ degradedMode: "native" });
    const down = makeSnapshot({ reachable: false, groups: {}, error: "timed out" });
    const result = await plan({ task: "MO-5", profile: "claude-max", host: "studio" }, degraded, new StubBroker(down), { now });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.spec.auth).toEqual({ mode: "native" });
    expect(result.spec.quota).toEqual({ source: "native", observedAt: down.observedAt, windows: [] });
  });

  it("still refuses when the broker is down and the host forbids native login", async () => {
    const degraded = makeConfig({ degradedMode: "native" });
    const down = makeSnapshot({ reachable: false, groups: {}, error: "timed out" });
    const result = await plan({ task: "MO-6", profile: "codex-pro", host: "mini" }, degraded, new StubBroker(down), { now });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("broker-unreachable");
    expect(result.refusal.detail).toContain('host "mini" does not permit native login');
    expect(result.refusal.detail).toContain("timed out");
  });

  it("refuses a broker outage when degraded mode is refuse", async () => {
    const down = makeSnapshot({ reachable: false, groups: {} });
    const result = await plan({ task: "MO-7", profile: "claude-max" }, config, new StubBroker(down), { now });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("broker-unreachable");
    expect(result.refusal.detail).toContain('degradedMode is "refuse"');
  });

  it("propagates an admission refusal unchanged", async () => {
    const tight = makeSnapshot({
      groups: { claude: { inFlight: 0, maxConcurrency: 5, healthyAccounts: 1, windows: [{ id: "5h", usedPercent: 95, resetsAt: "2026-10-28T17:00:00.000Z" }] } },
    });
    const result = await plan({ task: "MO-8", profile: "claude-max" }, config, new StubBroker(tight), { now });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("quota-exhausted");
  });

  it("is deterministic for the same request and catalog", async () => {
    const a = await plan({ task: "MO-9", profile: "claude-max" }, config, new StubBroker(makeSnapshot()), { now });
    const b = await plan({ task: "MO-9", profile: "claude-max" }, config, new StubBroker(makeSnapshot()), { now });
    expect(a).toEqual(b);
  });

  it("places before it consults the broker, so an outage cannot hide a config error", async () => {
    const down = makeSnapshot({ reachable: false });
    const result = await plan({ task: "MO-10", profile: "claude-max", host: "ghost" }, config, new StubBroker(down), { now });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("no-eligible-host");
  });
});
