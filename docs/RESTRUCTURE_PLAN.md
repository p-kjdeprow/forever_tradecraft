# Crowley cleanup and architecture plan

Updated 2026-11-15. Implementation authorized using parallel Luna implementation and Sol review loops.
Changes land on main; live trading remains subject to specific authorization.

Crowley will retain its working AH collection, main dashboard, and recipe watch, remove inactive
workflows, separate scanning from game control, and then add fresh automation for listing,
cancellation, mailbox collection, and movement. Completion means another agent can use a documented
interface to perform those operations and verify their effects without controlling the scanner or
reading implementation details.

Work proceeds by acceptance gates, without a calendar deadline. Available time should buy stronger
evidence and simpler boundaries, rather than additional features or infrastructure.

## Confirmed scope

The user has selected these requirements:

- Retain the main dashboard and recipe watch. Remove the advanced dashboard, advanced advice and
  forecast views, paper trading, and backtest workflows.
- Delete every inactive workflow, including the dormant executor and queue pipeline. Build the
  required automation afresh rather than retaining those workflows as an implementation scaffold.
- Automate AH listing and cancellation, mailbox collection, and movement between required locations.
  AH buying, vendor trading, and crafting automation are outside the final scope.
- Separate the scanner and automation into distinct modules and installable packages. This plan
  also gives them separate WoW addons so either can operate without the other installed.

Active dashboard calculations stay even when they discuss crafting or vendor economics. A retained
calculation does not imply retaining the corresponding simulation, strategy runner, or game action.

Existing project rules remain: integer copper, canonical itemKey identity, house isolation, no
fallback implementations, new database migrations only, private evidence, one GUI owner, and the
hard limit of at most one automated click per second with the required human pointer motion.
Building automation does not authorize real trades. The current requirement for authorization of
a specific queue is carried forward as authorization of an exact immutable operation batch, with
a separate session arm. Committing, pushing, and destroying data still require permission.

## Current dependencies that affect the work

The addon owns actual AH queries and a 900 second sweep timer. The Python process named
`crowley-scan` owns game launch and recovery, AH opening, scan-aware reload timing, and persistence
checks. The companion uploads SavedVariables and writes acknowledgements for a subsequent reload.

The following dependencies make deletion by directory unsafe:

| Connection | Consequence |
|---|---|
| `Agent.lua` requires Scanner and Executor; Scanner consults Agent lifecycle and construction fences | Removing either side requires changing startup and scheduling together |
| Schema 6 combines scan, movement, hover, executor, chat, and render state | Removing unused fields requires coordinated Lua publication, Python validation, and fixture changes |
| `analysis/trading_summary.py` imports the advisor cache and trading facts, recipes, fees, and pricing helpers | The dashboard needs a small extraction before the old workflows can disappear |
| Scan ingestion warms advanced advice, and the advanced dashboard calls advice and paper APIs | Remove background work and consumers along with the retired endpoints |
| The companion reads multiple record types and writes `Queue.lua` | Remove queue synchronization and unused record handlers with their configuration and generated schemas |
| The launcher invokes `crowley-scan presence-ok` and `probe-frame` | These must move to game control before launch can become independent of a scan plan |
| Installer resources are symlinked into the source checkout | Wheel and sdist contents must be checked; source edits may also affect an installed development addon |

Read-only checks found scanner and companion services active and the health, market-backup, and
paper-trading timers inactive. Disabled backup and health timers are operational settings, not
proof that backup, restore, or health functionality is unused. The focused scan, GUI-frame, and CLI
baseline is 210 passing tests; it is not the full repository or native-client qualification.

## Phase 0 establish the deletion inventory and contracts

Before deleting code, produce a workflow inventory with its entry points, actual consumers,
transitive dependencies, persisted records, tests, and final owner. Use imports, CLI registrations,
routes, frontend requests, timers, generated files, and installed configuration. A registered route
or a passing test establishes reachability, not actual use. The user's scope decisions above
determine which inactive products are retired.

Record the current checkout changes and installed artifact identities. Preserve the existing
uncommitted dashboard turnover changes. Check whether the installed addon is copied or points into
this checkout; implement in an isolated staging checkout if a normal collector reload could consume
unfinished source. Do not stash, reset, or overwrite unrelated changes.

Capture representative original scan and acknowledgement fixtures privately, plus sanitized fixtures
for tests. Characterize the active dashboard outputs, recipe watch, scan ingestion, cold rotation,
upload rejection behavior, save flush, and acknowledgement loading. Retain distinctions between
queried, incomplete, empty, and priced observations and between listing depletion and confirmed sales.
Protect the current six observed market units per hour filter and gap exclusion behavior.

Write the following short architecture decisions before implementing their consumers:

1. Package ownership and dependency direction.
2. Scanner publication, persistence state, and scan suspension semantics.
3. Separate game and execution telemetry, action staging, and acknowledgement semantics.
4. GUI ownership, AH resource ownership, and reload coordination.
5. Operation identity, authorization, effect reconciliation, and restart behavior.
6. Package resources, configuration ownership, and the coordinated upgrade procedure.

These decisions specify observable behavior and concrete race conditions, not speculative extension
frameworks. Fix contracts before building new automation; do not freeze assumptions about unmeasured
native APIs.

**Exit gate:** every workflow has a disposition, every retained dependency has an owner, current
behavior has representative checks, and the package and wire boundaries are written down.

## Phase 1 delete inactive functionality

Delete complete workflows, including their commands, routes, configuration, background jobs,
generated fields, UI controls, tests that only describe the retired feature, and obsolete docs.
Preserve independent market correctness tests and useful native-format fixtures. Do not keep
deprecated command aliases, disabled feature implementations, or compatibility facades for retired
workflows. Recover old implementation from repository history if later research needs it.

| Area | Planned deletion | Required preservation or extraction |
|---|---|---|
| Advanced UI and advice | `/dashboard/advanced`, advanced HTML, advice endpoints and CLI, advice warming, evaluator orchestration | Extract the cache and exact calculations used by the main dashboard and recipe watch |
| Forecasting | Forecast model workflows, fitting, reports, validation CLI, advanced advice integration | Preserve observed market activity and history calculations used by retained outputs |
| Paper trading and backtests | Simulator, strategy execution, paper state progression, CLI, API, timer/service templates, reports, paper-to-queue conversion | Extract facts, recipe requirements, prices, fees, and numeric helpers required by retained analysis |
| Old execution and queues | All executor action kinds and state machines, old queue schemas and CLI/API, queue fetching and `Queue.lua`, execution upload paths, executor telemetry | Preserve the published rules about authorization, one protected call per click, causal observation, and uncertain effects as requirements for the fresh implementation |
| Unused account and reference capture | Dormant capture commands and addon modules, their upload routes and handlers when no retained consumer uses them | Preserve existing reference/client data used by analytics; keep only historical readers demonstrably required by retained queries |
| Retired session machinery | Native construction and restore workflows, fences used only by that lifecycle, measurement commands/panels, unused motion/hover/mail/executor publication fields | Keep scanner timing, pending storage, acknowledgements, and current game controls working through a reduced explicit publication contract |
| Abandoned experiments | Private telemetry-region tooling and other experiments with no retained consumer | Keep the actual screenshot backend, native input tool, source verification, and build resources used in production |
| Historical runtime targets and legacy APIs | Unselected runtime target support and old API presentation paths with no retained consumer | Keep fixtures needed to test archived formats and keep access required by retained historical market queries |
| Support code | Helpers, options, environment variables, dependency entries, harness branches, and fixture builders left unreferenced after these removals | Retain code with a concrete active build, diagnosis, backup, restore, or verification purpose |

An entire `trading/` or `advisor/` directory cannot be deleted while the active analysis still imports
it. Extract only the required pure calculations into their intended market-domain ownership, then
remove the workflow. This limited extraction enables deletion; the broader package move remains in
Phase 2. Prune unused parameters and branches inside retained helpers as well.

Replace the combined telemetry contract atomically with the final minimal scanner and game-status
contracts defined in Phase 0, updating producer, decoder, generated schemas, and fixtures together.
Do not introduce a temporary reduced schema that will need another redesign during the package
move. Remove lifecycle calls in both directions. The currently working AH-opening and recovery
routine consumes those minimal game facts before it moves in Phase 2. The fresh execution driver
is not introduced during cleanup.

Preserve all applied SQL migrations and their ledger unchanged. Retiring a workflow removes its
runtime behavior, not existing market, beta, paper, queue, or execution records. Do not drop tables,
erase `.archive`, rewrite SavedVariables, or delete private evidence. If a retained historical format
is still required to read stored data, its narrow reader has a current purpose. A separate approved
data-retention migration can address obsolete stored data later.

Update the README and operational docs to describe the reduced product. Remove stale instructions
and broken links from maintained docs; repository history carries historical implementation detail.
Do not present the retired executor contract as an active API.

**Exit gate:** the main dashboard, recipe watch, collector, companion, launcher, health, and backup
and restore tools still work; retired commands and endpoints are absent; there are no dangling
imports, unused generated fields, or workflow-only dependencies. Relevant unit, integration,
database, package, lint, and type checks pass.

## Phase 2 restructure into independent components

Use one repository with three functional distributions and a small shared contract distribution.
Scanner and automation are independently installable; market analysis is separated so a server
does not install desktop control and a desktop controller does not install the database stack.
The contract library contains only types, validation, serialization rules, and protocol versions.
It has no GUI, filesystem discovery, network, database, or scheduler behavior.

| Distribution | Responsibility | Dependencies and exclusions |
|---|---|---|
| `crowley-contracts` | Canonical item and house identity; scan, action, result, and arbitration wire contracts | Small pure library; generated Lua constraints come from the same source |
| `crowley-market` | Ingestion, storage, retained analysis, main dashboard, recipe watch, server health, backup/restore | Contracts and server dependencies; no automation or scanner implementation imports |
| `crowley-scanner` | Scanner addon, installed scan plan, SavedVariables transport, upload and ACK handling, collector status | Contracts and transport dependencies; no keyboard, pointer, launcher, movement, or executor implementation |
| `crowley-automation` | Game ownership, launch, observation, input, routine execution, and later trading actions | Contracts and desktop dependencies; no market database, scan catalog, analytics, or scanner implementation dependency |
| Host composition | Configure the optional cooperation between scanner and automation, install selected components | Explicit composition entry points; not another general-purpose service or framework |

Start with this ownership structure and add submodules only for actual responsibilities:

```text
packages/
  contracts/src/crowley_contracts/
  market/src/crowley_market/
    domain/ application/ adapters/ entrypoints/
  scanner/src/crowley_scanner/
    domain/ application/ adapters/ entrypoints/
  automation/src/crowley_automation/
    domain/ application/ adapters/ entrypoints/
apps/
  host/                    # deployment and optional composition
addon/
  Crowley/                 # scanner; retain installed name and CrowleyDB identity
  Crowley_Data/            # scanner plan and acknowledgements only
  Crowley_Automation/      # introduced when its fresh implementation is ready
  Crowley_Host/            # small optional addon integration for combined operation
tests/
  contracts/ market/ scanner/ automation/ integration/
docs/
```

Keep the scanner addon's installed `Crowley` name and existing SavedVariables filename and keys.
Renaming it merely for symmetry would create an avoidable state migration. Automation owns a
different addon namespace and SavedVariables file. Its absence must not disable scanning.
Likewise, automation must observe and control the game when no scanner, catalog, or companion is
installed. Addon files ship in their owning package, with independent manifests and lifecycle tests.

The Python `crowley.scan` loop is chiefly game control today. Move launch, presence, process/window
ownership, keys, pointer primitives, and the existing control loop into automation. Move pure
scanner persistence observations and scan coordination into the scanner's public contract and the
host integration. Keep the behavior small; do not reconstruct the deleted session stack.
Create the minimal automation addon for these already-active game observations in this phase;
fresh market action staging and execution are added only in Phase 3. The host composition depends
on public interfaces from both sides, while neither side imports the host composition or the
other implementation. Combined operation is integration, not a circular dependency.

Each component has one explicit configuration model and owns its paths and state. Separate native
client build and window facts from scan-plan identity. The launcher's presence and in-world probe
must work without resolving companion configuration or loading a scan plan. A scan catalog change
must not require rebuilding automation.

Inside each package, domain code is pure, application code coordinates operations through explicit
ports, and adapters implement native APIs, desktop input, transport, persistence, or HTTP. Entry
points only assemble these pieces. Generic `core`, `utils`, or shared runtime dumping grounds are
replaced by named ownership. Extract an abstraction when there is an actual boundary to enforce.

### Separate the two resources being shared

The GUI lock serializes desktop input. It does not prevent an addon timer from starting AH queries
while another addon is posting or cancelling. Combined operation therefore also needs an AH
ownership protocol inside the client.

The host integration uses public contracts to request scanner suspension, observe that the current
query work has quiesced, and grant an operation exclusive AH ownership. Both scheduling and action
admission check that ownership, closing the race between an external observation and the next
timer callback. It never reads another module's private state. Standalone and coordinated operation
are explicit installation modes; a missing coordinator in coordinated mode is an error.

Grants bind to the current client boot and operation identity. Reload, restart, expiry, or an
uncertain effect invalidates permission to send another action. Invalidating a grant does not prove
an outstanding native operation finished; scanner resumption waits for a known safe state. Test
both orderings of timer start versus operation admission and the loss of an owner while in flight.

The scanner publishes scan activity, persistence needs, pending IDs, acknowledgements, and the
next due time. Automation publishes game and execution facts separately. A malformed movement
sample must not invalidate a scan publication. The host integration handles scan-aware save flush
and acknowledgement reloads through these public facts. The generic game controller never parses
the companion's internal state file or depends on a catalog hash.

Reload is an application operation with explicit preconditions and observed completion. It cannot
interrupt an admitted market effect, active query, or unresolved operation. The existing memory,
disk, upload, ACK-written, ACK-loaded, and eventual disk-pruned stages remain distinct.

Build and install wheels and sdists in clean environments with the repository unavailable. Check
resource ownership, CLI registration, dependency direction, and coordinated contract versions.
Remove old import paths and command aliases once deployment changes; do not leave a permanent
parallel architecture.

**Exit gate:** scanner alone collects when a person opens the AH; automation alone launches and
observes the game without scanner data; market alone ingests and serves retained outputs without
desktop dependencies. Combined operation preserves unattended collection through the explicit
integration. Every combination has a package-install and contract check.

## Phase 3 build the fresh automation foundation

Introduce a local structured interface that this agent and another agent can use. CLI commands
produce bounded JSON observations and receipts; the same application layer serves programmatic
callers. The deployed routine is deterministic and does not call an LLM.

The interface covers observing the current client, preparing and inspecting an immutable operation,
running a dry run, submitting an authorized batch, checking status, stopping, and reconciling an
uncertain result. Commands use house, character, itemKey, exact auction IDs, quantities, integer
copper prices, deposit limits, durations, and operation IDs. No API accepts item names as identity,
unrestricted Lua, arbitrary shell, or blind screen coordinates.

Use the existing supported model of allowlisted addon slash commands with fresh telemetry readback
to stage one bounded action at a time. Specify and test the command grammar and byte limits before
building its consumers. The addon publishes the staged action identity and its validated arguments
or canonical digest; Python verifies them before enabling execution. Protected API calls occur only
in the real button callback, one protected call per actual click. The old remote queue service and
per-job Queue.lua/reload pipeline are not rebuilt.

Independent automation telemetry covers game identity and boot, publication sequence and time,
position and motion, relevant open UI, inventory and owned-auction facts, the staged action, button
geometry/readiness, callback ordinal, and result state. Keep bounded domain publications separate
where their update rate or failure behavior differs. Frame transport can share byte primitives
without sharing one all-or-nothing semantic snapshot. Define payload limits and freshness rules
from actual native samples rather than recreating the old unused diagnostic fields.

Use one active routine runner and a small local durable operation ledger. Persist intent and a
dispatch marker before sending an input that can cause an effect. Record callback delivery and
native postconditions separately. Retain only the operational checkpoint needed for restart;
evidence blobs stay outside the checkpoint and are not replayed during startup.

Example action states are prepared, admitted, dispatched, observed, succeeded, refused, failed,
cancelled, and uncertain. Each transition must protect a concrete invariant. Dispatch without a
causal observation is uncertain. A lost acknowledgement cannot establish failure or authorize a
second post. Exactly-once delivery across process failure is not assumed; uncertain effects stop
until fresh inventory, owned auctions, mail, or native receipts establish what happened.

Authorization binds to the exact immutable batch, character, house, client session, quantities,
prices, and cost limits. Preserve two independent arms and clear the session arm on reload or
logout. A changed plan requires new authorization. Use the existing hard click gate and pointer
rules globally, including launcher and executor clicks. Check fresh target geometry, focus,
presence, identity, and action admission immediately before a press. The callback counter proves
delivery; the native result proves the effect.

**Exit gate:** scripted fake-native tests and captured fixtures prove the staging and receipt
contracts; dry runs cannot issue protected calls; duplicate submissions and restarts cannot
silently repeat an effect; a second process cannot acquire GUI ownership. Stale frames, focus
loss, human presence, mismatched identity, payload overflow, and unknown results stop admission.

## Phase 4 implement the required operations

Implement operations in the following order. Prove every primitive separately before combining it
into an unattended routine.

| Order | Operation | Completion evidence |
|---|---|---|
| 1 | Observe inventory, gold, owned auctions, and the relevant UI | Fresh native facts tied to the current character, house, boot, and observation |
| 2 | Interact with the nearby auctioneer and mailbox | Intended target and interaction range verified; the matching UI actually opens |
| 3 | List a commodity at an exact price, quantity, duration, and deposit ceiling | Correct inventory identity, protected callback receipt, native creation result, and matching owned listing |
| 4 | List a non-commodity item using its complete itemKey | Correct item variant and bag location, measured native post semantics, and matching owned listing |
| 5 | Cancel one exact owned auction | Auction identity and cancellation cost verified; native cancellation and subsequent ownership/mail state reconciled |
| 6 | Collect eligible mailbox contents | Message/item/money changes verified after each protected action; bag capacity checked; no COD payment or outgoing mail |
| 7 | Walk measured routes between the AH and mailbox | Position, facing, bounded progress, arrival, and interaction range verified |
| 8 | Compose a bounded selling routine | Every step has a receipt; scanning resumes after the AH is safely released |

Commodity and non-commodity posting are distinct native contracts. The old executor supported only
commodity posting. Verify item-level identity, native duration/price units, stack rules, deposits,
result events, and button geometry on the selected client before declaring item listing supported.
Re-read bag locations immediately before action admission; an itemID alone cannot identify a
specific item variant. Add stack manipulation only if a required posting case needs it, with its
own observed steps.

Listing policy is separate from execution. Initial automation executes an exact reviewed spec for
owned inventory. Pricing recommendations may come from the retained market API or an agent, but
neither is a dependency of movement or the action runner. Cancellation and relisting must have
explicit criteria and costs; avoid an unlimited cancel/repost loop.

Routes are measured for the deployed client, zone, and character movement mode. Start with the
actual AH/mailbox route rather than a general navigation engine. Use bounded key holds and
position/facing feedback. Stop on lost progress, changed instance, combat, death, focus loss, or
human takeover. Do not add speculative detours, mount handling, or combat recovery.

The complete routine observes and reconciles state, obtains AH ownership, lists or cancels the
specified auctions, releases ownership, visits the mailbox when required, collects eligible
contents, returns, reopens the AH, and lets ordinary collection continue. Interruptions are handled
at known action boundaries; no reload or relaunch is used to escape an unresolved market effect.

A listing result means listed, not sold. A sale depends on a buyer. Confirmed sales require actual
owned-auction/mail settlement evidence; listing depletion and a successful posting callback do not
establish revenue. Account for deposits, cancellation costs, returned items, and received gold
without collecting private mail subjects or sender names unnecessarily.

**Exit gate:** every operation has meaningful success, refusal, race, and interruption coverage;
native behavior is qualified first with dry runs and then individually authorized small live
operations. Full routines are enabled only after their component operations pass.

## Phase 5 deploy and prove the finished system

Prepare the complete package set, generated Lua artifacts, configuration diff, service changes,
and rollback artifacts before a live cutover. Back up database and private runtime state and check
restoration on a disposable database. Preserve migrations and stored data throughout. Use a quiet
collector boundary after saving, uploading, and acknowledging the preceding sweep.

Prefer staged artifacts and one coordinated cutover over maintaining two permanent runtimes.
Restart or reload only after installation is complete. Verify the game actually loaded the new
addon and contract versions, not merely that files on disk changed. Rollback may restore compatible
code and configuration but must retain the newest execution receipts and never re-arm an operation
that might already have taken effect. Historical unsafe code remains disabled.

Qualify these scenarios explicitly:

- Scanner, automation, and market each install and run in their supported standalone mode.
- Scanner timer and posting/cancellation attempts race in both orders without concurrent AH use.
- Scan completion, save flush, upload, ACK write, ACK load, and subsequent cleanup are independently
  observed, including companion/network interruption and recovery.
- A known route completes, a nearby interaction opens the right UI, and a manual takeover stops
  further routine actions.
- A commodity listing and a non-commodity listing are each observed in owned auctions after
  individually authorized trials; one cancellation and one mailbox collection are reconciled.
- Restart after preparation, during dispatch, after callback delivery, and before persistence never
  silently duplicates a listing or misreports an uncertain effect.
- Stale observations, changed bags, insufficient gold/capacity, vanished auctions, contract drift,
  reload, and owner loss produce explicit outcomes.
- The main dashboard and recipe watch preserve their expected semantics, including the turnover
  floor and outage gaps; removed endpoints and commands stay absent.
- Cold-slice coverage and scanner cadence recover after routine interruptions. Report measured
  sampling gaps, including gaps caused by walking or selling; do not treat them as zero activity.

Run qualification long enough to cover cold rotation, ordinary reload/ACK cycles, routine movement,
and the targeted interruption cases. Runtime performance limits should come from observed scan
slack and interaction costs. A fixed count of successful runs alone does not establish reliability
or a runtime percentile.

Finish by updating architecture, operator instructions, package and CLI help, handoff, and reusable
agent skills. Add a concise agent operation guide with exact commands, observation meanings,
authorization scope, receipts, stop behavior, and reconciliation rules. The final skill must point
to the supported interface rather than teach agents to assemble raw GUI actions.

**Exit gate:** another agent can follow that guide to inspect, dry run, execute a specifically
authorized operation, verify it, stop, and recover a known interruption. There is one supported
implementation of each workflow, no old executor/queue/simulator path, no circular package imports,
and no unowned configuration or generated artifacts.

## Execution discipline

Work in bounded changes with a clear invariant and exit gate. First delete, then move surviving
code once into its final ownership, then add the missing behavior. Do not combine a market schema
redesign, a native API behavior change, and a package move in one change.

Use these implementation batches; they are reviewable units, not an assumption about the number
of commits. Committing remains subject to the repository rule.

| Batch | Deliverable | Required evidence |
|---|---|---|
| 1 | Workflow disposition ledger, current-state baseline, and architecture decisions | Retained entry points and data obligations are explicit |
| 2 | Extraction of retained analysis calculations; deletion of advanced advice and forecasts | Main dashboard and recipe-watch outputs preserve their semantics |
| 3 | Deletion of simulation, backtests, paper services, and old queue API/transport | Retired commands and consumers are gone; no active output imports their runtime |
| 4 | Addon cleanup, removal of dormant capture/executor/lifecycle paths, final minimal publications | Scanner lifecycle, scheduling, save flush, and ACK behavior still pass |
| 5 | Removal of remaining orphan helpers, options, dependencies, experiments, and docs | Completed deletion ledger and clean retained suite |
| 6 | Contracts and market package extraction | Clean wheel/sdist installs; market runs without desktop packages |
| 7 | Scanner package and companion extraction | Scanner installs without automation and preserves existing SavedVariables |
| 8 | Existing game-control extraction, minimal automation addon, and host integration | Automation runs without a scan plan; timer/admission/reload races are covered |
| 9 | Fresh operation interface, command staging, authorization, and receipt ledger | Dry runs, duplicate submissions, crashes, and uncertain results are covered |
| 10 | Inventory/ownership observations and nearby interaction | Native observations agree with actual client state |
| 11 | Commodity posting, item posting, cancellation, and mail primitives | Each native contract passes its isolated qualification |
| 12 | Measured movement and bounded routine composition | Route, takeover, interruption, and scanner resumption are observed |
| 13 | Coordinated deployment, full qualification, docs, and agent skill | Another agent completes the supported workflow through the public interface |

The old addon can depend on the queue wire schema during cleanup, so batches 3 and 4 must share a
coordinated artifact boundary where necessary. Do not deploy the intermediate half of a removed
producer/consumer contract. The market and scanner package moves likewise ship as a coordinated
artifact set when their import/configuration changes require it.

Use captured native evidence and deterministic clocks before requesting live interaction. Preserve
original failed bytes; the harness models the client and cannot by itself establish native behavior.
Test new coordination at real publication and lifecycle boundaries rather than testing setters in
isolation. Database checks use disposable instances. Package checks run outside the checkout.

At each gate, run the relevant tests, Ruff checks, and type checks. Add regression tests for actual
behavior and boundary failures, and architecture checks for forbidden imports and installation
dependencies. Delete tests whose sole purpose is to retain retired behavior. Broaden checks when
a change touches a wider boundary, and finish with the complete retained suite.

Maintain a concise disposition ledger and update it as each deletion or migration lands. The work
is complete when every remaining function has an active product, runtime, diagnostic, build,
recovery, or verification purpose in the agreed scope. Do not leave speculative future action kinds,
parallel command transports, or an extensible strategy framework for features outside that scope.
