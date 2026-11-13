# Crowley agent guidance

Read [CLAUDE.md](CLAUDE.md) for project rules and [docs/HANDOFF.md](docs/HANDOFF.md)
for current state. Follow the user's current instructions and authorization.

Scan operations handoff (monitoring, incidents, deploy recipes, Codex/Luna setup):
`.archive/handoff-scan-ops-20261112.md` (local, untracked). Run Codex here with the pinned binary
(`CROWLEY_CODEX_PATH` in `~/.config/crowley/secrets.env`) and `-p crowley` (model `gpt-6-luna`).

`crowley-scan` owns the game (relaunch, enter world, open the AH, reload). Operator guide:
[docs/REPEATING_SCANS.md](docs/REPEATING_SCANS.md). Before manual contact with the game or troubleshooting game
windows, read [local-game-control](.agents/skills/local-game-control/SKILL.md); include this path when delegating
game operation to a worker that does not inherit repository context. Delegate the whole bounded outcome and keep
parent oversight focused on milestones and blockers.

For shared producer/consumer changes, verify worker messaging tools and routing. Workers may lack the parent's
agent-name tools; use a small shared API contract file when direct routing is unavailable. Starting new work in an
idle worker requires a follow-up task; a plain message only queues context.

For native client API, scan serialization, or Lua harness changes, read
[crowley-client-contracts](.agents/skills/crowley-client-contracts/SKILL.md).

Maintain reusable lessons in `.agents/skills/` so they travel with the codebase.
Consolidate observed mistakes into the relevant existing skill; create a new skill
only for a distinct workflow. Keep instructions short and exclude incident logs,
secrets, transient process IDs, and screen coordinates.
