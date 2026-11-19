# Bounded agent work

Crowley's installed scanner has no model calls. The 2026-11-19 quota audit found
the cost in long coding/review conversations and repeated oversight. Optimize that
workflow while preserving collection, dashboard calculations and trading guards.

## Work with short context

- Read the compact HANDOFF and only relevant linked docs. The dated history is
  retained for investigation; do not load it into every task.
- Use the pinned Codex binary with `-p crowley` for the existing Luna profile.
  Model/reasoning choices remain explicit; unavailable models are an error, not
  permission to switch silently to a more expensive model.
- Default to one implementation owner. If delegation is authorized and useful,
  use fresh context, concrete acceptance criteria, file ownership and artifact
  paths. Keep at most two workers in flight and no worker-spawn trees.
- Run one independent review for changes that warrant it. Fix real findings;
  request another review only for changed code or unresolved correctness concerns.
  Do not repeatedly resume workers to check unchanged work or wait for another worker.
- Keep complete build/test/output logs in private files. Return exit status,
  counts and failure excerpts; inspect additional lines only when needed. Project
  Codex config retains at most 2,000 tokens per tool output; full artifacts remain available.
- Run focused tests after the last relevant code change. Repeat or broaden only
  for a new edit, failure or specific uncovered concern. Prepare offline checks,
  coherent wheels and rollback before deploying once.
- End a completed task. Start the next bounded task with current state and links,
  rather than carrying the entire implementation conversation forward.

## Wait and verify locally

```sh
# Anchor ID is the last accepted sweep before the change. All core IDs are checked.
uv run crowley-ops watch --after-sweep 1234 --count 2 \
  --timeout 2400 --output "$XDG_STATE_HOME/crowley/qualification.json"
# Optional exact cohort, e.g. for a newly added feature:
uv run crowley-ops watch --after-sweep 1234 --items 118,2589 --count 2
```

Use the real anchor and an absolute private output path (expand your actual state
directory if XDG_STATE_HOME is unset). Run **one** command and await tool completion.
It polls the packaged deployment locally, emits only changed evidence and a final
outcome, and writes a private receipt. No model, game input, screenshot, reload,
service start or trade occurs. Explicit operator stops are preserved.

Exit 0 means the requested consecutive accepted sweeps are full and all watched
items succeeded; inspect `cadence_matches` before claiming the start cadence.
Exit 2 means an evidence/service fault, 3 stopped services, 4 deadline, 130 cancellation.
Partial/failed sweeps break the consecutive sequence. Historical qualification
does not prove the scanner is currently online. No p95 is inferred from two sweeps.

## Account for usage locally

```sh
uv run crowley-ops usage --hours 24
uv run crowley-ops usage --hours 24 --all-projects
```

The default scope is the current working directory; run at repository root or
pass `--project /absolute/repo`. Counters come from local active Codex sessions,
with duplicate snapshots and inherited lifetime totals excluded. Cached input
is included in input; reasoning is included in output. These are **not exact
quota charges** or a cloud/other-machine audit. No prompt or credential is printed.

The trusted project config compacts growing context at 64,000 tokens and caps
retained tool output and worker concurrency. It
does not change the selected model, reasoning level, permissions or game behavior.
New settings apply to new sessions; a long existing conversation does not shrink
just because the configuration changed.

Config reference: [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).
