/**
 * The factory's vocabulary. Everything here is data: a profile describes a
 * logical worker, a host describes a machine, and a run spec is the immutable
 * result of resolving one against the other. Nothing in this module performs
 * I/O, so every rule below is directly testable.
 */

export type ProviderId = "claude" | "codex" | "grok";

export type Permission = "plan" | "read-only" | "acceptEdits" | "workspace-write";

/**
 * A logical worker. A profile names subscription capacity, not a machine: the
 * same profile may run on any host it is eligible for, and several hosts may
 * run the same profile at once. That is the portability property.
 */
export interface Profile {
  id: string;
  provider: ProviderId;
  /** Broker group that supplies capacity for this profile. */
  group: string;
  model: string;
  effort?: string;
  permission: Permission;
  /** Empty or absent means "any registered host". */
  hosts?: string[];
  /**
   * Broker-side identity. One per profile so concurrency and attribution stay
   * separate; never a secret.
   */
  brokerUser?: string;
  /**
   * Where the worker's gateway key comes from, as a reference such as
   * `env:NAME` or `secret:factory/claude-max`. A reference only: the value is
   * resolved by the runner at spawn time and never appears in a plan.
   */
  keyRef?: string;
}

export interface Host {
  id: string;
  transport: "ssh" | "local";
  target: string;
  /** Absolute workspace roots this host may hand out. */
  workspaces: string[];
  /**
   * Whether this host may run on its own native provider login when the broker
   * is unreachable. This is the degraded mode, not the normal path.
   */
  nativeLogin: boolean;
  healthy: boolean;
}

export interface RunRequest {
  /** Paperclip issue or run identifier. */
  task: string;
  profile: string;
  /** Operator pin; bypasses placement order but never eligibility. */
  host?: string;
  workspace?: string;
}

export interface QuotaWindow {
  id: string;
  usedPercent: number;
  resetsAt: string;
}

export interface GroupUsage {
  /** Requests currently in flight across the whole fleet. */
  inFlight: number;
  maxConcurrency: number;
  /** Provider-reported windows for the accounts behind this group. */
  windows: QuotaWindow[];
  /** Accounts currently eligible to serve. */
  healthyAccounts: number;
}

export interface BrokerSnapshot {
  broker: string;
  reachable: boolean;
  observedAt: string;
  groups: Record<string, GroupUsage>;
  /** Why the snapshot is empty, when it is. Never a credential or provider body. */
  error?: string;
}

export type RefusalCode =
  | "unknown-profile"
  | "no-eligible-host"
  | "host-unhealthy"
  | "workspace-not-found"
  | "broker-unreachable"
  | "no-healthy-account"
  | "concurrency-exhausted"
  | "quota-exhausted"
  | "quota-unknown";

export interface Refusal {
  code: RefusalCode;
  detail: string;
}

export type Admission = { ok: true } | { ok: false; refusal: Refusal };

export interface Placement {
  host: Host;
  workspace: string;
}

/**
 * A fully resolved turn. The runner consumes this; it never consults the
 * profile catalog or the broker itself, so a stale plan cannot silently change
 * which subscription paid for a run.
 */
export interface RunSpec {
  task: string;
  profile: string;
  provider: ProviderId;
  host: string;
  transport: Host["transport"];
  target: string;
  workspace: string;
  model: string;
  effort?: string;
  permission: Permission;
  auth:
    | { mode: "broker"; baseUrl: string; keyRef: string }
    | { mode: "native" };
  quota: { source: string; observedAt: string; windows: QuotaWindow[] };
  generatedAt: string;
}

export type PlanResult =
  | { ok: true; spec: RunSpec }
  | { ok: false; refusal: Refusal };
