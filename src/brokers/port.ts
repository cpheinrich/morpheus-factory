import type { BrokerSnapshot, Profile, RunSpec } from "../types.js";

export type Auth = RunSpec["auth"];

/**
 * The seam that makes the subscription broker replaceable.
 *
 * Factory depends on this and nothing else, so swapping sub2api for another
 * gateway — or for host-native logins — is an adapter change, not a factory
 * change. It also means no broker schema is allowed to leak past this port.
 */
export interface BrokerPort {
  readonly id: string;
  /** One fleet-wide capacity read. Must fail closed, never throw. */
  snapshot(): Promise<BrokerSnapshot>;
  /** How a resolved run authenticates upward. Never returns a secret. */
  auth(profile: Profile): Auth;
}

export function unreachable(broker: string, now: Date): BrokerSnapshot {
  return { broker, reachable: false, observedAt: now.toISOString(), groups: {} };
}
