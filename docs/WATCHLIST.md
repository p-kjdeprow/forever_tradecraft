# First run scan catalog

## Revision 8 deployed plan (two-sweep qualification complete)

Revision 8 is deployed for build **1.60.1.70938** and house `us-normal-alliance`. The catalog hash is
`3e40d87ecee894e1f5c8d1cc46620b56660bbb9d0c73ec96f10322c086669dc5`; the plan hash is
`fa2230b79abfc38a1bf05d1428cc069b4f6969845848888c6e0e3d95aa93f473`. It preserves all 973 catalog entries and
806 active IDs: 402 core, no discovery pool, and 404 cold. The approved delta moves 87 hot items to cold, adds no
exclusions, and retains 390 hot IDs in the known and uncertain-rank crafting graphs plus 12 additional unresolved
dependencies. Source commit `fb36408` introduced revision 8; the installed scanner package, generated target and
companion `Data.lua` remain on its catalog and plan hashes. The current HTTP analysis server runs source commit
`a0c533c9fd9846fef4d8bb680b510825bfff75cb` on image
`sha256:3f078b72b0f322cc1efd48c40d40ec4c7de050c145aa80e308ba4922995f5738` and serves the same revision-8 plan.

The 12 retained dependency IDs are **118, 2842, 7069, 7971, 9262, 11082, 11175, 11178, 13926, 213369, 215430,
249427**. The production eligibility check links them to recipes, but related outputs are Bind-on-Pickup or have
missing client facts; vendor-sale and disenchant value remain unresolved. They remain hot pending that evidence, not because profitability
has been established.

Cadence remains 900 seconds, the cap 750 seconds, slice count six and price depth 20. Cold slices contain
68/68/67/67/67/67 items and sweep query counts are 470/470/469/469/469/469, averaging 13.38% fewer queries than
revision 7. Cold items remain in broad market discovery about every 90 minutes.

Two consecutive full revision-8 sweeps passed under the previous HTTP server image
`sha256:cf20118a09e422a0ac86ab140ced6607b554d53816a7246461343be152a8a772`; the scan plan and collector were not
changed by the later server-only analysis update. Sweep 591 (slice 2, UID
`us-normal-alliance-1794608359-Dandune-8967`) ran 2026-11-13 22:19:19–22:29:45 UTC (626 seconds); sweep 592
(slice 3, UID `us-normal-alliance-1794609259-Dandune-7251`) ran 22:34:19–22:43:39 UTC (560 seconds). Each matched
the exact 469-item plan; all 469 were observed, queried and `ok`, including all 402 hot IDs. Both were saved and
uploaded, with acknowledgements written; 395 and 400 items had prices. Starts were 900 seconds apart. All 402 hot
item query gaps were 833–899 seconds (median 883); individual gaps are not exactly 900 seconds. The client-loaded
ACK was not independently verified, and no extra reload was forced. Priced items do not establish sales or profit.
These two runtimes are measurements, not a p95. Receipts: `.archive/scan-slimming-20261113/ops/parent-sweep-591.json`,
`parent-sweep-592.json`, and `parent-two-sweep-proof.json`.

## Historical revision 7 deployed plan (2026-11-13; two-sweep qualification complete)

The following records are revision-7 deployment and live evidence; they do not qualify revision 8.

Revision 7 was deployed before revision 8; the following package and live-run details are historical.
At that checkpoint the package, generated addon target, companion `Data.lua` and healthy server agreed on the plan.
The catalog contained 973 entries: 806 active (488 core, 1 discovery, 317 cold), 116 research and 51 excluded. The
plan keeps the existing 900-second cadence, 750-second sweep cap, six cold slices and 20
retained price levels. Each sweep plans 488 core + 1 discovery item and one cold slice of 52
or 53 items, for 541–542 item queries. Its catalog hash is
`06bfde47588c8eab8a5a51bb46891828c4e5f32f03427782549827071050ca8e`; its plan hash is
`49f26420e3f174abfade44e63a212189bef2b2e8009e785974cb10d176ec8b05`. The revision-7 server image was
`5aef916d9cde916301e924e2dc22b8bd7a31d82e31ce0581060b337f3b552e27`.

The matching export-4 census uses 2,282 recipes and 4,556 item facts. It identifies 297 eligible
fixed, client-fact tradeable Alchemy and Tailoring outputs through planning skill 225, plus 111
reagent graph IDs. All 297 outputs and 91 eligible auction-house reagent inputs are protected in
the hot graph; no eligible AH input is missing. Nineteen vendor-cost inputs remain excluded, and
Bind-on-Pickup reagent 213378 is excluded. Revision 7 promotes all 165 existing cold outputs in
this known-eligible set to core and adds
the 15 previously missing outputs from export 4. Those 15 additions have client-facts status `ok`
and bonding code 2 (Bind on Equip); successful live query coverage is verified in sweeps 586–587,
but empty price results do not establish available listings, sales or profit. Current margin was
not used as an eligibility filter. Six further Alchemy outputs
(9154, 9155, 9233, 12190, 17967 and 18294) were promoted for research coverage, and Gurubashi Mojo
Madness (19931) was added as a seventh; their exported learning rank is unknown, so none of these
seven is claimed craftable by skill 225. The five already-hot skill-unknown Alchemy outputs remain
hot. All 12 skill-unknown outputs with known tradeable facts and their AH dependencies are protected
from the capacity demotions.

The Alchemy census accounts for all 196 recipes: 99 known-eligible recipes (94 fixed tradeable
outputs hot, four without sufficient output item facts, and one Philosopher's Stone output that is
Bind-on-Pickup), 74 recipes above skill 225, and 23 recipes with unknown skill requirements. The
known-eligible Alchemy graph's 44 auction-house reagents are hot. Separately, 12 skill-unknown
Alchemy outputs with known tradeable facts, plus their dependencies, remain hot for research.

The revision-7 catalog delta contained 171 cold-to-hot promotions, 16 new entries and 120 capacity moves from
hot to cold. To keep the larger hot cohort within scan capacity, 120 hot items outside the protected output
and reagent graphs moved to cold. They remained active for periodic price discovery; this was a capacity
rebalance, not a claim that those items are permanently unprofitable. At the revision-7 checkpoint both services
were active, the revision-7 server was healthy, and the scanner was at the Auction House. Two full
revision-7 sweeps passed exact-set and all-query-success checks, were saved and uploaded, and have
independently captured acknowledgement writes. Sweeps 586 and 587 took 733 s and 626 s; they are
two measurements, not a p95 or a long-term runtime guarantee. All 489 hot IDs appeared and queried
successfully in both sweeps. Their query gaps were 793–899 s (median 882 s); sweep starts were
exactly 900 s apart. Client-loaded acknowledgement was not independently verified; no extra
reload was forced solely to verify it. Private proof receipts are listed in [HANDOFF](HANDOFF.md).

The capacity estimate replays each of 20 complete matching-build baseline sweeps (database
sweeps 564–583) against all six revision-7 cold slices. The worst replay estimate is 690.5 s
using median per-item query cost and 697.79 s under mean-cost sensitivity, below the 750 s cap.
This is a replay estimate, distinct from the two measured revision-7 sweeps above. The 733 s first
revision-7 sweep was 17 s below the configured 750 s cap; the second took 626 s. Two samples do not
establish a p95 or robust long-term runtime. Exact planned-set execution, successful queries for
all 489 hot IDs, and two-sweep cadence verification are complete; live listings, sales and profit
remain unknown where observations contain no priced items.

### Historical revision 6 deployment baseline and live qualification (2026-11-13)

The deployed revision 6 baseline was for selected target build **1.60.1.70938**. It contained
957 entries: 790 active (421 core, 1 discovery, 368 cold), 116 research and 51 excluded. The active
plan keeps the existing 900-second cadence, 750-second sweep cap, six cold slices and 20 retained
price levels. Each sweep plans 421 core + 1 discovery item and one cold slice of 61 or 62 items,
for 483–484 planned item queries. Revision 6 had catalog hash
`082e567cf1c41e53306118cb0ad8cfbcd61b829e1861f9f076feafe5d5292147` and plan hash
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`; revision 7 supersedes it.

The revision moves 57 hot recipe teaching items from discovery to cold. Their exact IDs are:

```text
2555, 3395, 3830, 4292, 4416, 5484, 5642, 5775, 6044, 6211, 6326, 6328,
6663, 6716, 7976, 9295, 9298, 9304, 10644, 12958, 13477, 13486, 13487,
13488, 13489, 13490, 13491, 13494, 13495, 13496, 13497, 13499, 13519,
14468, 14499, 14510, 16111, 16113, 21099, 21219, 22307, 22308, 250380,
250382, 250977, 250982, 250983, 250989, 252783, 252784, 252811, 252812,
252942, 253665, 253668, 255723, 255725
```

Separately, Small Glowing Shard (11138) moves from core to cold.

This retained the revision-6 hot Alchemy/Tailoring IDs at their prior tiers before the matching
export-4 skill-225 eligibility census. All active IDs and price history remained. The teaching
items and 11138 stayed active for periodic cold price coverage. No items were promoted or
removed in revision 6. Copper, tin, iron, silver, gold and Truesilver ores, tin bars, coal, Lesser Astral
Essence and the existing Bow of Searing Arrows remained hot under their prior revision-6 policies;
the 115 other-profession recipe-linked items also remained hot in revision 6.

Three export-3 verified Tailoring outputs were admitted to cold for price discovery in revision 6: **279943
Spinning Wheel**, **279972 Faction Banner**, and **279973 Faction Banner**. Their exact item facts
come from client build 1.60.1.70888, report status `ok` and bonding code 0, and each has a fixed
Tailoring output link. The selected target pin is now 1.60.1.70938 from a measured native frame;
its matching recipe export is now loaded as export 4. This does not relabel the older admission
facts. Catalog tradeability is client-marked only; live Auction House eligibility, turnover and
profitability remain unverified. No BoP or unknown-fact item is admitted. In revision 6 Rich
Purple Silk Shirt (4335) and Phoenix Bindings (210781) were cold; revision 7 promotes both.
Bow of Searing Arrows (2825) remains hot in discovery at its existing tier.

During revision 6, its approved hashes were deployed to the package, generated addon target, healthy server and companion
`Data.lua`; all used build 1.60.1.70938 and plan hash
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`. The first full accepted sweep was
`us-normal-alliance-1794550375-Dandune-3791` (database sweep 538), cold slice 3, on 2026-11-13. It ran from
06:12:55 to 06:23:30 UTC (635 seconds): 483 declared IDs, 483 `query.kind=queried`, 483 `reason_code=ok`, and 418
priced items. Its exact item set matched the planned core, discovery and slice-3 set. The sweep was saved, uploaded,
and its UID was written to `Data.lua` acknowledgements. Client-loaded ACK was not independently verified; no reload
was forced. Private verification receipt: `.archive/scan-rebalance-20261112/run/parent-first70938-sweep.json`.

The observed 635 seconds was one full sweep, not a p95. At the initial revision-6 review, scaling a historical
24-sweep p95 of 652 seconds projected 593.3 seconds p95; that estimate was superseded for revision-7 planning by
the 20-sweep replay above. Response latency, GUI waits, retries, slice ordering and failures can still change runtime.
The first revision-6 sweep's slice 3 contained none of 279943 Spinning Wheel, 279972 Faction Banner or 279973 Faction
Banner, so that sweep did not establish their live Auction House eligibility, turnover or profitability. Export 4 for
build 70938 is now available; the three revision-6 utility admissions retain their original
1.60.1.70888 export-3 provenance, while revision-7 promotion decisions use matching export-4
evidence.

The user reports Alchemy skill 225 and Tailoring skill 150. Evaluate both professions through skill 225, including
Tailoring leveling targets above the current 150. Assume all recipes are known and ignore recipe-acquisition costs.
A 10 g starting budget is unconfirmed. Dandune is the scanner character on a
different account; no trading-character, recipe or inventory capture establishes recipe ownership or stock.


Crowley's first selective scan will use the item catalog in
[`src/crowley/data/first_run.json`](../src/crowley/data/first_run.json). Every item has one
entry keyed by its numeric item ID, with category, profession memberships, roles, inclusion
reason, evidence, binding information and source URLs. Names are for review and display.
Membership in multiple professions never causes duplicate queries.

The catalog and server delivery produce an immutable format-2 plan. The Lua scanner parses
the delivered `planJSON` with the client JSON API and queries only active IDs. Its plan hash
travels unchanged through every sweep, so delayed uploads remain valid after a later catalog
revision. `initial_price_levels = 20` is the retained unit-price depth; a query that proves a
higher witness is explicitly `bounded`, never a whole-market statistic.

## Historical revision 2 adoption note (2026-10-16)

Revision 2 pins client build **1.60.1.70450** while preserving the exact 544-item catalog,
428 active IDs, 352 core IDs, 76 discovery IDs, retained depth 20, cadence 900 seconds and
format 2. The reviewed catalog hash is
`90918db5cc912e889553b5ddd422b829f3c6cb3773ab6da56a2e79a5a8db04f3`; its plan hash is
`f1745d85c071b350ebea242add2a7b38e3f53df5bcc8a6df1f19002f995a31d4`. This is the reviewed
release identity for parent-coordinated publication; it does not claim live scanner qualification.
The original revision 1 / 70425 release artifacts and the initial 70374 evidence remain preserved.

## Initial coverage (historical revision 1)

Revision 1 contains 544 distinct item IDs: 428 active, 74 under research and 42 excluded.
The active set has 352 core items and 76 discovery items. Categories are disjoint; profession
memberships overlap.

| Active category | Items |
|---|---:|
| Trade goods | 196 |
| Crafting components | 46 |
| Consumables | 67 |
| Containers | 16 |
| Armor and weapon enhancements | 25 |
| Recipe unlocks | 74 |
| Equipment | 4 |

The active set spans all eight crafting professions plus Mining, Herbalism, Skinning and
Fishing. It includes raw ores and bars, stones, cloth and bolts, leather and hides, scales,
gems, pearls, elementals, enchanting materials, herbs, fish, meat, dyes, oils, bags, food,
potions, bandages, armor kits, weapon enhancements, ammunition and engineering components.

The equipment entries are the two selected leveling wands, Staff of Jordan and Bow of
Searing Arrows. The named world drops are observations for discovery; their prices,
availability and turnover are unverified. The catalog does not admit every random-suffix
armor piece, crafted leveling item or world drop.

## Status and pool

- `active`: include in the exported scan plan. This authorizes observation, not a purchase
  or a claim of profitability. Active entries currently have client-data evidence, with
  live auction eligibility still to verify.
- `research`: preserve an exact item identity and evidence while withholding it from
  scans. Most of these are equipment outputs from leveling routes without a demonstrated
  market use.
- `excluded`: preserve the reason an item should not be queried. Known bind-on-pickup
  items are rejected from active admission. Vendor supplies explicitly identified by the
  reference routes are also excluded from this first auction scan; their costs belong in
  recipe costing. This does not establish that all vendor stock is unlimited or that
  vendor arbitrage is impossible.

`core` contains the production inputs, intermediates and selected useful outputs scanned every
sweep. `discovery` is also scanned every sweep; revision 7 has one discovery item, Bow of Searing
Arrows (2825). `cold` rotates through six slices, scanning one slice per sweep for periodic price
coverage. The revision 7 plan has 488 core items, 1 discovery item and 317 cold items, with
slices of 52 or 53 items. Use category, profession and tag views to review memberships; do not maintain
separate hand-edited copies of the item list.

Scan watchlists describe what to observe; they are not a generic market-inventory list.

## Evidence and admission

The initial evidence is dated October 14, 2026 and records client build **1.60.1.70374**.
Source URLs are attached to each entry. The starting set combines the reference site's
280 selected-route candidates, explicitly selected staple families, additional specialty
materials and world drops, and ingredient/unlock links for the selected production chains.

[ForeverWowCraft's published plans](https://foreverwowcraft.com/data/classicbetapve--horde/manifest.json)
are candidate discovery evidence, not a complete recipe graph. Exact item IDs, ingredient
links and binding flags were checked against
[Bind on Forever's client-data pages](https://bindonforever.com/professions). Its profession
pages distinguish client-file recipe presence from actual learnability. Drop locations
shown by that site are not live Forever drop verification.

Only identity, selection metadata and evidence are stored here. Full recipe quantities,
yields, cooldowns, skill gates and acquisition requirements need a separate recipe catalog;
do not infer them from aggregated leveling-route purchases. This first catalog is a
starting observation universe, not proof that a craft is available or profitable.

To admit an item, identify a useful output, a material dependency, a repeated consumer,
owned inventory or a named catalyst. Record its exact ID and sources, check binding, and
explain the inclusion. Put speculative equipment under research. If an ID is unresolved,
finish the identity research before creating an entry; never guess or match names fuzzily.
Retire items by changing status and retaining their evidence. Do not delete price history.

## Inspect and edit

```sh
uv run crowley-watchlist summary
uv run crowley-watchlist items --profession blacksmithing
uv run crowley-watchlist items --category trade_goods
uv run crowley-watchlist items --status research
uv run crowley-watchlist items --status excluded
uv run crowley-watchlist export
```

`summary` validates the catalog before showing counts. `items` prints tab-separated rows;
`export` prints the active plan as JSON. Filters do not change the canonical list.

Edit the JSON, merge all memberships into the existing item entry, keep entries sorted by
`item_id`, increment `revision`, and update `reviewed_on` and `client_build` when appropriate.
Run the commands above and the repository checks. Unknown fields, duplicate IDs, invalid
values and active known-nontradeable items fail validation. For a draft file:

```sh
uv run crowley-watchlist --catalog /path/to/draft.json summary
```

The revision identifies a reviewed release. An automatically calculated SHA-256 hash
identifies its exact content, including metadata, so changes are detectable even if someone
forgets the revision bump. Commits and pushes still require the user's instruction.

## Delivery to the addon

The JSON is packaged with Python and the server validates it at startup. The authenticated
`GET /v1/scan-plan` endpoint returns the active IDs, core/discovery/cold memberships, revision,
content hash, target client build and intended initial depth. `POST /v1/addon-data` includes
the same object as `scanPlan`. The companion serializes it into `Crowley_Data/Data.lua`. For a
plan change, follow the safe stop/close/install/relaunch sequence in
[REPEATING_SCANS](REPEATING_SCANS.md); do not treat an in-place `/reload` as a cutover procedure.

The plan body contains schema version 2, `id`, revision, catalog hash, target client build,
900-second cadence, configured `coldSlices`, initial levels, sorted IDs, and the `core`,
`discovery`, and `cold` pool partitions. Its
canonical compact JSON receives a SHA-256 `planHash`; `planJSON` and `planHash` are both
returned by `GET /v1/scan-plan` and `POST /v1/addon-data`. Change the shipped plan only through
the reviewed rollout procedure in [REPEATING_SCANS](REPEATING_SCANS.md). Do not edit generated
Data.lua or maintain a second Lua item list.

An observation has one object entry per planned ID. It records query bounds, full/bounded/
incomplete/unqueried coverage, source completeness, rational cutoff and higher-price witness,
and retained rows. Commodity rows use unit copper and aggregate price buckets. Item rows use
whole-auction copper divided by quantity as a reduced rational; links and raw level/suffix/pet
identity are retained. Bid-only rows are outside buyout coverage. A missing link, stale event,
unsafe numeric comparison, failed query or cap produces incomplete evidence.

The addon starts sweeps 900 seconds apart while the AH is open, rotates after the last
attempted ID, and records an unqueried tail when a cap, logout or AH close interrupts work.
Six unacknowledged sweeps are retained without eviction; collection pauses until the
companion uploads them and a subsequent reload loads acknowledgements. The first reload
writes SavedVariables and uploads; a second reload is needed to prune and resume.

A historical Lua harness run measured a representative 428-item payload with one retained row
per item and one-second response cadence: 359,943 decoded JSON bytes, 16,960 raw-deflate bytes,
and 22,616 base64 characters. That run exercises retained item identity, owner and time
fields, and the real 100-message rolling throttle; it is a payload-size sample, not a
guarantee for every live sweep. More rows, links, variants, and owner names increase the
size, so verify capacity with real beta captures before relying on the six-sweep limit.

Missing observations are never interpreted as zero supply, and bounded depth is never
reported as whole-market supply or a median. Preserve item variants when querying equipment.
Targeted query sorting, pagination, throttle limits, metadata freshness and event payloads
require build-specific live evidence. A historical retained beta build70450 sweep queried 428
items with 407 full, 21 bounded observations and 2,183 positive-price rows; this validates that
run, rather than every possible item variant or future client build.

## Recipe knowledge

Recipe facts are maintained separately in
[`recipe_foundation.json`](../src/crowley/data/recipe_foundation.json), with strict
validation and coverage reporting in
[`recipes.py`](../src/crowley/core/recipes.py). Its packaged source manifest binds
known fields to reviewed evidence; raw snapshot checks run separately during review.
Loading the installed package requires no network or archive directory.

Coverage includes the eight crafting professions plus Mining. The initial Mining
and Cooking records remain blocked: output quantities, server learnability and
other requirements still need evidence. This is a recipe-data foundation, not a
complete recipe graph or a producer of crafting margins. The
544-item watchlist remains the independent scan catalog.
