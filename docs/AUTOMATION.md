# Agent operation guide

Use the installed crowley-automation CLI for game operations. Source changes and passing
harness tests do not deploy an addon or qualify protected calls on the selected client.
The CLI reports runtimeImplemented separately from liveQualification. Current live client
qualification is unverified, and no qualified route/target profiles are shipped.

Read HANDOFF.md and check the installed version before operating. Do not assemble raw GUI
clicks, call protected APIs, type /cauto commands manually, or resurrect Queue.lua. Runtime
owns staging, exact argument readback, rate limits, receipts and scanner coordination.

## Inspect and prepare

```sh
crowley-automation observe
crowley-automation inventory --page 0
crowley-automation owned --page 0
crowley-automation mail --page 0
crowley-automation inspect batch.json
crowley-automation prepare batch.json
crowley-automation status
```

observe captures native identity, boot, publication sequence, relevant UI, gold, movement,
target/hover/bindings, native operation and ownership. It sends no protected action.
Inspection pages contain at most four rows; follow hasMore with the next page. The owned
view requests fresh native data under exclusive AH ownership and does not depend on a
scanner-populated cache. Inspection can type a bounded read-only command, so it still needs
focus, physical idleness and the GUI lease. It never pays COD or sends mail.

Batch JSON has exactly schemaVersion, house, character, expiresAt and actions. schemaVersion
is integer 1; expiresAt is an integer Unix timestamp. At most 32 ordered actions and 16 KiB
input are allowed. Integers reject booleans, fractional notation, nonfinite values and
out-of-range products. Unknown or duplicate keys fail. Canonical JSON's full SHA256 is
the batch ID; any field change changes the ID and requires matching authorization.

| Action kind | Exact additional fields |
|---|---|
| post_commodity / post_item | itemKey, quantity, unitPriceCopper, durationHours, maxDepositCopper |
| cancel_auction | auctionId, maxCancelCostCopper |
| collect_mail | messageLimit |
| interact | targetId |
| move_route | routeId |

Each action includes kind. Prices/cost caps are integer copper, quantity is 1..200, duration
is 12/24/48 hours, and messageLimit is 1..50. Posting matches the full canonical itemKey and
actual bag identity. Item prices are unit prices in the public spec; native item posting
uses total bid/buyout. The native callback recomputes stock, variant, costs, funds and
ownership. Unavailable facts refuse; no names or older observations substitute for identity.

Prepare records immutable intent without executing it. It does not arm a session. Save
specs and evidence privately. Runtime uses the canonical XDG state ledger shared with host;
production run/dry-run reject alternate ledger files that could bypass its interlock.

## Authorization and execution

Obtain authorization for the concrete inspected batch before real effects. Authorization
must cover the full digest, house/character, quantities/prices, duration and cost caps.
An earlier pricing recommendation, dry-run or different batch does not authorize a trade.
Do not ask again when the user already authorized that exact unchanged batch.

Observe the current boot and choose an explicit arm expiry within the next 120 seconds.
Both the batch and arm must stay valid throughout the operation. There is no automatic arm
renewal. Use small batches for initial qualification; a long routine can expire safely.

```sh
crowley-automation dry-run batch.json \
  --confirm FULL_BATCH_SHA256 --arm EXACT_OBSERVED_BOOT --arm-expires ARM_EXPIRY_EPOCH
crowley-automation run batch.json \
  --confirm FULL_BATCH_SHA256 --arm EXACT_OBSERVED_BOOT --arm-expires ARM_EXPIRY_EPOCH
crowley-automation status FULL_BATCH_SHA256
```

Dry-run performs native staging/readback and cost-cap/identity preflight. It cannot issue
protected clicks or movement holds. It may type preparatory commands and temporarily obtain
AH ownership. It reports validated requested caps and readiness, not an unavailable exact
computed charge. A real listing still needs the separately confirmed run and a native result.

One batch holds the shared GUI lease. Host checks the public runner/uncertainty guard before
policy and again before input; native busy state blocks maintenance independently of scanner
canvas validity. The optional Crowley_Host addon suspends scanner scheduling and waits for
existing work to quiesce before market admission. A known-safe grant stays with the same batch
across successive actions; explicit final cleanup resumes collection. Missing expected
coordination refuses. Automation does not import scanner or market implementations.

Receipts distinguish dispatch/callback delivery from verified native postcondition and action
completion. Inspect every action and primitive step, including partial mail/route progress.
A listing result means listed, not sold. Confirmed sales require settlement evidence.

## Measured targets and routes

Profiles live under the XDG configuration directory's crowley/automation/targets and routes
subdirectories. Each filename is its content-derived ID with .json. The Python public
TargetProfile and RouteProfile validators expose profile_id/route_id; write those exact
validated files and reference those IDs in a batch. Changing measurements changes the ID.
Missing profiles disable only referenced interaction/movement actions; there are no guessed
stock profiles or default coordinates.

Both profiles bind house, character, actual numeric native clientBuild and instanceId.
NPC targets pin exact name/GUID and expected AH UI. Mailbox profiles pin the exact Mailbox
world-object hover, measured screen rect, and a reviewed raw player-position anchor/radius.
The runtime moves the pointer without clicking, then verifies fresh hover/cursor, native
position, focus and presence before the real right-click. An already-open desired UI should
be handled when preparing the batch rather than assuming another interaction is necessary.

Route profiles additionally contain measured signed left/right radians-per-second rates,
forward units-per-second, tolerance and 1..64 raw x/y waypoints. Raw heading is atan2(dy,dx),
with forward=(cos facing,sin facing). The actual arrow bindings must read MOVEFORWARD,
TURNLEFT and TURNRIGHT. Holds are at most 0.25 seconds, one key at a time, with fresh feedback.
Stale/changed boot, build, instance, identity, combat/death/mount, wrong motion, takeover,
no progress or the waypoint deadline stop the route. A primitive maps one persisted waypoint;
an uncertain waypoint is never silently replayed. Historical beta measurements do not qualify
current Forever locations or controls.

The exact profile JSON fields are:

| Profile | Fields |
|---|---|
| NPC | schema=1, kind="npc", house, character, clientBuild, instanceId, expectedUi="ah", name, guid |
| Mailbox | schema=1, kind="mailbox", house, character, clientBuild, instanceId, expectedUi="mail", name="Mailbox", rect={x,y,width,height}, anchor={x,y,maxDistance} |
| Route | schema=1, house, character, clientBuild, instanceId, leftRadiansPerSecond, rightRadiansPerSecond, forwardUnitsPerSecond, tolerance, waypoints=[[x,y],...] |

Mailbox rect coordinates are integer physical screen pixels; maxDistance is positive and at
most five raw world units. Route tolerance is 0.1..5, signed turn rates have opposite signs
and magnitude at most 20 radians/second, and forward speed is positive. These bounds validate
data and do not replace measurements. Unknown/duplicate fields, booleans as numbers,
nonfinite values and content/filename mismatches fail. Profile files must be owned regular
files without group/world write permission.

Validate and obtain the filename without interacting with the game:

```sh
python -c 'from pathlib import Path; from crowley_automation.targets import TargetProfile; print(TargetProfile.from_json(Path("profile.json").read_bytes()).profile_id)'
python -c 'from pathlib import Path; from crowley_automation.movement import RouteProfile; print(RouteProfile.from_json(Path("route.json").read_bytes()).route_id)'
```

## Stop and reconcile

```sh
crowley-automation stop
crowley-automation status FULL_BATCH_SHA256
crowley-automation reconcile FULL_BATCH_SHA256 --ordinal ACTION_ORDINAL --step STEP_INDEX
crowley-automation stop --clear
```

Stop is durable and checked at input boundaries and waits. It does not undo a dispatched
effect or prove failure. No cleanup input is sent after stop/takeover. Host's own lifecycle
is controlled separately; read local-game-control before stopping/operating that service.

A crash after a dispatch marker, lost callback, changed boot or contradictory evidence is
uncertain and interlocks new batches. Do not retry it, invent a new digest to escape it,
clear native pending state, edit the ledger/SavedVariables, or reload/relaunch to hide it.
Reconcile accepts only a fresh causal native receipt bound to the original batch/action/step,
original boot and an advancing receipt sequence. It cannot declare success from command exit
or a human guess. If evidence remains inconclusive, retain the interlock and explain what
is known and what evidence is missing. Clearing stop is permitted only after uncertainty is
resolved; continuing needs current explicit authorization and a fresh session arm.

Read [ARCHITECTURE](ARCHITECTURE.md), [REPEATING_SCANS](REPEATING_SCANS.md) and
[TESTING](TESTING.md) for the supporting contracts and qualification limits.
