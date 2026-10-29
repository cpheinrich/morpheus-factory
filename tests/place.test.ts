import { describe, expect, it } from "vitest";
import { place, within } from "../src/place.js";
import { claudeMax, mini, studio } from "./fixtures.js";

const hosts = [studio, mini];

describe("within", () => {
  it("accepts the root itself", () => {
    expect(within("/srv/work/app", "/srv/work/app")).toBe(true);
  });

  it("accepts a descendant", () => {
    expect(within("/srv/work/app/sub", "/srv/work/app")).toBe(true);
  });

  it("rejects a sibling that merely shares a prefix", () => {
    // The boundary that a naive startsWith gets wrong: "app" vs "application".
    expect(within("/srv/work/application", "/srv/work/app")).toBe(false);
  });

  it("rejects a parent", () => {
    expect(within("/srv/work", "/srv/work/app")).toBe(false);
  });
});

describe("place", () => {
  it("honours an operator pin", () => {
    const result = place({ task: "t", profile: "claude-max", host: "mini" }, claudeMax, hosts);
    expect(result).toEqual({ ok: true, placement: { host: mini, workspace: "/Users/zoe/code/app" } });
  });

  it("refuses a pin the profile is not allowed on", () => {
    const restricted = { ...claudeMax, hosts: ["studio"] };
    const result = place({ task: "t", profile: "claude-max", host: "mini" }, restricted, hosts);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("no-eligible-host");
    expect(result.refusal.detail).toContain("may not run on host");
  });

  it("refuses an unknown pinned host", () => {
    const result = place({ task: "t", profile: "claude-max", host: "ghost" }, claudeMax, hosts);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("no-eligible-host");
  });

  it("refuses an unhealthy pinned host instead of silently moving the run", () => {
    const sick = [{ ...studio, healthy: false }, mini];
    const result = place({ task: "t", profile: "claude-max", host: "studio" }, claudeMax, sick);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("host-unhealthy");
  });

  it("distinguishes 'no eligible host' from 'every eligible host is down'", () => {
    const restricted = { ...claudeMax, hosts: ["ghost"] };
    const ineligible = place({ task: "t", profile: "claude-max" }, restricted, hosts);
    expect(ineligible.ok).toBe(false);
    if (!ineligible.ok) expect(ineligible.refusal.code).toBe("no-eligible-host");

    const allDown = hosts.map((host) => ({ ...host, healthy: false }));
    const waiting = place({ task: "t", profile: "claude-max" }, claudeMax, allDown);
    expect(waiting.ok).toBe(false);
    if (!waiting.ok) expect(waiting.refusal.code).toBe("host-unhealthy");
  });

  it("prefers configuration order so placement is deterministic", () => {
    const reversed = [mini, studio];
    const result = place({ task: "t", profile: "claude-max" }, claudeMax, reversed);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.placement.host.id).toBe("mini");
  });

  it("finds a host that can reach a pinned workspace", () => {
    const result = place({ task: "t", profile: "claude-max", workspace: "/Users/zoe/code/app/feature" }, claudeMax, hosts);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.placement.host.id).toBe("mini");
    expect(result.placement.workspace).toBe("/Users/zoe/code/app/feature");
  });

  it("refuses a pinned workspace no host serves", () => {
    const result = place({ task: "t", profile: "claude-max", workspace: "/nowhere" }, claudeMax, hosts);
    expect(result.ok).toBe(false);
    if (result.ok) return;
    expect(result.refusal.code).toBe("workspace-not-found");
  });

  it("normalises a trailing slash before comparing", () => {
    const result = place({ task: "t", profile: "claude-max", host: "studio", workspace: "/srv/work/app/" }, claudeMax, hosts);
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.placement.workspace).toBe("/srv/work/app");
  });
});
