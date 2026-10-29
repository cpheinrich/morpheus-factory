#!/usr/bin/env node
/**
 * Operator entry point.
 *
 * `plan` is the only executable path in this version because execution already
 * belongs to the Paperclip process adapter and the pool runner, which are
 * proven. Reimplementing them here would duplicate the one part of the system
 * that is not the factory's problem.
 *
 * Exit codes follow the pool's convention so a Paperclip run result stays
 * meaningful: 0 resolved, 75 refused and requeueable, 78 configuration or usage.
 */
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parseConfig } from "./config.js";
import { createBroker } from "./brokers/index.js";
import { plan } from "./plan.js";
import type { RunRequest } from "./types.js";

const USAGE = `factory <command> [options]

  plan --task <id> --profile <id> [--host <id>] [--workspace <path>] [--config <path>]
      Resolve one run into a spec, or print the refusal that stopped it.
  profiles [--config <path>]    List the profile catalog.
  hosts [--config <path>]       List hosts and eligibility.

Options:
  --config <path>   Configuration file (default: $FACTORY_CONFIG or ./factory.json)
`;

const REFUSED = 75;
const CONFIG = 78;

type Flags = Record<string, string>;

async function main(argv: string[]): Promise<number> {
  const [command, ...rest] = argv;
  const flags = parseFlags(rest);

  if (command === undefined || command === "help" || command === "--help") {
    process.stdout.write(USAGE);
    return command === undefined ? CONFIG : 0;
  }

  const configPath = resolve(flags.config ?? process.env.FACTORY_CONFIG ?? "factory.json");
  let raw: unknown;
  try {
    raw = JSON.parse(await readFile(configPath, "utf8"));
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unreadable";
    process.stderr.write(`factory: cannot read ${configPath}: ${detail}\n`);
    return CONFIG;
  }

  const parsed = parseConfig(raw);
  if (!parsed.ok) {
    process.stderr.write(`factory: ${configPath} is invalid\n`);
    for (const issue of parsed.issues) process.stderr.write(`  ${issue.path || "(root)"}: ${issue.message}\n`);
    return CONFIG;
  }
  const config = parsed.config;

  if (command === "profiles") {
    process.stdout.write(`${JSON.stringify(config.profiles, null, 2)}\n`);
    return 0;
  }

  if (command === "hosts") {
    process.stdout.write(`${JSON.stringify(config.hosts, null, 2)}\n`);
    return 0;
  }

  if (command === "plan") {
    if (flags.task === undefined || flags.profile === undefined) {
      process.stderr.write("factory: plan requires --task and --profile\n");
      return CONFIG;
    }
    const request: RunRequest = { task: flags.task, profile: flags.profile };
    if (flags.host !== undefined) request.host = flags.host;
    if (flags.workspace !== undefined) request.workspace = flags.workspace;

    const broker = createBroker(config.broker);
    const result = await plan(request, config, broker);
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    return result.ok ? 0 : REFUSED;
  }

  process.stderr.write(`factory: unknown command "${command}"\n\n${USAGE}`);
  return CONFIG;
}

function parseFlags(args: string[]): Flags {
  const flags: Flags = {};
  for (let index = 0; index < args.length; index += 1) {
    const token = args[index];
    if (token === undefined || !token.startsWith("--")) continue;
    const name = token.slice(2);
    const value = args[index + 1];
    if (value === undefined || value.startsWith("--")) {
      flags[name] = "";
      continue;
    }
    flags[name] = value;
    index += 1;
  }
  return flags;
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (error: unknown) => {
    process.stderr.write(`factory: ${error instanceof Error ? error.message : "unexpected failure"}\n`);
    process.exit(1);
  },
);
