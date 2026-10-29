import type { Admission, BrokerSnapshot, Profile, QuotaWindow } from "./types.js";
import type { Policy } from "./config.js";

/**
 * Decides whether the broker can serve one more turn for this profile's group.
 *
 * Kept separate from placement because the two failures have different
 * remedies: an unplaceable run is a configuration problem, a refused run is a
 * wait. The runner reports the code and Paperclip can requeue rather than fail.
 */
export function admit(profile: Profile, snapshot: BrokerSnapshot, policy: Policy): Admission {
  if (!snapshot.reachable) {
    return { ok: false, refusal: { code: "broker-unreachable", detail: `broker "${snapshot.broker}" did not answer` } };
  }

  const usage = snapshot.groups[profile.group];
  if (usage === undefined) {
    // No group means no data at all — nothing to gate on, so the only question
    // is whether the operator accepts unproven capacity.
    return policy.requireKnownQuota
      ? { ok: false, refusal: { code: "quota-unknown", detail: `broker reports no group "${profile.group}"` } }
      : { ok: true };
  }

  if (usage.healthyAccounts <= 0) {
    return { ok: false, refusal: { code: "no-healthy-account", detail: `group "${profile.group}" has no eligible account` } };
  }

  // Quota is the only gate that may be waived. Concurrency is a hard safety
  // limit and is enforced below regardless, so a relaxed quota policy can never
  // silently admit past the slot cap.
  let quotaKnown = true;
  if (usage.windows.length === 0) {
    if (policy.requireKnownQuota) {
      return { ok: false, refusal: { code: "quota-unknown", detail: `group "${profile.group}" reports no quota windows` } };
    }
    quotaKnown = false;
  } else {
    const unreadable = usage.windows.find((window) => !isReadable(window));
    if (unreadable !== undefined) {
      if (policy.requireKnownQuota) {
        return { ok: false, refusal: { code: "quota-unknown", detail: `window "${unreadable.id}" reported an unusable percentage` } };
      }
      quotaKnown = false;
    }
  }

  if (quotaKnown) {
    // At the threshold, refuse: the next turn would push past it, and the cost
    // of waiting is a requeue while the cost of overshooting is a provider rate
    // limit that strands every worker in the group.
    const blocked = usage.windows.find((window) => window.usedPercent >= policy.stopAtPercent);
    if (blocked !== undefined) {
      return {
        ok: false,
        refusal: {
          code: "quota-exhausted",
          detail: `window "${blocked.id}" is ${blocked.usedPercent}% used (stop at ${policy.stopAtPercent}%), resets ${blocked.resetsAt}`,
        },
      };
    }
  }

  if (usage.inFlight >= usage.maxConcurrency) {
    return {
      ok: false,
      refusal: {
        code: "concurrency-exhausted",
        detail: `${usage.inFlight}/${usage.maxConcurrency} slots in use for group "${profile.group}"`,
      },
    };
  }

  return { ok: true };
}

function isReadable(window: QuotaWindow): boolean {
  return Number.isFinite(window.usedPercent) && window.usedPercent >= 0 && window.usedPercent <= 100;
}
