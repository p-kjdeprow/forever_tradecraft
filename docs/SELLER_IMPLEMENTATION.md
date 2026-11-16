# Continuous seller implementation

Implementation authorized through parallel Luna implementation and independent read-only Sol review
loops. The parent owns integration, verification, documentation and git. The source is on main;
installation and live qualification are separate. Read [SELLER](SELLER.md) for operator commands
and [SELLER_PLAN](SELLER_PLAN.md) for the approved design and original plan review.

## Final business rules

- Commodities only for continuous selling; manual item posting remains supported.
- Always match the observed competing price; never undercut. An unknown price or below-floor
  price pauses that item. Empty-market posting needs an explicit starting price or a prior
  causal posting price.
- Post one small batch per item per 60 seconds while same-price competition is present.
  Native equal-price purchase priority is unknown; no claim of first-to-sell rank is made.
  Wait while our managed price is strictly lower. No catch-up posts.
- When another batch is needed and admitted bag stock is exhausted, cancel only active managed
  listings of that exact item. Freeze the cancellation set and continue in bounded chunks.
- Collect the exact commodity up to its outstanding cancelled quantity without requiring an
  auction-to-mail link. Stock is fungible within the exact item key. A whole attachment must
  fit the outstanding pool; never take COD, GM mail, money or unrelated attachments.
- Return to the measured AH, refresh observations, then decide again. Physical mailbox travel
  suspends market monitoring. Items share one serial controller and retain separate progress.

These rules supersede the early plan's strict tie-rank and auction-linked-mail evidence gates.
They are fixed behavior, not optional modes.

## Architecture and contracts

| Module | Responsibility |
|---|---|
| seller.py | Strict immutable policy, validated facts and pure pricing/stock decisions |
| seller_store.py | Session authorization, conserved stock, managed auctions, reservations and child derivation in the existing Ledger SQLite file |
| seller_native.py | Targeted native market, inventory, owned and selective-return observations |
| seller_loop.py | One foreground controller, fair monotonic scheduling, bounded recycle/travel progression and status |
| runner.py / ledger.py | Existing dispatch/receipt path with session-derived authorization and atomic seller bookkeeping |
| backend.py / runtime.py / cli.py | Concrete desktop/native transport, existing shared GUI lease and public commands |

There is no second database, queue, workflow service, scanner import or market-history dependency.
The optional host addon coordinates exclusive quiescent AH ownership through the scanner's public
suspend/resume contract. The seller holds the existing GUI lease for its foreground lifetime.
Every protected operation still uses the existing native staging/readback and human-speed input
path, with fresh focus, presence, identity and target checks before the real press.

Policy schemaVersion is 1, intervalSeconds is exactly 60, clientBuild is a string, and at most
32 exact commodity entries are accepted. All keys are required, with explicit null for an unused
starting price. Duplicate/unknown keys, floats and booleans in integer fields are rejected.
Canonical JSON determines the full SHA256 policy digest. Start requires that exact digest and
an explicit observed boot. The stored policy fixes expiry, admitted stock, price floors,
spending, session-total recycle rounds and reviewed target/route IDs.

The runner validates each persisted child against its policy-derived phase and fresh source
facts before staging and dispatch. Post attempts enforce the durable per-item 60-second interval
from the actual attempt, including known failures, reconciliation and restart. Admission remains
fresh within three seconds; coherent stock/market observations retain their original timestamps
and expire after 60 seconds. Travel phases have a bounded 300-second window, always capped by
policy expiry. Selective return candidates expire after 60 seconds.

Admitted stock is conserved across bag, managed-active, returning, unresolved and sold quantities.
Unexpected later inventory cannot increase admission. Missing/partial-sale identity remains
unresolved rather than becoming revenue. Separately proven admitted bags can still be posted;
unknown managed IDs cannot be cancelled. Actual uncertain effects interlock all new work.
Costs are reserved before effects, and actual native charges and stock changes commit atomically
with terminal receipt/reconciliation. A known failure releases only conclusively unused reservations.

Explicit resume loads the same stored policy and requires its digest and a fresh boot arm.
It reconciles observations at the measured AH or mailbox without renewing expiry, stock or money.
Interrupted routes retain their exact child/cursor history. Fresh location-bound recovery uses
one persisted resume epoch; completed/uncertain primitives are never silently replayed.
After expiry, release-expired can release only reconciled bag-only ownership from fresh AH
observations, with the original digest and fresh arm. It cannot trade or clear uncertainty.

## Native contract

Automation addon version 0.3.0 uses frame schema 3; scanner schema remains independent.
Targeted market facts use native SENT/RESPONSE barriers, matching result events, throttled
pagination and full-source bounded aggregation. Overlap, cache changes during aggregation,
incomplete hooks or inconsistent owned facts invalidate the query. Representative result IDs
are not proof of ownership or equal-price purchase priority.

Inventory/owned pages contain at most four rows; mail pages contain one message. Collections
bind a frozen request/generation, source count and original capture time. Mail frames expose
sanitized exact item/quantity and expiring opaque selection tokens, never sender or subject.
CollectReturn pins only itemKey, full quantity and token. The callback reselects exact commodity
stock after inbox reindexing, checks eligibility/capacity, and issues one TakeInboxItem call.
Native success and exact bag increase establish completion. Identical matching messages remain
collectible without inventing a permanent mail ID or interpreting wasReturned as auction origin.

## Review and verification

Luna workers owned three disjoint scopes: native facts/receipts, policy/store/runner, and
controller/backend/CLI. Independent Sol reviews required concrete regressions and fix rounds
for query provenance, actual-charge event order, reservation idempotency, late receipt handling,
cancellation continuation, post-attempt cadence and interrupted mailbox/route recovery.
All three final independent Sol reviews are READY, with no remaining Must-Fix, Should-Fix or
Nits. Native review independently passed 95 tests; policy/store review passed 157 combined
tests; controller review passed 68 tests. These scopes overlap and are not additive totals.

Final parent verification: full default suite 1,323 passed / 31 skipped. Ruff check/format passed,
Pyright reported zero errors, generated launch-target validation passed and both updated skills
validated. Final parent automation verification passed 283 tests, followed by 35 Store tests
after the last message-only edit. All 12 applied SQL migrations retain their original bytes.
Installed-package tests
build wheels/sdists, check owned resources/import directions and exercise all seller subcommand
help surfaces outside the checkout. Recovery checks include interruption after the final
collection before route preparation, independently known bags with unresolved partial-sale
stock, expired unused recovery routes in both directions, and expired bag-only ownership release.

No deployment, real game input, live trade or production database operation was performed.
Current-client protected behavior and measured current-build routes/targets remain unqualified;
no qualified profiles ship by default. Task work uses only main and leaves private historical
.archive evidence and pre-existing distribution artifacts untouched.
