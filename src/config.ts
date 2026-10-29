import type { Host, Permission, Profile, ProviderId } from "./types.js";

export interface BrokerConfig {
  kind: "sub2api" | "native";
  /** Required for sub2api; the origin every worker points its CLI at. */
  baseUrl?: string;
  timeoutMs?: number;
}

export interface Policy {
  /** Refuse at or above this percentage on any window. */
  stopAtPercent: number;
  /** Refuse when the broker cannot prove the group has capacity. */
  requireKnownQuota: boolean;
}

export interface FactoryConfig {
  broker: BrokerConfig;
  /** What a run does when the broker is unreachable. */
  degradedMode: "native" | "refuse";
  policy: Policy;
  profiles: Profile[];
  hosts: Host[];
}

export interface ConfigIssue {
  path: string;
  message: string;
}

const PROVIDERS: readonly ProviderId[] = ["claude", "codex", "grok"];
const PERMISSIONS: readonly Permission[] = ["plan", "read-only", "acceptEdits", "workspace-write"];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isNonEmptyString = (value: unknown): value is string =>
  typeof value === "string" && value.trim() !== "";

/**
 * Parses untrusted configuration into a validated FactoryConfig. Issues are
 * returned as data so an operator sees every problem at once instead of
 * repairing the file one throw at a time.
 */
export function parseConfig(raw: unknown): { ok: true; config: FactoryConfig } | { ok: false; issues: ConfigIssue[] } {
  const issues: ConfigIssue[] = [];
  const add = (path: string, message: string) => issues.push({ path, message });

  if (!isRecord(raw)) return { ok: false, issues: [{ path: "", message: "configuration must be an object" }] };

  const brokerRaw = raw.broker;
  let broker: BrokerConfig = { kind: "native" };
  if (!isRecord(brokerRaw)) {
    add("broker", "must be an object with a kind");
  } else {
    const kind = brokerRaw.kind;
    if (kind !== "sub2api" && kind !== "native") {
      add("broker.kind", 'must be "sub2api" or "native"');
    } else {
      broker = { kind };
      if (kind === "sub2api") {
        const baseUrl = brokerRaw.baseUrl;
        if (!isNonEmptyString(baseUrl)) {
          add("broker.baseUrl", "is required when broker.kind is sub2api");
        } else if (!isAllowedOrigin(baseUrl)) {
          add("broker.baseUrl", "must be an https URL, or http on localhost");
        } else {
          broker.baseUrl = baseUrl.replace(/\/+$/, "");
        }
      }
      if (brokerRaw.timeoutMs !== undefined) {
        if (typeof brokerRaw.timeoutMs !== "number" || !Number.isFinite(brokerRaw.timeoutMs) || brokerRaw.timeoutMs <= 0) {
          add("broker.timeoutMs", "must be a positive number");
        } else {
          broker.timeoutMs = brokerRaw.timeoutMs;
        }
      }
    }
  }

  const degradedMode = raw.degradedMode;
  if (degradedMode !== "native" && degradedMode !== "refuse") {
    add("degradedMode", 'must be "native" or "refuse"');
  }

  const policyRaw = raw.policy;
  const policy: Policy = { stopAtPercent: 90, requireKnownQuota: true };
  if (policyRaw === undefined) {
    // Defaults are intentional: an operator who configures nothing still gets
    // a conservative gate rather than unbounded admission.
  } else if (!isRecord(policyRaw)) {
    add("policy", "must be an object");
  } else {
    if (policyRaw.stopAtPercent !== undefined) {
      const value = policyRaw.stopAtPercent;
      if (typeof value !== "number" || !(value > 0 && value <= 100)) {
        add("policy.stopAtPercent", "must be greater than 0 and at most 100");
      } else {
        policy.stopAtPercent = value;
      }
    }
    if (policyRaw.requireKnownQuota !== undefined) {
      if (typeof policyRaw.requireKnownQuota !== "boolean") {
        add("policy.requireKnownQuota", "must be a boolean");
      } else {
        policy.requireKnownQuota = policyRaw.requireKnownQuota;
      }
    }
  }

  const hosts = parseHosts(raw.hosts, add);
  const hostIds = new Set(hosts.map((host) => host.id));
  const profiles = parseProfiles(raw.profiles, hostIds, add);

  if (profiles.length === 0 && Array.isArray(raw.profiles)) add("profiles", "must contain at least one profile");
  if (hosts.length === 0 && Array.isArray(raw.hosts)) add("hosts", "must contain at least one host");

  if (issues.length > 0) return { ok: false, issues };
  return { ok: true, config: { broker, degradedMode: degradedMode as FactoryConfig["degradedMode"], policy, profiles, hosts } };
}

function parseHosts(raw: unknown, add: (path: string, message: string) => void): Host[] {
  if (!Array.isArray(raw)) {
    add("hosts", "must be an array");
    return [];
  }
  const hosts: Host[] = [];
  const seen = new Set<string>();
  raw.forEach((entry, index) => {
    const path = `hosts[${index}]`;
    if (!isRecord(entry)) return add(path, "must be an object");
    const id = entry.id;
    if (!isNonEmptyString(id)) return add(`${path}.id`, "is required");
    if (seen.has(id)) return add(`${path}.id`, `duplicates host "${id}"`);
    seen.add(id);
    const transport = entry.transport;
    if (transport !== "ssh" && transport !== "local") return add(`${path}.transport`, 'must be "ssh" or "local"');
    if (!isNonEmptyString(entry.target)) return add(`${path}.target`, "is required");
    const workspaces = entry.workspaces;
    if (!Array.isArray(workspaces) || workspaces.length === 0) {
      return add(`${path}.workspaces`, "must list at least one absolute path");
    }
    const paths: string[] = [];
    for (const [i, workspace] of workspaces.entries()) {
      if (!isNonEmptyString(workspace) || !workspace.startsWith("/")) {
        return add(`${path}.workspaces[${i}]`, "must be an absolute path");
      }
      paths.push(workspace.replace(/\/+$/, ""));
    }
    hosts.push({
      id,
      transport,
      target: entry.target,
      workspaces: paths,
      // Absent means "not allowed": a host never inherits a credential path it
      // was not explicitly granted.
      nativeLogin: entry.nativeLogin === true,
      healthy: entry.healthy !== false,
    });
  });
  return hosts;
}

function parseProfiles(raw: unknown, hostIds: Set<string>, add: (path: string, message: string) => void): Profile[] {
  if (!Array.isArray(raw)) {
    add("profiles", "must be an array");
    return [];
  }
  const profiles: Profile[] = [];
  const seen = new Set<string>();
  raw.forEach((entry, index) => {
    const path = `profiles[${index}]`;
    if (!isRecord(entry)) return add(path, "must be an object");
    const id = entry.id;
    if (!isNonEmptyString(id)) return add(`${path}.id`, "is required");
    if (seen.has(id)) return add(`${path}.id`, `duplicates profile "${id}"`);
    seen.add(id);

    // Report every field problem on one pass: an operator repairing a catalog
    // should not have to re-run validation after each fix.
    let valid = true;

    const provider = entry.provider;
    if (!PROVIDERS.includes(provider as ProviderId)) {
      valid = false;
      add(`${path}.provider`, `must be one of ${PROVIDERS.join(", ")}`);
    }

    const permission = entry.permission;
    if (!PERMISSIONS.includes(permission as Permission)) {
      valid = false;
      add(`${path}.permission`, `must be one of ${PERMISSIONS.join(", ")}`);
    }

    if (!isNonEmptyString(entry.group)) {
      valid = false;
      add(`${path}.group`, "is required");
    }

    if (!isNonEmptyString(entry.model)) {
      valid = false;
      add(`${path}.model`, "is required");
    }

    const profile: Profile = {
      id,
      provider: provider as ProviderId,
      group: entry.group as string,
      model: entry.model as string,
      permission: permission as Permission,
    };
    if (isNonEmptyString(entry.effort)) profile.effort = entry.effort;
    if (isNonEmptyString(entry.brokerUser)) profile.brokerUser = entry.brokerUser;

    if (entry.keyRef !== undefined) {
      if (!isNonEmptyString(entry.keyRef) || !/^(env|secret):\S+$/.test(entry.keyRef)) {
        valid = false;
        add(`${path}.keyRef`, 'must be a reference like "env:NAME" or "secret:path", never a key');
      } else {
        profile.keyRef = entry.keyRef;
      }
    }

    if (entry.hosts !== undefined) {
      if (!Array.isArray(entry.hosts)) {
        valid = false;
        add(`${path}.hosts`, "must be an array of host ids");
      } else {
        const hosts: string[] = [];
        for (const [i, hostId] of entry.hosts.entries()) {
          if (!isNonEmptyString(hostId)) {
            valid = false;
            add(`${path}.hosts[${i}]`, "must be a host id");
          } else if (!hostIds.has(hostId)) {
            valid = false;
            add(`${path}.hosts[${i}]`, `unknown host "${hostId}"`);
          } else {
            hosts.push(hostId);
          }
        }
        profile.hosts = hosts;
      }
    }

    if (valid) profiles.push(profile);
  });
  return profiles;
}

function isAllowedOrigin(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.protocol === "https:") return true;
    return url.protocol === "http:" && (url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]");
  } catch {
    return false;
  }
}
