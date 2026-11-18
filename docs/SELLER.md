# Continuous seller

The seller posts selected commodity stock in small batches and checks each item every 60 seconds
while at the auction house. It always matches price; there is no undercut option. Scanner and market
history are not inputs to its execution decisions. Read [AUTOMATION](AUTOMATION.md) for the underlying
input, receipt, stop and uncertainty contracts.

## Policy and start

Begin at the auction house with it open, the chosen stock in bags, and the matching installed
automation configuration. Mailbox travel requires reviewed current-build target/route profiles.

Prepare a private policy JSON file with exact item identities and quantities, batch sizes, minimum
unit prices, an explicit starting price for an empty market if desired, listing duration and cost
limits. The policy also binds house, character, native build, expiry, maximum recycling rounds and
measured AH/mailbox targets/routes. Inspect the entire policy and its digest before authorizing it.

Policy JSON has exactly these keys; optional starting prices are represented by an explicit null:

| Fields | Values |
|---|---|
| schemaVersion, intervalSeconds | 1, 60 |
| house, character, clientBuild | Exact house key, character name and native build string (for example "70938") |
| expiresAt | Explicit integer Unix expiry |
| items | Selected commodity entries, at most 32 |
| maxTotalSpendCopper, maxRecycleRounds | Session spending and recycling limits |
| ahTargetId, mailboxTargetId | Reviewed content-derived target profile IDs |
| outboundRouteId, returnRouteId | Reviewed route IDs for AH to mailbox and back |

Each item has exactly kind="commodity", itemKey, quantity, batchSize, minUnitPriceCopper,
startUnitPriceCopper, maxDepositCopper and maxCancelCostCopper. `durationHours` is optional
and defaults to **2 hours**; explicit overrides are 8 or 24 hours. Canonical inspection
materializes the duration before computing the authorization digest. Quantity is the
total admitted stock, batchSize is 1..200, and prices/caps are integer copper. A null startUnitPriceCopper leaves empty-market pricing unavailable. Unknown/duplicate
keys, floating-point numbers, booleans in integer fields and unsupported item kinds fail validation.

`batchSize` is per exact itemKey, never a single global stack size. For example,
one item can post 2 units at a time, another 5 and another 20. Every new posting is
`min(batchSize, admitted bag quantity)`, so a final remainder can be smaller. Total
`quantity` separately caps the stock available to the session. Sizes are explicit
user policy; the agent does not infer demand or silently enlarge a batch. The
one-post-per-item-per-minute interval and stock/spending caps still apply. Changing
a size changes the immutable policy digest; existing grants are not rewritten.

The public interface is:

```sh
crowley-automation seller inspect policy.json
crowley-automation seller preview policy.json
crowley-automation observe
crowley-automation seller start policy.json \
  --confirm-policy-id FULL_POLICY_SHA256 --arm EXACT_OBSERVED_BOOT
crowley-automation seller status SESSION_ID
crowley-automation seller revoke SESSION_ID
crowley-automation seller start --resume SESSION_ID \
  --confirm-policy-id FULL_POLICY_SHA256 --arm EXACT_OBSERVED_BOOT
crowley-automation seller release-expired SESSION_ID \
  --confirm-policy-id FULL_POLICY_SHA256 --arm EXACT_OBSERVED_BOOT
```

Inspect validates/canonicalizes without game input. Preview obtains fresh observations and reports
decisions without protected posting, cancellation, collection or movement. Its bounded preparatory
queries still require focus, physical idleness and GUI/AH ownership. Start requires authorization
for this exact policy and an explicit observed boot. It runs in the foreground and reports the
session ID. Use status/revoke from another terminal. The global `crowley-automation stop` also
applies. Production execution uses the same canonical private ledger as manual operation and host
maintenance; alternate files cannot create an independent execution lane.

The controller generates immutable, persisted child batches within the approved policy. The runner
checks their policy derivation, freshness, stock and spending reservations before dispatch. A policy
does not renew itself, change price floors or admit unexpected later inventory. Expiry, stop,
takeover, boot/identity changes and uncertain effects block further input. An interrupted session
does not restart automatically or regain its original stock/spending allowance. Explicit resume
loads the stored policy, requires its full digest and a fresh matching boot arm, and reconciles
remaining inventory/owned facts. It preserves session identity, expiry, stock and spending history.

After expiry, release-expired can release ownership of conclusively reconciled bag-only stock at
the measured AH location. It requires the original policy digest, a fresh current-boot arm and
fresh inventory/owned observations. It cannot post, cancel, collect, move, renew the policy or
clear an uncertain effect. Outstanding auctions, returns or unresolved stock prevent release.

## Posting and recycling

With 200 admitted units and a batch size of 20, the first post is 20. Keep the remaining stock in
bags. Wait while our managed listing is strictly cheaper than competitors. If a competitor is cheaper
or shares our price, match that price and post the next 20 at the next scheduled check. The runner
enforces at most one post attempt per item per 60 seconds, including across restart. Replenish sold leading stock too.
Below-floor prices pause the item. The final partial batch uses its actual remaining quantity.

An empty bag alone does not trigger cancellation. When another batch is needed and bags are empty,
cancel only active managed auctions of that exact item. Do not cancel another item, variant,
character's listings or unrelated manual auctions. Collect matching commodity attachments up to the outstanding cancelled quantity, return to the
AH, refresh the market and resume from the observed returned stock. Cancellation, mail arrival and
bag receipt are separate facts; delayed or ambiguous returns remain outstanding. A missing listing
is not recorded as a sale.

Items have independent progress and share one serial execution loop. A mailbox trip suspends market
monitoring; there is no claim to maintain priority while away. The status distinguishes fresh
observations from overdue work. Native throttling, scanner query work and desktop contention may
delay the 60-second target. There are no catch-up query bursts.

## Qualification and receipts

The client does not expose reliable purchase rank among equal-price commodity listings. The user
chose one fresh matched-price batch per item per minute while same-price competition is present.
This is scheduled replenishment; it does not claim observed first-to-sell rank. An unknown competing
price remains a pause. The seller never subtracts a copper or creates catch-up posts.

Native created-auction receipts identify managed listings. Partial-sale auction identity changes
require conclusive continuity before cancellation; uncertainty does not authorize cancelling every
matching item. Mail collection uses exact commodity identity and the outstanding cancellation pool,
not an auction-to-mail link. Matching units are fungible under this approved policy. An expiring,
one-use native attachment token, exact full quantity, non-COD/non-GM checks and fresh callback
validation protect each take. Identical matching messages can be collected after reindexing.
An oversized attachment, unrelated item, money or other attachment remains untouched. Collection
never replenishes the original stock cap or spending allowance.

Source implementation and harness tests do not qualify these live client facts. Check the installed
version and reported liveQualification before operating; no qualified route/target profiles are
shipped by default. Use only specifically authorized current-build trials for live qualification.
Read per-primitive receipts: posted is not sold, cancellation is not collection, and dispatch is not
verified completion. Do not edit the ledger or reload the client to evade an uncertainty interlock.

Implementation design and review history are in [SELLER_PLAN](SELLER_PLAN.md) and the completion
record is in [SELLER_IMPLEMENTATION](SELLER_IMPLEMENTATION.md).
