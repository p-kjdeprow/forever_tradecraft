# Merchant's Favor and Alchemy unlocks

The read-only Merchant's Favor tab is at `/dashboard/merchant-favor`, alongside
Crafting. It compares the configured Alliance house, `us-normal-alliance`, for the
Azeroth Commerce Authority. Nina Surefire sells the recipes at Three Corners,
Redridge Mountains. Marcy Baker takes completed crates; Dianne Softstep sells
forged receipts once Friendly.

## Unlock targets

The current vendor catalog has **28 potion/elixir recipes costing 3,780 Favor**.
The final gate is **Honored**, or 9,000 base reputation earned from Neutral zero.
Friendly starts at 3,000; its displayed bar to Honored is another 6,000.
Alchemy Certification is a separate optional 1,000 Favor title, excluded from the
recipe target. Older lists include the removed Disorienting Smog Potion; it is
not part of the current 28-recipe total.

| Reputation | Alchemy recipe | Skill | Favor |
|---|---|---:|---:|
| Neutral | Elixir of Lesser Spirit | 65 | 45 |
| Neutral | Lesser Mageblood Elixir | 75 | 45 |
| Neutral | Elixir of Lesser Intellect | 95 | 45 |
| Neutral | Lesser Cleric's Elixir | 95 | 45 |
| Neutral | Lesser Arcane Elixir | 120 | 45 |
| Neutral | Mender's Potion | 130 | 45 |
| Neutral | Spellblasting Potion | 135 | 45 |
| Neutral | Frenzy Potion | 140 | 45 |
| Friendly | Elixir of Greater Spirit | 165 | 120 |
| Friendly | Greater Mageblood Elixir | 175 | 120 |
| Friendly | Greater Cleric's Elixir | 195 | 120 |
| Friendly | Elixir of Greater Fortitude | 200 | 120 |
| Friendly | Dragonfire Potion | 245 | 120 |
| Honored | Elixir of the Phalanx | 250 | 180 |
| Honored | Caustic Smog Potion | 265 | 180 |
| Honored | Elixir of Cunning | 275 | 180 |
| Honored | Elixir of Ferocity | 275 | 180 |
| Honored | Elixir of Sages | 275 | 180 |
| Honored | Elixir of the Whale | 275 | 180 |
| Honored | Potion of Venomous Blood | 275 | 180 |
| Honored | Elixir of the Owl | 280 | 180 |
| Honored | Major Mender's Potion | 280 | 180 |
| Honored | Potion of Elemental Purging | 280 | 180 |
| Honored | Elixir of Nature Power | 285 | 180 |
| Honored | Elixir of Wicked Regeneration | 285 | 180 |
| Honored | Major Spellblasting Potion | 285 | 180 |
| Honored | Major Frenzy Potion | 290 | 240 |
| Honored | Elixir of the Grizzly | 300 | 240 |

The source JSON pins numeric spell, recipe-item, output-item and material IDs.
Spell/recipe/output identities were checked against build 1.60.1.70959 client data;
vendor costs and gates are guide-backed, not a new live-vendor observation.

## Favor and reputation are separate

An ordinary filled crate gives Favor and coin, not recurring reputation. Pick
one complete material alternative, not every listed alternative. A forged return
instead consumes one **empty** crate plus a Forged Delivery Receipt (item 262902,
15 Favor), producing a Notarized Shipping Container (262917). Its return awards
50 base reputation and 1 silver. Forging earns no Favor.

Without writs or bonuses, Friendly to Honored requires 120 forged returns,
1,800 Favor and 120 empty crates. This is a reference calculation, not the route:
the planner can replace receipts with cheaper eligible daily writs. It never
repeats the one-time Shipping Label reward. User defaults are Neutral 2,150,
30 Favor and character level 35, saved locally with recipe ownership and completed
daily quest IDs. Actual currency, reputation and bag contents are entered manually.

## Profit-first instructions

`GET /v1/houses/us-normal-alliance/merchant-favor/route` returns a read-only plan
for the latest accepted house/build/sweep and entered progress. It joins all 28
recipe identities to current output asks and complete one-craft reagent costs.
It matches the lowest observed ask, deducts the house cut, and normalizes by output
yield. Validated vendor bundles supply reagents at consumed-unit value; buying whole
packs requires more upfront cash. The route explicitly admits Imbued Vials from
a sourced vendor listing and the build-pinned native 30,000c/five-vial bundle.
Other market views keep their existing supply assumptions. Sold-auction deposits are
refunded, so they are not a realized successful-sale expense; cancellation/expiry
losses and demand are not predicted. Own asks can establish a matched selling
price, while own/account-owned groups are excluded from purchases.

Only positive current margins enter the unowned recipe queue, in descending net
profit per produced item. Missing evidence is distinct from a negative margin.
There is **no gold budget or Alchemy-skill gate**. Character level still determines
crate eligibility; learning requirements are shown only for reference.

The page places the next recipe and **Do this scan** checklist first: exact AH
shopping quantities/costs, daily-writ customers and zones, fills, empty crates to
keep for forging, receipt spending, and the recipe purchase. It plans one next
unlock at a time, with the full profitable roadmap underneath. After completing
steps, update actual balances and mark the recipe/writs. The next accepted scan
or profile change recalculates the plan. No game actions or purchases occur.

A bounded integer optimizer jointly allocates fills, forged returns and daily
writs. All alternatives share the same retained item quantities and marginal
price tiers. A writ can be selected only once per daily reset; forging requires
Friendly, including reaching it through selected writs first. The objective first
maximizes reachable progress toward the next gate, then available recipe Favor,
then minimizes known net cost. If complete funding exists, this yields its lowest
known net cost within the qualified cohort and retained stock. Extra receipt/fill
farming is excluded. Partial stock produces an explicit partial checklist; an
optimizer timeout emits no shopping instructions. Variable writ Coin Pouch
payouts are excluded from known refunds and cost comparisons. Travel time is not
priced, so this is a monetary route rather than a fastest-travel claim.

The qualified cohort contains **11 named daily writs worth 1,025 base reputation**
per reset. Each has numeric writ/delivery/quest identities and sourced NPC/zone
stops. Required character level 10 and daily scope were checked against primary
tooltips. Reports and addon documentation support writ reputation after Friendly,
but the server reward cap has not been independently observed. Black Mageweave
Vest is excluded because of reported turn-in failures. The page can reset daily
completion manually and does not assume that a new scan resets quests.

## Scan coverage and evidence

Catalog revision **11** retains all prior IDs, adds the 11 writs, and promotes the
16 slower faction-potion outputs. It has **509 core + 344 cold IDs**, 853 total;
six cold slices produce 566 or 567 planned queries. All 28 recipe outputs, the 30
crates, 96 fill materials and the cohort's delivery goods are in every sweep.
The 900-second cadence, 750-second cap and query algorithm are unchanged.

Prices are pinned to one repeatable-read house/build/sweep. The comparison API
`GET /v1/houses/us-normal-alliance/merchant-favor` remains available separately.
Its quotes include the complete empty crate plus material bundle through retained
buyable depth, less verified cash rewards. Own/account-owned mixed groups cannot
be separated and are excluded. Variants and missing/unqueried/failed/empty books
stay explicit. The action plan requires the current canonical plan and expires
from original query timestamps after 30 minutes, including time spent optimizing
and time cached in the browser. Failed refreshes hide old purchase instructions;
profile changes clear the previous focus, totals and priorities immediately.

Artisan fills have reported Favor rewards but unverified cash payouts, so they
remain visible in comparisons and excluded from optimized net-cost fills. Native
server eligibility for every empty forging target has not been independently
qualified; no trading authorization is inferred from this page.

## Sources and qualification

Reviewed Nov 18, 2026:

- [Nina Surefire vendor catalog](https://www.wow-professions.com/forever/npc/nina-surefire).
- [Crate rewards, writs and receipts](https://www.wow-professions.com/forever/merchants-favor-durotar-supply-azeroth-commerce).
- [Receipt tooltip](https://www.wowhead.com/forever/item=262902/forged-delivery-receipt)
  and [Crate Return](https://www.wowhead.com/forever/quest=93862/crate-return).
- [Writ reputation reports](https://www.wowhead.com/forever/guide/professions/craftsmans-writs-overview-recipes-rewards)
  and [writ planner author documentation](https://www.curseforge.com/wow/addons/pugger-waylaid-companion).
- [Blizzard's auction deposit rules](https://us.media.blizzard.com/manuals/wow/wow-classic-manual-enUS.pdf).
- Every writ retains primary item/quest tooltip URLs, versioned addon-author data
  and pinned QuestieDB provenance in `merchant_writs.json`; recipe relationships
  also match the local build's client export.

Sol reviewed the source and bounded deployment helper READY. Verification covers
profit normalization, vendor bundles, refundable deposits, finite shared stock,
scarce fill/forge competition, optimal daily subsets, later writ rewards, surplus
prevention, timeouts, query freshness, current plan, progress validation and routes.
The compatible common/scanner wheels alter only two common data files and
GeneratedTarget.lua; installed input/listing code remains unchanged. The new
server image is `sha256:02ba2aac444db0a52a8266e351e61a7847cbbb947e5c6bcd991f5ad9f27c9aec`.
Forward and rollback paths verify the client-data field set and canonical hash
before resuming collection. See HANDOFF for live collection/browser results.
Private receipts and rollback wheels: `~/.local/state/crowley/favor-route-20261118/`.
