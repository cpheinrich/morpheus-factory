import type { BrokerSnapshot, GroupUsage, Profile, QuotaWindow } from "../types.js";
import type { Auth, BrokerPort } from "./port.js";

export interface CapacityBrokerOptions {
  /** Origin of the adapter that publishes capacity. Never a provider origin. */
  baseUrl: string;
  /** Env var holding the capacity token; the token itself is never stored here. */
  tokenEnv?: string;
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
  now?: () => Date;
}

/**
 * Reads one fleet-wide capacity document.
 *
 * Deliberately not a sub2api client. The gateway's admin payloads are
 * version-specific and user-facing, so a small adapter publishes the stable
 * shape below instead; that keeps a broker upgrade from breaking placement.
 *
 *   GET {baseUrl}/factory/v1/capacity
 *   { "observedAt": "<iso8601>",
 *     "groups": { "<group>": { "inFlight": 0, "maxConcurrency": 5,
 *                              "healthyAccounts": 2,
 *                              "windows": [{ "id": "5h", "usedPercent": 12,
 *                                            "resetsAt": "<iso8601>" }] } } }
 *
 * Anything unreadable is reported as unreachable: a broker that cannot prove
 * capacity must not be treated as having some.
 */
export class CapacityBroker implements BrokerPort {
  readonly id = "capacity";
  readonly baseUrl: string;
  #tokenEnv: string | undefined;
  #timeoutMs: number;
  #fetch: typeof globalThis.fetch;
  #now: () => Date;

  constructor(options: CapacityBrokerOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.#tokenEnv = options.tokenEnv;
    this.#timeoutMs = options.timeoutMs ?? 5_000;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#now = options.now ?? (() => new Date());
  }

  async snapshot(): Promise<BrokerSnapshot> {
    const at = this.#now().toISOString();
    const headers: Record<string, string> = { accept: "application/json" };
    const token = this.#tokenEnv === undefined ? undefined : process.env[this.#tokenEnv];
    if (token !== undefined && token !== "") headers.authorization = `Bearer ${token}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.#timeoutMs);
    try {
      const response = await this.#fetch(`${this.baseUrl}/factory/v1/capacity`, {
        headers,
        signal: controller.signal,
      });
      if (!response.ok) {
        return { broker: this.baseUrl, reachable: false, observedAt: at, groups: {}, error: `HTTP ${response.status}` };
      }
      const parsed = parseCapacity(await response.json());
      if (!parsed.ok) {
        return { broker: this.baseUrl, reachable: false, observedAt: at, groups: {}, error: parsed.error };
      }
      return { broker: this.baseUrl, reachable: true, observedAt: parsed.observedAt, groups: parsed.groups };
    } catch (error) {
      const reason = error instanceof Error && error.name === "AbortError" ? "timed out" : "request failed";
      return { broker: this.baseUrl, reachable: false, observedAt: at, groups: {}, error: reason };
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Emits a reference, never a value. The runner resolves it at spawn time so
   * the key cannot end up in a plan, a log line or a Paperclip comment.
   */
  auth(profile: Profile): Auth {
    return {
      mode: "broker",
      baseUrl: this.baseUrl,
      keyRef: profile.keyRef ?? `env:MORPHEUS_FACTORY_KEY_${envSuffix(profile.id)}`,
    };
  }
}

export function envSuffix(id: string): string {
  return id.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

type Parsed = { ok: true; observedAt: string; groups: Record<string, GroupUsage> } | { ok: false; error: string };

export function parseCapacity(raw: unknown): Parsed {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return { ok: false, error: "capacity is not an object" };
  const record = raw as Record<string, unknown>;
  const observedAt = record.observedAt;
  if (typeof observedAt !== "string" || Number.isNaN(Date.parse(observedAt))) {
    return { ok: false, error: "capacity.observedAt is not a timestamp" };
  }
  const groupsRaw = record.groups;
  if (typeof groupsRaw !== "object" || groupsRaw === null || Array.isArray(groupsRaw)) {
    return { ok: false, error: "capacity.groups is not an object" };
  }

  const groups: Record<string, GroupUsage> = {};
  for (const [name, value] of Object.entries(groupsRaw as Record<string, unknown>)) {
    const parsed = parseGroup(value);
    if (parsed === null) return { ok: false, error: `capacity.groups["${name}"] is malformed` };
    groups[name] = parsed;
  }
  return { ok: true, observedAt, groups };
}

function parseGroup(value: unknown): GroupUsage | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const inFlight = number(record.inFlight);
  const maxConcurrency = number(record.maxConcurrency);
  const healthyAccounts = number(record.healthyAccounts);
  if (inFlight === null || maxConcurrency === null || healthyAccounts === null) return null;
  if (maxConcurrency <= 0) return null;

  const windowsRaw = record.windows;
  if (!Array.isArray(windowsRaw)) return null;
  const windows: QuotaWindow[] = [];
  for (const entry of windowsRaw) {
    if (typeof entry !== "object" || entry === null || Array.isArray(entry)) return null;
    const window = entry as Record<string, unknown>;
    const id = window.id;
    const usedPercent = number(window.usedPercent);
    if (typeof id !== "string" || id === "" || usedPercent === null) return null;
    windows.push({ id, usedPercent, resetsAt: typeof window.resetsAt === "string" ? window.resetsAt : "" });
  }
  return { inFlight, maxConcurrency, healthyAccounts, windows };
}

function number(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}
