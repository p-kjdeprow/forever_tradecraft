# Refactor package and operation interfaces

Read with RESTRUCTURE_PLAN.md and RESTRUCTURE_EXECUTION.md. This is the shared implementation
contract for parallel workers. Parent owns updates. No live game operations or real trades are
authorized by this file.

## Package ownership

Use these four component distributions and a thin root host application. Do not create more
packages, a plugin framework, or empty layer directories. Existing modules move into named owners.
The common distribution groups already-shared OS helpers with contract modules to avoid duplicating
privacy/path code or adding a fifth foundation library. Contract validation itself remains pure.

- packages/common/src/crowley_common: house, auction_rules, errors, scanformat, game_target,
  watchlist, data, platform (paths/privfs only), and later frames (pure byte/schema/pixel validation).
  It has no scanner runtime, GUI input, database, HTTP, action execution, or component resolvers.
- packages/market/src/crowley_market: retained market models/cache/economics/client_data,
  analysis, volume, server, store, client_import (old core/client_data), and backup support as needed.
- packages/scanner/src/crowley_scanner: companion, state (old companion_state), savedvars,
  luawriter, scanner configuration/plan transport, and owned addon resources Crowley/Crowley_Data.
  No GUI/launcher or automation imports.
- packages/automation/src/crowley_automation: input/capture/native adapters, standalone game
  observation/config, new operations/ledger/runner, movement, and Crowley_Automation addon.
  No scanner implementation, market, catalog/plan resolution, HTTP client, or database imports.
- Root application source becomes apps/host/src/crowley_host: the existing scan-aware policy loop,
  integration observer/actor, combined installation/doctor/health CLI and explicit composition.
  Only this application may import both scanner and automation implementations.

Common may ship the selected target/catalog descriptors consumed by scanner and market, but
automation configuration must not resolve or load a catalog/scan plan. Each installed component
owns its configuration; the host installer can render them from the host contract. Build isolated
wheels and sdists; no resource paths through a checkout and no duplicated ownership across wheels.

## Staging during parallel work

Package extraction may prepare the new package trees while the old paths remain in use by the
addon worker. This is an uncommitted staging step, not a compatibility layer: parent switches
all imports and deletes the original trees in the coordinated integration. Do not change the old
addon/scan/gui files while that worker owns them. Copy final addon and frame resources only when
parent signals the addon loop is complete. Generated target and migrations keep their bytes.

## Fresh operation model

Operation schemas belong to automation, not the market service. Public input is strict JSON and
exact integers. No buys, crafting, vendors, arbitrary Lua, or arbitrary screen actions.

An immutable batch contains schema version, expected house and character, expiration, and ordered
actions. Its full SHA256 canonical JSON digest is the batch ID. Supported actions are post_commodity,
post_item, cancel_auction, collect_mail, interact, and move_route. Posting identifies exact itemKey,
quantity, integer unit price, 12/24/48-hour duration, and maximum deposit. Cancellation names the exact
auction ID and maximum cancellation cost. Mail has a bounded message limit and never pays COD or
sends mail. Interaction/route targets are explicitly configured known targets/routes, not fuzzy names.

Validate every action's exact keys, reject booleans as integers, nonfinite numbers, unknown kinds,
bad item identities, oversize data and duplicate JSON keys. A dry run never calls a protected API.
Specific real batch authorization plus a separate current-client session arm is required for live
effects; changing the batch changes its ID and authorization. There is no remote queue pipeline.

One local runner owns execution. Persist immutable intent and a dispatch marker before an effect
can be sent. Record actual callback delivery separately from the native result. A crash after a
dispatch marker, missing acknowledgement, changed boot or ambiguous effect produces uncertain,
never an automatic retry. A duplicate submission returns the existing record; a changed spec under
an existing ID is rejected. Terminal success requires a verified postcondition, not command exit 0.

Keep the ledger small: a local SQLite file is sufficient, with a narrowly bounded schema and
transactions for operation identity and state changes. It is not a general workflow engine or
event-sourcing system. Evidence blobs are separate private files and are not replayed at startup.
Use one concrete runner and explicit methods at real side-effect boundaries; do not create a generic
command bus or dozens of ports. The engine must be testable with a small fake backend.

The runner's backend contract stages a validated action, obtains a fresh admission observation,
dispatches at most one bounded primitive, and returns a causal receipt/native postcondition.
Incomplete or uncertain receipts stop that operation. GUI ownership and client-side AH ownership
are separately required for market actions. Session/identity, human presence, focus and freshness
are rechecked before dispatch. The existing hard click interval and curved pointer path are retained.

Combined operation has one GUI owner: the host loop executes submitted automation through the
public runner instead of allowing an independent trading process to race its scan-maintenance
actions. Standalone automation refuses a conflicting GUI owner. During an active or uncertain
effect the host cannot type scan commands, reload, or relaunch behind the runner. Use a small
explicit ownership handoff/status interface; do not add a second supervisor or message broker.
Mail collection and walking use bounded primitive steps with a persisted progress cursor. Every
protected mail call gets its own dispatch marker and causal receipt. Posting and cancellation
remain one protected-call step each.

CLI behavior should be explicit and JSON-readable: prepare/inspect, dry-run, run with exact batch
confirmation and session arm, status, stop, and reconcile. Do not ship a CLI that claims to execute
but only mutates ledger state. The first foundation batch may expose prepare/status/inspection while
native backend integration is pending; label that implementation stage accurately.

## Native posting evidence

The installed Auctionator modern-AH source uses PostCommodity(location, duration, quantity,
unitPrice) and PostItem(location, duration, quantity, minBid, buyout), with item/commodity deposit
calls distinguished by native commodity status. This establishes another addon's call signatures,
not observed protected-call behavior on this client. The stored Crowley probe records these APIs
on client build 70862, not the currently selected 70938. Recheck actual current-client availability,
event order, price units, and protected callback behavior during the native implementation gate.
Do not copy Auctionator fallback/default behavior, and do not claim a dry run proves a real post.

## Parallel wave two write scopes

Package worker owns NEW packages/common/, packages/market/, packages/scanner/ and their package
metadata/tests under tests/packages/. It may read original source but may not modify/delete it yet,
nor edit root pyproject/uv.lock/existing tests/docs/addon/scan/gui.

Operation worker owns NEW packages/automation/ and tests/automation/. Initially implement only the
strict action/batch model, private durable ledger, bounded runner contract, meaningful fake-backend
verification and honest prepare/status interface. Do not copy/edit existing GUI/scan/addon code yet.
Do not implement native effects or movement until their exact adapter contracts are handed off.
Keep runtime Python below roughly 700 lines for this foundation unless a concrete invariant needs
more; discuss scope growth with parent. No placeholder success paths or speculative action options.

Parent owns final import switch, root workspace metadata, old-tree deletion, resource handoff,
host composition, docs, verification and git operations. Native addon implementation remains with
the addon worker until its existing loop is reviewed.
