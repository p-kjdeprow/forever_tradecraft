# Local Crowley operations

Crowley can verify accepted scans and summarize local usage without a model
performing repeated status checks. These commands do not change Codex sessions.

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

