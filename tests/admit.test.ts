import { describe, expect, it } from "vitest";
import { admit } from "../src/admit.js";
import type { BrokerSnapshot, GroupUsage } from "../src/types.js";
import { claudeMax, makeSnapshot } from "./fixtures.js";

const policy = { stopAtPercent: 90, requireKnownQuota: true };
const group = (overrides: Partial<GroupUsage> = {}): GroupUsage => ({
  inFlight: 0,
  maxConcurrency: 5,
  healthyAccounts: 1,
  windows: [{ id: "5h", usedPercent: 10, resetsAt: "2026-10-28T17:00:00.000Z" }],
  ...overrides,
});
const snapshotWith = (usage: GroupUsage): BrokerSnapshot => makeSnapshot({ groups: { claude: usage } });

describe("admit", () => {
  it("admits below the threshold", () => {
    expect(admit(claudeMax, snapshotWith(group()), policy)).toEqual({ ok: true });
  });

  it("refuses exactly at the threshold", () => {
    // The boundary that matters: 90 is already too far, not "still fine".
    const result = admit(claudeMax, snapshotWith(group({ windows: [{ id: "5h", usedPercent: 90, resetsAt: "x" }] })), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("quota-exhausted");
  });

  it("adjusts to a configured threshold", () => {
    const strict = { stopAtPercent: 50, requireKnownQuota: true };
    const usage = group({ windows: [{ id: "5h", usedPercent: 50, resetsAt: "x" }] });
    const result = admit(claudeMax, snapshotWith(usage), strict);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.refusal.detail).toContain("stop at 50%");
  });

  it("refuses the last free slot exactly at the cap", () => {
    const result = admit(claudeMax, snapshotWith(group({ inFlight: 5, maxConcurrency: 5 })), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("concurrency-exhausted");
  });

  it("admits at one below the cap", () => {
    expect(admit(claudeMax, snapshotWith(group({ inFlight: 4, maxConcurrency: 5 })), policy)).toEqual({ ok: true });
  });

  it("refuses a group with no eligible account", () => {
    const result = admit(claudeMax, snapshotWith(group({ healthyAccounts: 0 })), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("no-healthy-account");
  });

  it("refuses an unknown group when quota must be known", () => {
    const result = admit(claudeMax, makeSnapshot({ groups: {} }), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("quota-unknown");
  });

  it("admits an unknown group when quota is allowed to be unknown", () => {
    const relaxed = { stopAtPercent: 90, requireKnownQuota: false };
    expect(admit(claudeMax, makeSnapshot({ groups: {} }), relaxed)).toEqual({ ok: true });
  });

  it("refuses an empty window set rather than reading it as zero usage", () => {
    const result = admit(claudeMax, snapshotWith(group({ windows: [] })), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("quota-unknown");
  });

  it("refuses an unusable percentage instead of treating it as free", () => {
    const result = admit(claudeMax, snapshotWith(group({ windows: [{ id: "5h", usedPercent: Number.NaN, resetsAt: "x" }] })), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("quota-unknown");
  });

  it("reports an unreachable broker", () => {
    const result = admit(claudeMax, makeSnapshot({ reachable: false }), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("broker-unreachable");
  });

  it("checks account health before quota, because an empty pool is the real cause", () => {
    const usage = group({ healthyAccounts: 0, windows: [{ id: "5h", usedPercent: 99, resetsAt: "x" }] });
    const result = admit(claudeMax, snapshotWith(usage), policy);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("no-healthy-account");
  });
});
