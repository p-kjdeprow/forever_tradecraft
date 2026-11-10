# Live trading test runbook

Who: the operator runs the in-game steps (with `crowley-scan` stopped, see [REPEATING_SCANS](REPEATING_SCANS.md)); the data agent runs server-side steps and checks.
Mode: **paper only** unless the user explicitly authorizes a specific armed test (step 5). Every executor click is a dry run
unless the published queue is armed **and** `/crowley arm <queueId>` was typed in the current session
([contract](EXECUTOR_CONTRACT.md)).

## 0. Launch deployment and identity

The beta is over. These are launch-day instructions; no game input, service restart,
migration or deployment is authorized by this document alone. Review the active
[HANDOFF](HANDOFF.md) and [launch target procedure](LAUNCH_TARGET.md) first.
The addon is a symlink into this checkout, so a later game reload consumes local source.

1. Record the actual launch build/interface, native region/realm/house, executable,
   installation roots and client facts. Edit the packaged launch target and regenerate
   the coordinated artifacts and staged configuration. Review drift checks and the
   complete staged diff before installing it. Preserve beta data and route provenance.
2. At an authorized quiet boundary, take a market backup and deploy the reviewed server.
   Production currently has migrations 001–008; migration 009 adds immutable paper-run
   capability metadata. Verify health and retained beta rows. Create a new launch-house
   paper run with observed capabilities; do not relabel or rewrite a beta run.
3. Install the coordinated addon/data source and reviewed staged configuration with `crowley-ops install`.
   Configure the companion's exact launch house and character. With no queue configured,
   it writes `CROWLEY_QUEUE = false`; queue data stays in Queue.lua, outside CROWLEY_DATA.
4. Reload the addon only at the authorized boundary. Confirm no Lua errors and no active queue.
   Preserve original frames and receipts privately.

## 1. Account capture
In game: open bags/mailbox/profession window once, then `/crowley account` (prints gold, slots, professions, recipes, mail
state) and an ordinary saving reload. Data agent: `GET /v1/houses/<house>/account-state/latest?character=<name>` returns
the capture; compare gold and a few bag stacks with what the operator sees. No names, senders or subjects are stored.

## 2. Reference capture (deposits)
With the AH open: `/crowley reference`, then a saving reload. Data agent: check `latest_item_reference` and
`item_deposits`; compare real deposits with the engine's assumed rate (D1, 15 % of vendor value per 12 h) and replace the
assumption with measured deposits (code change + new paper run).

## 3. Dry-run queue, every kind
Data agent publishes an **unarmed** test queue covering each action kind for items the character actually has/needs (spec = `house`, `character`, `expiresAt`, `actions` per the
[contract](EXECUTOR_CONTRACT.md); `armed` omitted):
`uv run python -m crowley.trading queue publish --spec dry-run.json` (prints the exact queue, then submits it), or from the
launch paper run: `uv run python -m crowley.trading queue from-paper --run <launch-paper-run> --character <name>`
(always unarmed). Within one companion poll the queue reaches `Queue.lua`; `queue status --house <house> --character
<name>` shows the active queue. Operator: reload, open the matching frame (AH / merchant / mailbox / profession window),
click the Crowley executor button once per action. Expected per click: "dry run: would …", a `dry_run` result, no
protected call, nothing traded. Then a saving reload (the companion uploads `CrowleyDB.executions`);
data agent runs `queue reconcile --queue <queueId>` and checks every action has a `dry_run` result with the resolved details (prices, bag slots, merchant index, recipe).

## 4. Refusal checks (still unarmed)
Expired queue, wrong character, AH action during an active sweep, wrong frame open, two clicks < 1 s apart, `/crowley arm`
with a wrong id: each must refuse with its reason code and record it.

## 5. Armed micro-test — ONLY with explicit user authorization for this exact queue
Build the exact armed queue with one cheap action (e.g. buy 1 unit of a cheap commodity with a tight max price; later
queues: post that unit at a high price for 12 h; then cancel it):
`queue preview --spec micro.json --armed --out micro-queue.json` (no network; prints the queue and its full queueId).
The user reviews that preview and authorizes it; then
`queue publish --spec micro-queue.json --armed --confirm-real-trades <full queueId>` submits exactly that file. Operator types
`/crowley arm <queueId>`, clicks once per protected step, `/crowley disarm` afterwards. Data agent reconciles each result
against the account capture and the next sweep (bought unit in bags, listing visible in owned auctions, cancellation).

## Rollback
Stop `crowley-scan` and disarm the session (`/crowley disarm`); clear the published queue through the ordinary
queue workflow. Restore a reviewed coordinated source/config version at an authorized quiet
boundary. Preserve all original execution results and receipts for audit;
never manufacture permission to retry an unknown effect.

## Not covered

The route walker, world-click and mail loop were deleted with the session stack (2026-11-09), so nothing walks to
the mailbox or clicks the executor button automatically. The steps above are operator-driven. The executor
button's rendered bounds, XTEST callback delivery and protected API behavior are still unqualified on the launch
client; a future driver must follow [EXECUTOR_TELEMETRY](EXECUTOR_TELEMETRY.md) and the click rules in `CLAUDE.md`.
