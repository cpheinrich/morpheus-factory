# Worker bootstrap

Run `bash worker/setup.sh` from a factory clone on the Mac that will host workers.
The entry point runs the scripts in `worker/setup/` in order. Each stage is
rerunnable, and `--check` reports local readiness without changing the host.
`--code-root /absolute/path` chooses where the Morpheus checkout lives.

| Stage | Responsibility |
| --- | --- |
| `prerequisites.sh` | Check macOS, login user and Apple command-line tools. |
| `apps.sh` | Remove iMovie, GarageBand, Keynote, Pages and Numbers after verifying their Apple bundle IDs; remove orphaned GarageBand audio content when no compatible app remains. |
| `tools.sh` | Install Git, Node, pnpm and gh through Homebrew when missing. |
| `source.sh` | Clone Morpheus without resetting an existing checkout. |
| `morpheus.sh` | Install the copied Morpheus CLI when missing; the watchdog uses its guarded worktree cleanup. |
| `spotlight.sh` | Exclude existing source, worktree, VM and cache roots from Spotlight indexing. |
| `factory.sh` | Install locked dependencies and build the factory package. |
| `cleanup.sh` | Install the hourly storage watchdog launchd job. |

This prepares the host locally. Fleet activation will need separate stages for
the factory catalog, broker connectivity, provider CLIs and Morpheus's runner.
Those stages should keep credentials out of this repository and should not
start a worker unless their readiness checks pass.
