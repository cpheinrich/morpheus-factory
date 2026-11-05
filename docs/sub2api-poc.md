# sub2api PoC — runbook

Status: working proof of concept. Deployed 2026-10-31; runbook revised
2026-11-04.

## What this is

A single-machine deployment of [sub2api](https://github.com/Wei-Shaw/sub2api)
(an AI subscription → API gateway) in **simple mode** on `zoe-mac-mini`, with two
personal subscriptions registered behind it:

| Subscription | sub2api account | Group | API key (name) |
|---|---|---|---|
| Claude (Pro/Max) | `claude-max` (Anthropic OAuth) | `anthropic-default` | `claude` |
| ChatGPT/Codex (Pro) | `codex-pro` (OpenAI OAuth) | `openai-default` | `codex` |

The gateway exposes Anthropic- and OpenAI-compatible endpoints. A CLI points its
base URL at it and authenticates with an `sk-` gateway key; sub2api routes to the
matching subscription account.

This repo owns only the **install** (`scripts/setup-sub2api.sh`) and this
runbook. Who uses the gateway, when, and with which key is decided by the
Morpheus Factory plugin (`cpheinrich/morpheus`, `plugins/factory`), described
below.

## Current state

- The Factory plugin delegates worker runs to the gateway end to end. Chris
  reported full worker delegation working on 2026-11-04.
- The plugin's gateway quota inspection, 2026-11-04, read Claude at **59%** of
  its 5-hour window and **27%** of its 7-day window, and Codex at **100%** of its
  7-day window. Codex is therefore not eligible for gateway work until that
  window resets.
- Not claimed: that every Claude Code / Codex mode (all streaming variants,
  websockets, every resume path) works through the gateway. The plugin README
  records which worker paths have been exercised.

## How work reaches the gateway

The Factory plugin, not a script in this repo, routes delegated work:

- Gateways are listed in the plugin config by provider, origin and key
  **reference** (`env:` / `secret:`), never a raw key.
- Quota is read from the sub2api admin API at each inspection and start (15 s
  timeouts; Claude via an active usage read, Codex from its stored snapshot).
  An account counts as its most-used unexpired 5-hour or 7-day window. A
  gateway is used while below the threshold (90% used by default); unknown
  quota is never treated as headroom. With no eligible gateway the controller
  picks the other provider's native login, never the coordinator's own.
- The CLI is launched with per-process overrides (base URL and gateway token).
  Global `~/.claude` / `~/.codex` configuration and native logins are not
  rewritten.

See `plugins/factory/README.md` ("Gateway mode") in `cpheinrich/morpheus` for
configuration, quota details and the `doctor` command.

### Relation to factory's broker design

`docs/design.md` describes the broker port: a factory-owned
`/factory/v1/capacity` document that a small adapter would publish from the
gateway. That remains the planned shape for factory placement; it is **not**
built yet, and nothing in `src/brokers/` talks to this deployment. The plugin's
direct admin-API read is the working path today.

## Deployment

The deployment lives **outside** the repo (it holds secrets):

```
~/sub2api-deploy/
  docker-compose.local.yml   # upstream file at a pinned commit, SHA-256 checked
  docker-compose.pin.yml     # pins the application image release
  .env                       # secrets, mode 600 — never printed or committed
  data/ postgres_data/ redis_data/
```

Install or re-apply with:

```sh
scripts/setup-sub2api.sh            # idempotent; preserves .env and data
```

- Runs on Colima. `docker compose` needs the compose plugin
  (`brew install docker-compose` + `cliPluginsExtraDirs` in
  `~/.docker/config.json`). After a reboot: `colima start`, then re-run the
  script.
- `.env` sets `RUN_MODE=simple` (no billing or balance checks),
  `SIMPLE_MODE_AUTO_CREATE_DEFAULT_GROUPS=true` (seeds the default groups) and
  `TZ=America/Los_Angeles`.
- Admin UI: port 8080, login `admin@sub2api.local`. The password is the
  `ADMIN_PASSWORD` line of `~/sub2api-deploy/.env`; the script never prints it.
- **Upgrades are deliberate.** Bump `COMPOSE_REF` + `COMPOSE_SHA256` and the
  image tag (`SUB2API_IMAGE_TAG`, default `0.2.19`) together after reading the
  upstream changes. A checksum mismatch aborts before anything changes.

## Network

- sub2api binds `0.0.0.0:8080` by default (`BIND_HOST` in `.env`), so it is
  reachable from the tailnet as
  `http://zoe-mac-mini.tail7476d9.ts.net:8080` — **and from the local LAN**.
  To restrict it, set `BIND_HOST` in `.env` to the machine's tailnet address
  and re-run the script.
- Plain HTTP at the app layer; WireGuard encrypts tailnet traffic. No TLS is
  configured. Revisit before anything leaves the tailnet.

## Gotchas

- **`gpt-5.5` is retired.** The Codex "Use Key" dialog defaults to `gpt-5.5`,
  which OpenAI retired from Codex-with-ChatGPT on 2026-10-14. Use a current
  model such as `gpt-6.1-sol`.
- **Claude native login vs gateway token.** With both a saved Claude login and a
  gateway token present, the Claude CLI was observed to use the saved login and
  fail with `401` against the gateway (see the plugin README).
- **Two Tailscale installs.** An orphaned Tailscale.app extension can make bare
  `tailscale status` report "stopped". The Homebrew tailscaled is live:
  `tailscale --socket /var/run/tailscaled.socket status`.
- **One owner per subscription.** A host that also holds a native login for a
  brokered account can rotate the same refresh chain and invalidate the
  gateway's copy (`docs/design.md`, credential rule 3).
- **Terms of service.** sub2api's README warns that routing subscription traffic
  through a gateway may violate upstream terms. This PoC uses two personal
  subscriptions; that is the operator's call.

## Open questions

- Which sub2api usage fields are reliable enough to publish as factory's
  `/factory/v1/capacity` document, and at what freshness. The plugin found that
  Claude's stored 5-hour figure goes stale and needs an active read, and that
  Codex figures are upstream snapshots of varying age.
- Easy onboarding of further machines and workers (next step; not in this PR).

---

## Appendix A — Review of PR #20, 2026-11-04

The PR originally added three scripts. Review findings and what was done:

| Finding | Resolution |
|---|---|
| `setup-sub2api.sh` printed `ADMIN_PASSWORD` to the terminal (violates the no-secrets-in-logs rule) | Never printed; output names the `.env` line instead. `.env` is forced to mode 600 on every run. |
| Compose file fetched from upstream `main` on every run; image `:latest` | Pinned to commit `f17918ea` with a SHA-256 check before install (mismatch aborts, nothing changes); image pinned to `0.2.19` via an override file. Both match what was live on 2026-10-31. |
| `configure-client.sh` overwrote `~/.claude/settings.json`, `~/.codex/config.toml` and `auth.json` wholesale, built JSON/TOML by string interpolation, and left the Claude key world-readable | **Removed.** It also globally redirected every CLI session on the host, including a coordinator's own, which the plugin's per-process overrides deliberately avoid. |
| `route.sh` fell back silently to the native login (contradicting controller-only delegation), interpolated the admin password into JSON, used the threshold in shell arithmetic (command injection via `SUB2API_THRESHOLD`), had no HTTP timeouts, read Claude's stale stored 5-hour figure, and picked only one Codex window rather than the most-used | **Removed.** The plugin implements the same rule with key references, timeouts, active Claude reads and fail-closed handling. |
| Runbook claimed only single inference calls; mixed planning with current state | Rewritten to current observed state, with the broker design marked as planned. |

## Appendix B — Verification

- `tests/test_setup_sub2api.py` (unittest, no network or real Docker): secrets
  never appear in output and `.env` is 600; reruns keep `.env` and skip the
  download when the pin matches; a checksum mismatch changes nothing and does not
  start the stack; non-release image tags are refused; the committed pin is a
  full commit and digest.
- `shellcheck scripts/setup-sub2api.sh`: clean.
- The pinned compose digest was computed from
  `raw.githubusercontent.com/Wei-Shaw/sub2api/f17918ea…/deploy/docker-compose.local.yml`;
  Docker Hub showed `latest` and `0.2.19` published together on 2026-10-28.
- The revised script was **not** re-run against the live deployment.
- Original PoC smoke checks (2026-10-31): `GET /health` 200 over localhost,
  tailnet IP and MagicDNS; Anthropic `POST /v1/messages` and OpenAI
  `GET /v1/models` / `POST /v1/responses` returned real completions.
