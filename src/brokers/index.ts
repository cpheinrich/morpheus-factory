import { CapacityBroker } from "./http.js";
import { NativeBroker } from "./native.js";
import type { BrokerPort } from "./port.js";
import type { BrokerConfig } from "../config.js";

export { CapacityBroker } from "./http.js";
export { NativeBroker } from "./native.js";
export type { Auth, BrokerPort } from "./port.js";
export { unreachable } from "./port.js";

/** Builds the broker the configuration asks for. Assumes a validated config. */
export function createBroker(config: BrokerConfig, now: () => Date = () => new Date()): BrokerPort {
  if (config.kind === "native") return new NativeBroker(now);
  if (config.baseUrl === undefined) {
    throw new Error("broker.baseUrl is required for the capacity broker; configuration was not validated");
  }
  return new CapacityBroker({ baseUrl: config.baseUrl, gatewayBaseUrl: config.gatewayBaseUrl, timeoutMs: config.timeoutMs, now });
}
