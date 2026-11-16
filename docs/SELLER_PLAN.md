# Continuous seller plan

Updated 2026-11-15. Planning and independent Sol review only; this document does not implement,
deploy or authorize trades. The source refactor is complete; see [HANDOFF](HANDOFF.md) and
[AUTOMATION](AUTOMATION.md) for the existing bounded execution interface.

## Confirmed behavior

The operator loads a character with chosen stock. The seller posts small batches, checks the
selected items every 60 seconds while at the AH, and posts another batch when a competitor
displaces our eligible listing or our eligible stock sells out. It recycles unsold listings
through cancellation and mailbox collection when another batch is needed and bag stock is empty.

The user's explicit decisions override earlier alternatives:

- Always match the current competing price. Never subtract a copper or other increment; there
  is no undercut mode, flag or fallback.
- Recycling cancels only the seller-managed, currently active auctions for the exact item in
  question. Other items, variants, houses, characters and unrelated manual auctions are excluded.
- Use the existing posting/cancellation/mail/movement primitives. Keep scanner and automation
  independent. Buying, crafting, vendors and a remote queue remain out of scope.

A batch is a chosen sale quantity, not necessarily one physical bag stack. Native posting and
purchase semantics must establish what the client actually sells; do not invent a stack entity.
The first release supports commodity batches; non-commodity variants follow only when their
price, quantity and purchase-priority comparisons have equivalent verified contracts.

## Concrete example and timing

With 200 admitted units and batch size 20, initially post 20. At each due observation, wait if
our remaining listing has purchase priority. If a competitor displaces it, match the current
best competing unit price and post another 20, subject to the configured minimum price and budget.
If our leading quantity sells out and bag stock remains, replenish even without an undercut.

Posting the final 20 from bags does not itself trigger cancellation. Continue monitoring. Only
when another batch is required and no admitted bag stock remains do we recycle this item's
remaining unsold managed auctions. Reconcile cancellations, visit the mailbox, collect the
observed returns, return to the AH, refresh the market and restart with the returned quantity.
A final remainder smaller than the batch size is posted at its actual quantity.

If no admitted stock or active/returning managed stock remains, finish for that item. An auction
vanishing is not proof of a sale; retain unresolved quantities until conclusive evidence resolves
them. A cancelled quantity is never counted as sold or returned before the corresponding proof.

The 60-second interval is a target observation cadence, not a guarantee of uninterrupted priority.
Use monotonic scheduling, record each item's last successful observation and lateness, and avoid
catch-up bursts. Queries/actions are serial, throttle-aware and bounded. While away collecting
mail, monitoring is explicitly suspended; requery before posting on return. Do not advertise
minute checks for a sell list too large to observe under native query limits.

## Selling rules and required native evidence

"Lowest price" and "first to sell at that price" are separate facts. Before live execution,
qualify the selected build's equal-price priority behavior and the evidence available to detect
competitor displacement, including same-price arrivals, partial sales and self listings.
A matching post must be shown to restore priority for the supported item kind. If priority cannot
be established from native evidence, expose it as unknown and pause that item; do not substitute
price subtraction, guessed row ordering or endless equal-price reposting. This is an acceptance
gate, not a configurable assumption. Do not claim priority lasts after the observation.

A fresh targeted result carries exact house/item identity, request provenance, completion/coverage,
price levels, available ownership/priority facts, native boot/sequence and capture time. Exclude
our own managed listings from competitor detection and identify other auctions of the same
character so they cannot be mistaken for competitors. Repeated reads of unchanged results must
not trigger more posts. After a confirmed post, refresh and establish its priority before accepting
another displacement. A genuinely fresh displacement can trigger at most one new batch per
scheduled item check; action failure does not generate a fresh batch for the same decision.

Match the best currently buyable competing price exactly in integer copper. Do not match stale
cached/dashboard prices or non-buyout bids. A competitor lowering price may lower our next match,
but a match below the item's approved floor pauses that item. Never round a fractional comparison
into an unapproved price; non-commodity comparisons require compatible sale units and exact totals.
When no competing offer exists, retain a known valid managed price or use an explicit operator
starting price. Without either, pause for a price; do not infer one from names or inventory value.
Revalidate the price/decision freshness before dispatch; an expired decision requires a new query.

## Minimal architecture

All seller implementation belongs in packages/automation/src/crowley_automation. Add a small
seller model/decision module and one concrete controller, with the targeted native observation
adapter alongside the existing addon/backend. Keep pure decisions separate from input. Extend
the current private SQLite ledger for sessions, item progress and managed auction provenance;
do not create a second database, service, workflow framework, plugin system or command bus.

The seller directly invokes the existing runtime with immutable child batches and records their
causal results. It does not shell out repeatedly with generated confirmations. The market server,
historical scanner, catalog and dashboard are not runtime dependencies. A targeted live search
is part of selling, not an invocation/import of the scanner. Shared byte contracts stay in common.

Use the existing shared GUI lease, runner lock and native AH ownership bridge. Each query/action
obtains quiescent AH ownership before touching native search state and records its provenance.
Market-result events lack request IDs: reject ambiguous/foreign overlapping work, incomplete
results and stale caches. Release known-safe grants on idle boundaries; do not leave native staged
state or a batch-bound grant dangling across independent child batches. Unknown effects keep the
existing durable interlock and cannot release/resume behind uncertainty.

The controller holds the existing GUI lease while its foreground session is active, including
waits and mailbox travel, so host maintenance cannot type/reload/relaunch between its batches.
Idle waits remain interruptible. Scanner scheduling is separate: release AH ownership during
idle periods, suspend/quiesce again before queries, and report overdue checks if collection is
busy. Do not promise the 60-second cadence when a scanner query cannot safely be interrupted.
Host needs only the existing maintenance guard/GUI lease contract; no seller-specific host mode.

## Session configuration and authorization

A session fixes character, house, admitted exact item identities/quantities, per-item batch sizes,
minimum unit prices, optional empty-market starting prices, listing duration, spending limits,
expiry and reviewed AH/mailbox route/target profiles. The interval is 60 seconds and pricing is
always match; neither an undercut option nor unrelated trading options are added. Inventory
preview lets the operator choose stock; do not automatically admit everything carried or later
received. Positive quantities and copper use exact integers; the existing primitive limit of
200 units bounds each post. Current verified bag locations determine executable quantity;
fragmented stock needs supported posting behavior, not an assumed cross-slot native operation.

Existing execution requires exact batch approval plus a short current-boot arm. An unattended
controller cannot keep inventing approvals or silently refreshing that arm. Add an explicit,
bounded selling-session authorization path that approves this immutable policy and stock up front.
Keep the existing exact-batch path unchanged. Each derived child batch stores the session/policy
digest, fresh decision evidence, exact actions, deadline and budget reservation before dispatch.
The runner enforces both policy derivation and the ordinary input/effect gates; a controller cannot
bypass validation by passing a session ID. This requires a reviewed runner change, not a CLI wrapper.

Bind the session authorization to the observed client boot and explicit expiry. Stop/revoke,
identity/build changes, expiry or unresolvable effects block new input; no automatic renewal or
restart after client reload. Resuming interrupted work requires reconciliation and a fresh explicit
session arm. Do not weaken existing manual run/dry-run authorization or global uncertainty checks.

Reserve listing deposits and cancellation charges atomically before effects; reconcile actual costs
from native evidence, including nonrefundable relisting losses. Bound total spend and recycling
rounds for the session, so continuous equal-price competition cannot consume unlimited deposits.
Repeating returned stock does not replenish spending limits or increase admitted quantity.
Show actual charges separately from reserved/unknown amounts; unknown charges remain reserved.

## Auction ownership, recycling and mail

Persist exact created auction IDs and item identity/quantities from causal native posting evidence.
The current boolean receipt is insufficient for this association: add structured result data through
the Lua publication, strict common decoder, Python receipt and ledger. Do not guess created IDs
by diffing uncorrelated listings. Native auction IDs can change after partial sales: qualify a
reliable continuity rule; uncertain association blocks cancellation rather than sweeping by item name.
Pre-existing manual listings are observed but are not silently adopted into session ownership.

When recycling item K, freeze new posts for K and refresh its owned auctions. Snapshot exact active
managed IDs for K only. Recheck identity, active status, remaining quantity, cancel permission and
cost before each cancel. Sales or expirations during the sequence change the remaining work;
a missing row is reconciled, never blindly retried. Recycle progress persists per item and per
primitive; a crash after dispatch retains the current uncertain-effect behavior.

At the mailbox, collect only eligible auction returns admitted to this item's recycle pass.
The existing collect_mail(messageLimit) can collect general non-COD mail; extend it with exact
bounded eligibility/readback instead of using it unchanged for selective recycling. Establish the
native evidence needed to identify returned exact variants, quantities and stable message/attachment
identity across inbox reindexing. Item ID alone is insufficient for variant matching. Unknown return
identity blocks collection; never collect unrelated mail or pay COD to recover inventory.

Cancellation completion, mail arrival and bag receipt are separate observations. Track expected
remaining returns, partial arrivals, full bags, mailbox incompleteness and a bounded arrival deadline.
Collect/reconcile incrementally; reserve enough bag capacity for observed attachments. Missing or
delayed mail pauses the recycle pass with its outstanding quantities recorded. Do not loop travel
indefinitely or infer a return because an auction disappeared. Only newly verified admitted stock
may be reposted, after returning to the AH and refreshing current price and priority.

Different items keep independent progress. One item's exhaustion never cancels another item's
auctions. The single controller may combine mailbox travel for items already awaiting returns,
without forcing unrelated items into cancellation. There are no concurrent GUI/movement workers.

## Delivery loops and acceptance gates

Use bounded Luna implementation and independent read-only Sol review loops. Parent owns contracts,
integration and git. Assign disjoint files only after the preceding shared contract is stable.
Each loop must reach READY, pass its meaningful regressions and parent verification before merging.
This plan does not start those implementation loops or authorize live deployment/trades.

1. **Native facts and exact result contracts.** Own the automation addon, common frame decoder,
   backend/receipt plumbing and their contract tests. Add targeted read-only market observations,
   causal created-auction identity and selective return facts. Verify modeled throttle/freshness,
   self/competitor/variant discrimination, equal-price displacement, partial-sale continuity and
   bounded frame size. Separately qualify actual selected-build ordering and protected primitives
   under specific live trial authorization; unsupported evidence is a gate, never a mock-only claim.
2. **Pure policy, session authorization and durable progress.** Own new seller model/decisions,
   ledger/runner admission extensions, CLI policy preview and focused tests. Implement always-match,
   per-item recycling, inventory caps, budget reservation and immutable policy-derived child batches.
   Simulated decisions must cover the example, equal/lower-price competition, own/manual listings,
   repeated snapshots, sold leading stock, final remainder, empty-market pricing and floor refusal.
   Verify manual execution remains unchanged, policy tampering/overruns fail, and restart cannot
   duplicate dispatch, reset budgets, admit unrelated stock or authorize itself.
3. **One-item controller and recycling.** Own the concrete seller loop, runtime integration, CLI,
   user guide and end-to-end tests. Begin observation-only; report would-post/would-recycle without
   protected calls. Then qualify a small exact session with measured AH/mailbox travel. Test the
   full post/wait/displace/repost/recycle/return cycle, sale during cancel, delayed/partial returns,
   full bags, moving mailbox indices, price changes during travel, stop/expiry/takeover and crash
   boundaries. Every protected effect must have the existing causal receipt and durable marker.
4. **Multiple items and packaged operation.** Extend the same controller with serial fair scheduling
   and shared trips for independently ready items. Prove item A recycling never cancels B/variant A,
   show overdue checks instead of stale decisions, and test scanner/host contention between batches.
   Add installed CLI/resource checks and update AUTOMATION/HANDOFF plus the automation skill.
   Do not add dashboard controls, selling recommendations or separate background infrastructure.

For each code loop run its focused pytest tests with -n0, scoped Ruff check/format and full Pyright.
At integration run `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`,
`uv run pyright`, and `uv run python tools/generate_launch_target.py --check`. Build/install artifact
checks remain in tests/packages. New seller local-ledger migrations are tested on disposable local
files; no market schema migration or deployed database is required. Live trials have their own
concrete approved policy/batch, current boot and receipts and are not supplied by passing tests.

## Review record

Independent read-only review completed on 2026-11-15 by gpt-6.1-sol at xhigh reasoning.
Must-Fix: none. Should-Fix: none. Nits: none. STATUS: READY.

Review covered user scope, always-match pricing, exact per-item cancellation, selective mail,
native evidence limits, session authorization, stock/budget/crash behavior, package independence,
existing GUI/AH ownership contracts, delivery order and acceptance gates. No revisions were
requested. Equal-price priority, partial-sale continuity and return identity remain explicitly
unqualified implementation/live-evidence gates, not assumed native capabilities. The reviewer
changed no files and performed no tests, service operations or game input. Parent verified local
documentation links and whitespace; no application tests are needed for this planning-only change.
