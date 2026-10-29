import type { BrokerSnapshot, Profile } from "../types.js";
import type { Auth, BrokerPort } from "./port.js";

/**
 * No broker: the host authenticates with its own provider login, exactly as the
 * pool does today. Capacity is unknown here, so a deployment using this adapter
 * must relax `policy.requireKnownQuota`; the host's own quota command remains
 * the execution-time gate.
 *
 * This exists so a broker outage degrades the fleet instead of stopping it.
 */
export class NativeBroker implements BrokerPort {
  readonly id = "native";
  #now: () => Date;

  constructor(now: () => Date = () => new Date()) {
    this.#now = now;
  }

  async snapshot(): Promise<BrokerSnapshot> {
    return { broker: this.id, reachable: true, observedAt: this.#now().toISOString(), groups: {} };
  }

  auth(_profile: Profile): Auth {
    return { mode: "native" };
  }
}
