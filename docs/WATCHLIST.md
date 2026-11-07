# First run scan catalog

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

## Revision 2 adoption note (2026-10-16)

Revision 2 pins client build **1.60.1.70450** while preserving the exact 544-item catalog,
428 active IDs, 352 core IDs, 76 discovery IDs, retained depth 20, cadence 900 seconds and
format 2. The reviewed catalog hash is
`90918db5cc912e889553b5ddd422b829f3c6cb3773ab6da56a2e79a5a8db04f3`; its plan hash is
`f1745d85c071b350ebea242add2a7b38e3f53df5bcc8a6df1f19002f995a31d4`. This is the reviewed
release identity for parent-coordinated publication; it does not claim live scanner qualification.
The original revision 1 / 70425 release artifacts and the initial 70374 evidence remain preserved.

## Initial coverage

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

`core` contains production inputs, intermediates and selected useful outputs. `discovery`
contains recipe unlocks and the two named world drops. The first-run plan includes both
pools. Lower-frequency discovery scans are a future scheduling change, not implemented
behavior. Use category, profession and tag views to review memberships; do not maintain
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
`GET /v1/scan-plan` endpoint returns the active IDs, core/discovery memberships, revision,
content hash, target client build and intended initial depth. `POST /v1/addon-data` includes
the same object as `scanPlan`. The companion already serializes it into
`Crowley_Data/Data.lua`; WoW reads that file on the next login or `/reload`.

The plan body contains exactly schema version 2, `id`, revision, catalog hash, target client
build, 900-second cadence, initial levels, sorted IDs and the two pool partitions. Its
canonical compact JSON receives a SHA-256 `planHash`; `planJSON` and `planHash` are both
returned by `GET /v1/scan-plan` and `POST /v1/addon-data`. Deploy a reviewed package and
restart the server to change the shipped plan; let the companion write Data.lua, then
`/reload`. Do not edit generated Data.lua or maintain a second Lua item list.

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

The Lua harness measured a representative full 428-item payload with one retained row per
item and one-second response cadence: 359,943 decoded JSON bytes, 16,960 raw-deflate bytes,
and 22,616 base64 characters. That run exercises retained item identity, owner and time
fields, and the real 100-message rolling throttle; it is a payload-size sample, not a
guarantee for every live sweep. More rows, links, variants, and owner names increase the
size, so verify capacity with real beta captures before relying on the six-sweep limit.

Missing observations are never interpreted as zero supply, and bounded depth is never
reported as whole-market supply or a median. Preserve item variants when querying equipment.
Targeted query sorting, pagination, throttle limits, metadata freshness and event payloads
require build-specific live evidence. The first retained build70450 sweep queried all428
items with407full/21bounded observations and2183positive-price rows; this validates that
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
