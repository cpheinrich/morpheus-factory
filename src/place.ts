import type { Host, Placement, Profile, Refusal, RunRequest } from "./types.js";

/**
 * Chooses a host and workspace for a profile. Pure: the same request against
 * the same catalog always yields the same placement, and configuration order is
 * the operator's preference order.
 *
 * Eligibility is two separate questions and the distinction matters to the
 * caller: "this profile may not run here" is a configuration error, while
 * "every eligible host is unhealthy" is an operational wait.
 */
export function place(request: RunRequest, profile: Profile, hosts: Host[]): { ok: true; placement: Placement } | { ok: false; refusal: Refusal } {
  const allowed = profile.hosts && profile.hosts.length > 0 ? profile.hosts : hosts.map((host) => host.id);
  const pinned = request.host;

  if (pinned !== undefined) {
    if (!allowed.includes(pinned)) {
      return refuse("no-eligible-host", `profile "${profile.id}" may not run on host "${pinned}"`);
    }
    const host = hosts.find((candidate) => candidate.id === pinned);
    if (host === undefined) return refuse("no-eligible-host", `unknown host "${pinned}"`);
    if (!host.healthy) return refuse("host-unhealthy", `host "${host.id}" is marked unhealthy`);
    return withWorkspace(request.workspace, host);
  }

  const candidates = hosts.filter((host) => allowed.includes(host.id));
  if (candidates.length === 0) {
    return refuse("no-eligible-host", `no registered host matches profile "${profile.id}"`);
  }

  const healthy = candidates.filter((host) => host.healthy);
  if (healthy.length === 0) {
    return refuse("host-unhealthy", `every host eligible for "${profile.id}" is unhealthy: ${candidates.map((h) => h.id).join(", ")}`);
  }

  if (request.workspace !== undefined) {
    for (const host of healthy) {
      const selected = selectWorkspace(request.workspace, host);
      if (selected.ok) return { ok: true, placement: { host, workspace: selected.workspace } };
    }
    return refuse("workspace-not-found", `no eligible host can reach workspace "${request.workspace}"`);
  }

  const first = healthy[0]!;
  const workspace = first.workspaces[0]!;
  return { ok: true, placement: { host: first, workspace } };
}

function withWorkspace(pinnedWorkspace: string | undefined, host: Host): { ok: true; placement: Placement } | { ok: false; refusal: Refusal } {
  if (pinnedWorkspace === undefined) {
    return { ok: true, placement: { host, workspace: host.workspaces[0]! } };
  }
  const selected = selectWorkspace(pinnedWorkspace, host);
  return selected.ok ? { ok: true, placement: { host, workspace: selected.workspace } } : selected;
}

function selectWorkspace(requested: string, host: Host): { ok: true; workspace: string } | { ok: false; refusal: Refusal } {
  const normalized = requested.replace(/\/+$/, "");
  const root = host.workspaces.find((candidate) => within(normalized, candidate));
  if (root === undefined) {
    return refuse("workspace-not-found", `host "${host.id}" does not serve a root containing "${requested}"`);
  }
  return { ok: true, workspace: normalized };
}

/** True when `child` is `parent` itself or lives beneath it, respecting segment boundaries. */
export function within(child: string, parent: string): boolean {
  if (child === parent) return true;
  return child.startsWith(`${parent}/`);
}

function refuse(code: Refusal["code"], detail: string): { ok: false; refusal: Refusal } {
  return { ok: false, refusal: { code, detail } };
}
