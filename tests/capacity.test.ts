import { afterEach, describe, expect, it } from "vitest";
import { CapacityBroker, envSuffix, parseCapacity } from "../src/brokers/http.js";

const capacity = {
  observedAt: "2026-10-28T12:00:00.000Z",
  groups: {
    claude: {
      inFlight: 2,
      maxConcurrency: 5,
      healthyAccounts: 3,
      windows: [{ id: "5h", usedPercent: 12.5, resetsAt: "2026-10-28T17:00:00.000Z" }],
    },
  },
};

describe("parseCapacity", () => {
  it("accepts the published shape", () => {
    const result = parseCapacity(capacity);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.groups.claude).toEqual({
      inFlight: 2,
      maxConcurrency: 5,
      healthyAccounts: 3,
      windows: [{ id: "5h", usedPercent: 12.5, resetsAt: "2026-10-28T17:00:00.000Z" }],
    });
  });

  it("rejects a document without a usable timestamp", () => {
    const result = parseCapacity({ ...capacity, observedAt: "yesterday" });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.error).toContain("observedAt");
  });

  it("rejects a group that cannot serve, rather than admitting on a bad read", () => {
    const broken = { ...capacity, groups: { claude: { ...capacity.groups.claude, maxConcurrency: 0 } } };
    const result = parseCapacity(broken);
    expect(result.ok).toBe(false);
  });

  it("rejects a window without an id", () => {
    const broken = { ...capacity, groups: { claude: { ...capacity.groups.claude, windows: [{ usedPercent: 1 }] } } };
    expect(parseCapacity(broken).ok).toBe(false);
  });

  it("rejects an array where an object belongs", () => {
    expect(parseCapacity([]).ok).toBe(false);
    expect(parseCapacity({ observedAt: "2026-10-28T12:00:00.000Z", groups: [] }).ok).toBe(false);
  });
});

describe("CapacityBroker", () => {
  const original = process.env.FACTORY_TEST_TOKEN;
  afterEach(() => {
    if (original === undefined) delete process.env.FACTORY_TEST_TOKEN;
    else process.env.FACTORY_TEST_TOKEN = original;
  });

  it("fails closed on a non-2xx response", async () => {
    const broker = new CapacityBroker({ baseUrl: "https://broker.test/", fetch: async () => new Response("nope", { status: 503 }) });
    const snapshot = await broker.snapshot();
    expect(snapshot.reachable).toBe(false);
    expect(snapshot.error).toBe("HTTP 503");
    expect(broker.baseUrl).toBe("https://broker.test");
  });

  it("fails closed on a malformed document", async () => {
    const broker = new CapacityBroker({ baseUrl: "https://broker.test", fetch: async () => new Response(JSON.stringify({ groups: {} }), { status: 200 }) });
    const snapshot = await broker.snapshot();
    expect(snapshot.reachable).toBe(false);
  });

  it("reports a timeout as unreachable rather than as zero usage", async () => {
    const abort = Object.assign(new Error("aborted"), { name: "AbortError" });
    const broker = new CapacityBroker({ baseUrl: "https://broker.test", fetch: async () => Promise.reject(abort) });
    const snapshot = await broker.snapshot();
    expect(snapshot.reachable).toBe(false);
    expect(snapshot.error).toBe("timed out");
  });

  it("sends the capacity token from the named environment variable", async () => {
    process.env.FACTORY_TEST_TOKEN = "capacity-secret";
    let seen: string | undefined;
    const broker = new CapacityBroker({
      baseUrl: "https://broker.test",
      tokenEnv: "FACTORY_TEST_TOKEN",
      fetch: async (_input, init) => {
        seen = new Headers(init?.headers).get("authorization") ?? undefined;
        return new Response(JSON.stringify(capacity), { status: 200 });
      },
    });
    const snapshot = await broker.snapshot();
    expect(snapshot.reachable).toBe(true);
    expect(seen).toBe("Bearer capacity-secret");
  });

  it("emits a key reference, never a key", () => {
    const broker = new CapacityBroker({ baseUrl: "https://broker.test" });
    const auth = broker.auth({ id: "claude-max", provider: "claude", group: "claude", model: "m", permission: "plan" });
    expect(auth).toEqual({ mode: "broker", baseUrl: "https://broker.test", keyRef: "env:MORPHEUS_FACTORY_KEY_CLAUDE_MAX" });
  });

  it("derives stable environment names", () => {
    expect(envSuffix("claude max")).toBe("CLAUDE_MAX");
    expect(envSuffix("__codex--pro__")).toBe("CODEX_PRO");
  });
});

describe("CapacityBroker — gateway origin and range", () => {
  it("rejects a percentage above 100 at parse time", () => {
    const broken = { ...capacity, groups: { claude: { ...capacity.groups.claude, windows: [{ id: "5h", usedPercent: 150, resetsAt: "x" }] } } };
    expect(parseCapacity(broken).ok).toBe(false);
  });

  it("points the CLI at the gateway origin, not the capacity origin", () => {
    const broker = new CapacityBroker({ baseUrl: "https://capacity.internal", gatewayBaseUrl: "https://gateway.internal" });
    const auth = broker.auth({ id: "claude-max", provider: "claude", group: "claude", model: "m", permission: "plan" });
    expect(auth).toEqual({ mode: "broker", baseUrl: "https://gateway.internal", keyRef: "env:MORPHEUS_FACTORY_KEY_CLAUDE_MAX" });
  });

  it("defaults the gateway origin to the capacity origin", () => {
    const broker = new CapacityBroker({ baseUrl: "https://broker.test" });
    const auth = broker.auth({ id: "claude-max", provider: "claude", group: "claude", model: "m", permission: "plan" });
    if (auth.mode !== "broker") throw new Error("expected broker auth");
    expect(auth.baseUrl).toBe("https://broker.test");
  });
});
