import { admit } from "./admit.js";
import { place } from "./place.js";
import type { FactoryConfig } from "./config.js";
import type { BrokerPort } from "./brokers/port.js";
import type { Host, PlanResult, RunRequest, RunSpec } from "./types.js";

export interface PlanOptions {
  now?: () => Date;
}

/**
 * Resolves one turn end to end: catalog, placement, capacity, spec.
 *
 * The order is deliberate. Placement never depends on the broker, so a broker
 * outage cannot make a run unplaceable — it can only make it unadmitted, and
 * only when degraded mode forbids the fallback.
 */
export async function plan(
  request: RunRequest,
  config: FactoryConfig,
  broker: BrokerPort,
  options: PlanOptions = {},
): Promise<PlanResult> {
  const now = options.now ?? (() => new Date());

  const profile = config.profiles.find((candidate) => candidate.id === request.profile);
  if (profile === undefined) {
    return { ok: false, refusal: { code: "unknown-profile", detail: `no profile "${request.profile}"` } };
  }

  const placed = place(request, profile, config.hosts);
  if (!placed.ok) return placed;
  const { host, workspace } = placed.placement;

  const snapshot = await broker.snapshot();

  if (!snapshot.reachable) {
    const fallbackAllowed = config.degradedMode === "native" && host.nativeLogin;
    if (!fallbackAllowed) {
      const why = snapshot.error === undefined ? "" : ` (${snapshot.error})`;
      const hint = host.nativeLogin
        ? 'degradedMode is "refuse"'
        : `host "${host.id}" does not permit native login`;
      return {
        ok: false,
        refusal: { code: "broker-unreachable", detail: `broker "${snapshot.broker}" is unreachable${why}; ${hint}` },
      };
    }
    // Native fallback: capacity is unverified by definition, so it is reported
    // as such rather than as an empty window set that reads like "no usage".
    return {
      ok: true,
      spec: buildSpec(request, profile.provider, host, workspace, {
        model: profile.model,
        effort: profile.effort,
        permission: profile.permission,
        auth: { mode: "native" },
        quota: { source: "native", observedAt: snapshot.observedAt, windows: [] },
        now,
      }),
    };
  }

  const admission = admit(profile, snapshot, config.policy);
  if (!admission.ok) return admission;

  const usage = snapshot.groups[profile.group];
  return {
    ok: true,
    spec: buildSpec(request, profile.provider, host, workspace, {
      model: profile.model,
      effort: profile.effort,
      permission: profile.permission,
      auth: broker.auth(profile),
      quota: {
        source: snapshot.broker,
        observedAt: snapshot.observedAt,
        windows: usage?.windows ?? [],
      },
      now,
    }),
  };
}

interface SpecParts {
  model: string;
  effort: string | undefined;
  permission: RunSpec["permission"];
  auth: RunSpec["auth"];
  quota: RunSpec["quota"];
  now: () => Date;
}

function buildSpec(
  request: RunRequest,
  provider: RunSpec["provider"],
  host: Host,
  workspace: string,
  parts: SpecParts,
): RunSpec {
  const spec: RunSpec = {
    task: request.task,
    profile: request.profile,
    provider,
    host: host.id,
    transport: host.transport,
    target: host.target,
    workspace,
    model: parts.model,
    permission: parts.permission,
    auth: parts.auth,
    quota: parts.quota,
    generatedAt: parts.now().toISOString(),
  };
  if (parts.effort !== undefined) spec.effort = parts.effort;
  return spec;
}
