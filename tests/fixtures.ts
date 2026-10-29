import type { FactoryConfig } from "../src/config.js";
import type { BrokerSnapshot, Host, Profile } from "../src/types.js";

export const studio: Host = {
  id: "studio",
  transport: "ssh",
  target: "studio.local",
  workspaces: ["/srv/work/app", "/srv/work/site"],
  nativeLogin: true,
  healthy: true,
};

export const mini: Host = {
  id: "mini",
  transport: "ssh",
  target: "mini.local",
  workspaces: ["/Users/zoe/code/app"],
  nativeLogin: false,
  healthy: true,
};

export const claudeMax: Profile = {
  id: "claude-max",
  provider: "claude",
  group: "claude",
  model: "claude-sonnet-5",
  permission: "acceptEdits",
};

export const codexPro: Profile = {
  id: "codex-pro",
  provider: "codex",
  group: "codex",
  model: "gpt-6.1-codex",
  effort: "high",
  permission: "workspace-write",
  keyRef: "secret:factory/codex-pro",
};

export function makeConfig(overrides: Partial<FactoryConfig> = {}): FactoryConfig {
  return {
    broker: { kind: "sub2api", baseUrl: "https://broker.example.test" },
    degradedMode: "refuse",
    policy: { stopAtPercent: 90, requireKnownQuota: true },
    hosts: [studio, mini],
    profiles: [claudeMax, codexPro],
    ...overrides,
  };
}

export function makeSnapshot(overrides: Partial<BrokerSnapshot> = {}): BrokerSnapshot {
  return {
    broker: "https://broker.example.test",
    reachable: true,
    observedAt: "2026-10-28T12:00:00.000Z",
    groups: {
      claude: { inFlight: 1, maxConcurrency: 5, healthyAccounts: 2, windows: [{ id: "5h", usedPercent: 20, resetsAt: "2026-10-28T17:00:00.000Z" }] },
      codex: { inFlight: 0, maxConcurrency: 4, healthyAccounts: 1, windows: [{ id: "weekly", usedPercent: 10, resetsAt: "2026-11-04T00:00:00.000Z" }] },
    },
    ...overrides,
  };
}
