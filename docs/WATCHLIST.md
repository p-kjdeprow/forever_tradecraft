# First run scan catalog

## Revision 6 source review and live qualification (2026-11-13)

The reviewed repository source catalog is revision 6 for selected target build **1.60.1.70938**. It contains
957 entries: 790 active (421 core, 1 discovery, 368 cold), 116 research and 51 excluded. The active
plan keeps the existing 900-second cadence, 750-second sweep cap, six cold slices and 20 retained
price levels. Each sweep plans 421 core + 1 discovery item and one cold slice of 61 or 62 items,
for 483–484 planned item queries. The current source catalog hash is
`082e567cf1c41e53306118cb0ad8cfbcd61b829e1861f9f076feafe5d5292147`; the current source plan hash
is `d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`.

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

This keeps all 297 hot Alchemy/Tailoring recipe reagent/output IDs at their existing hot tier;
the full client-export graph contains 829 such IDs. All existing active IDs and price history remain.
The teaching items and 11138 stay active for periodic cold price coverage. No items are promoted
or removed. Copper, tin, iron, silver, gold and Truesilver ores, tin bars, coal, Lesser Astral
Essence and the existing Bow of Searing Arrows remain hot under their prior policies; the 115
other-profession recipe-linked items also remain hot.

Three export-3 verified Tailoring outputs are admitted to cold for price discovery: **279943
Spinning Wheel**, **279972 Faction Banner**, and **279973 Faction Banner**. Their exact item facts
come from client build 1.60.1.70888, report status `ok` and bonding code 0, and each has a fixed
Tailoring output link. The selected target pin is now 1.60.1.70938 from a measured native frame;
its matching recipe export is now loaded as export 4. This does not relabel the older admission
facts. Catalog tradeability is client-marked only; live Auction House eligibility, turnover and
profitability remain unverified. No BoP or unknown-fact item is admitted. Rich Purple Silk Shirt
(4335) and Phoenix Bindings (210781) remain cold. Bow of Searing Arrows (2825) remains hot in
discovery at its existing tier; this revision makes no gear promotion because its demand evidence
is sparse.

The approved source hashes above are deployed to the package, generated addon target, healthy server and companion
`Data.lua`; all use build 1.60.1.70938 and plan hash
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`. The first full accepted sweep was
`us-normal-alliance-1794550375-Dandune-3791` (database sweep 538), cold slice 3, on 2026-11-13. It ran from
06:12:55 to 06:23:30 UTC (635 seconds): 483 declared IDs, 483 `query.kind=queried`, 483 `reason_code=ok`, and 418
priced items. Its exact item set matched the planned core, discovery and slice-3 set. The sweep was saved, uploaded,
and its UID was written to `Data.lua` acknowledgements. Client-loaded ACK was not independently verified; no reload
was forced. Private verification receipt: `.archive/scan-rebalance-20261112/run/parent-first70938-sweep.json`.

The observed 635 seconds is one full sweep, not a p95. Separately, scaling the historical 24-sweep p95 of 652 seconds
by planned query count projects 593.3 seconds p95, 58.7 seconds lower, and about 1,148 fewer item queries over 24
sweeps. That remains an estimate; response latency, GUI waits, retries, slice ordering and failures can change runtime.
The first sweep's slice 3 contained none of 279943 Spinning Wheel, 279972 Faction Banner or 279973 Faction Banner,
so that sweep did not establish their live Auction House eligibility, turnover or profitability. Export 4 for build
70938 is now available for analysis; these admission entries retain their 1.60.1.70888 export-3 provenance.

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
sweep. `discovery` is also scanned every sweep; revision 6 has one discovery item, Bow of Searing
Arrows (2825). The recipe-teaching items moved in revision 6 are in `cold`, not `discovery`.
`cold` rotates through six slices, scanning one slice per sweep for periodic price coverage.
The revision 6 plan has 421 core items, 1 discovery item and 368 cold items, with slices of 61
or 62 items. Use category, profession and tag views to review memberships; do not maintain
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
