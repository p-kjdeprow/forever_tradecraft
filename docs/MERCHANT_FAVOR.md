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

Friendly to Honored therefore needs 120 forged returns and 1,800 Favor before
bonuses, plus 120 empty crates, returning 1g20s. All recipes plus that reputation
step cost 5,580 Favor. At the user's reported **Neutral 2,150**, **about 30 Favor**,
**level 35**, the dashboard starts with an **850 reputation pre-Friendly gap** and
a **5,550 Favor deficit** for all unowned recipes plus subsequent receipts.
These are editable estimates, saved locally; ownership checkboxes remove recipes
from the goal. The page does not read character reputation, currency or bag stock.

Before Friendly, use Craftsman's Writs. Their base rewards are 75/125/200 for
Journeyman/Expert/Artisan; each exact order is daily, not the entire tier. For
example, seven distinct Expert writs yield 875 reputation. Writ acquisition,
required goods, delivery travel and eligibility are not priced by this crate tab;
it explicitly marks that step unpriced and forged returns locked. The one-time
Shipping Label quest gives 2,000 reputation and 50 Favor; existing progress must
not receive that reward a second time. No racial, Legacy or event bonus is assumed.

## Price comparisons

Catalog revision 10 admits 30 named empty crates and 96 distinct fill materials,
all in the regular core pool. Existing scan IDs are preserved. The plan has 482
core IDs plus 360 cold IDs across six slices: 542 planned items per full sweep.
The 900-second cadence, 750-second cap and native query algorithm are unchanged.

`GET /v1/houses/us-normal-alliance/merchant-favor` pins a repeatable-read snapshot
to one latest house/build/sweep. It reads only those 126 items. A next-turn-in quote
includes the full empty-crate price and full material bundle through retained
buyable price depth, less known coin returned. Own/account-owned groups are
excluded; mixed groups cannot reveal a separately buyable quantity. Nontrivial
variants are not silently merged. Missing, unqueried, failed, stale and genuinely
empty observations remain distinct. Prices expire after 30 minutes.

Forgeries are ranked by fresh empty-crate cost. Their effective cost per rep also
includes acquiring the spent Favor at the cheapest available complete fill rate:
`(crate gold + 15 × gold per Favor − 100 copper) / 50`.

Capacity is counted across all retained prices and is not a promise that every
turn-in costs the first quoted rate. Alternatives share stock and cannot be added
together. Bulk recipe/receipt budgets explicitly extrapolate next-turn-in prices;
they are not stock-reserved shopping lists or proofs of a globally cheapest bulk
purchase. No auction purchase, filling, forging, spending or travel is performed.

Artisan Favor rewards of 20/40 are addon-author reports, while their cash rewards
remain unverified in current guides. They are scanned and displayed with all fill
options but do not determine the verified net-cost ranking. No public evidence
independently qualifies every server-side forging target or level restriction;
the page states that limit. Level 35 meets every listed crate filling gate.

## Sources and qualification

Reviewed Nov 18, 2026:

- [Nina Surefire's current vendor catalog](https://www.wow-professions.com/forever/npc/nina-surefire).
- [Crate quantities, rewards, writs and receipts](https://www.wow-professions.com/forever/merchants-favor-durotar-supply-azeroth-commerce).
- [Receipt tooltip](https://www.wowhead.com/forever/item=262902/forged-delivery-receipt)
  and [Crate Return quest](https://www.wowhead.com/forever/quest=93862/crate-return).
- [Reported Artisan correction](https://www.curseforge.com/wow/addons/waylaid-auction-helper/files/9580959).
- Each crate and recipe row retains its numeric Wowhead source URL; material IDs
  and spell/recipe/output relationships also match the local build's client export.

Sol's source review is READY after fixing owned-listing exclusion. Focused checks
cover exact depth pricing, stock insufficiency, ownership, variants, stale/failed
queries, house/build scope, progression arithmetic, target generation and routes.
The offline headless browser checks all table counts, initial targets, local
ownership/progress updates, persistence, and 390px layout without screenshots.
Live qualification accepted sweeps 1005 and 1006: both queried all 542 items
successfully, including every one of the 126 watched IDs. Both snapshots had
126 priced books. Durations were 745 and 633 seconds, with starts exactly 900
seconds apart and watched query gaps of 791–899 seconds as the second scan warmed.
These two samples establish the observed coverage/cadence, not a runtime p95.
The live browser also passed table counts, profile/ownership persistence, invalid
input restoration, finite-stock warnings, both dashboard tabs and mobile layout,
without errors or screenshots. The website retained its database and raw mounts;
doctor passes. The compatible common/scanner wheels change only the catalog,
selected target descriptor and generated scanner plan. Input/listing code remains
on the previously installed qualified baseline. See the handoff for the image and
initial market observations; prices themselves continue to change with new scans.
Private wheels, rollback artifacts and receipts:
`~/.local/state/crowley/merchant-favor-20261118/`.
