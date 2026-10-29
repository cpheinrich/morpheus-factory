import { describe, expect, it } from "vitest";
import { parseConfig } from "../src/config.js";

const base = {
  broker: { kind: "sub2api", baseUrl: "https://broker.example.test" },
  degradedMode: "refuse",
  hosts: [{ id: "studio", transport: "ssh", target: "studio.local", workspaces: ["/srv/work/app"] }],
  profiles: [{ id: "claude-max", provider: "claude", group: "claude", model: "claude-sonnet-5", permission: "acceptEdits" }],
};

describe("parseConfig", () => {
  it("accepts a minimal valid configuration and applies safe defaults", () => {
    const result = parseConfig(base);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.policy).toEqual({ stopAtPercent: 90, requireKnownQuota: true });
    expect(result.config.hosts[0]?.nativeLogin).toBe(false);
    expect(result.config.hosts[0]?.healthy).toBe(true);
  });

  it("keeps a host out of the native fallback unless it opts in", () => {
    const result = parseConfig({ ...base, hosts: [{ ...base.hosts[0], nativeLogin: "yes" }] });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.hosts[0]?.nativeLogin).toBe(false);
  });

  it("rejects a key value where a reference belongs", () => {
    // The guard that keeps a gateway key out of the catalog file.
    const result = parseConfig({
      ...base,
      profiles: [{ ...base.profiles[0], keyRef: "sk-ant-oat01-abcdef" }],
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.map((issue) => issue.path)).toContain("profiles[0].keyRef");
  });

  it("accepts an env reference", () => {
    const result = parseConfig({ ...base, profiles: [{ ...base.profiles[0], keyRef: "env:FACTORY_KEY_CLAUDE" }] });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.profiles[0]?.keyRef).toBe("env:FACTORY_KEY_CLAUDE");
  });

  it("reports every problem at once", () => {
    const result = parseConfig({
      ...base,
      degradedMode: "sometimes",
      policy: { stopAtPercent: 0 },
      profiles: [{ id: "p", provider: "nope", group: "", model: "", permission: "yolo" }],
    });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.length).toBeGreaterThanOrEqual(5);
  });

  it("refuses an unknown host reference in a profile", () => {
    const result = parseConfig({ ...base, profiles: [{ ...base.profiles[0], hosts: ["ghost"] }] });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues[0]?.path).toBe("profiles[0].hosts[0]");
  });

  it("refuses a duplicated profile id", () => {
    const result = parseConfig({ ...base, profiles: [base.profiles[0], base.profiles[0]] });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues.some((issue) => issue.message.includes("duplicates profile"))).toBe(true);
  });

  it("allows a threshold of exactly 100 and refuses 101", () => {
    expect(parseConfig({ ...base, policy: { stopAtPercent: 100 } }).ok).toBe(true);
    const tooHigh = parseConfig({ ...base, policy: { stopAtPercent: 101 } });
    expect(tooHigh.ok).toBe(false);
  });

  it("requires an origin for the capacity broker", () => {
    const result = parseConfig({ ...base, broker: { kind: "sub2api" } });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues[0]?.path).toBe("broker.baseUrl");
  });

  it("requires https except on loopback", () => {
    expect(parseConfig({ ...base, broker: { kind: "sub2api", baseUrl: "http://broker.example.test" } }).ok).toBe(false);
    expect(parseConfig({ ...base, broker: { kind: "sub2api", baseUrl: "http://localhost:8080" } }).ok).toBe(true);
  });

  it("strips trailing slashes so identity comparisons stay stable", () => {
    const result = parseConfig({ ...base, broker: { kind: "sub2api", baseUrl: "https://broker.example.test/" } });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.config.broker.baseUrl).toBe("https://broker.example.test");
  });

  it("requires an absolute workspace root", () => {
    const result = parseConfig({ ...base, hosts: [{ ...base.hosts[0], workspaces: ["srv/work/app"] }] });
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.issues[0]?.path).toBe("hosts[0].workspaces[0]");
  });
});
