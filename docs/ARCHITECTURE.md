# Architecture

## Constraints that shape everything

- **No official data feed.** Blizzard's Game Data API has no Forever auction endpoint, so
  every price we have comes from our own in-game observations. Starts are 900 seconds apart
  while a character stands at an auctioneer; all 428 active watchlist IDs may not finish every sweep. Each item
  has a 30-second query budget, each sweep has a 750-second budget (target `sweepCapSeconds`), and the client permits at
  most 100 throttled sends in any rolling 60 seconds.
- **Addons cannot do I/O.** The only way out is SavedVariables, written on `/reload`,
  logout or exit. The only way in is an addon file loaded at login or `/reload`. The
  companion bridges both directions; this is the same pattern as the TSM desktop app.
- **Realmless houses.** A house is `(region, ruleset, faction)`, where faction includes the
  goblin `neutral` houses (15% cut instead of 5%). The house key `us-normal-horde` is
  decided **at scan time** in game (auto-detected, overridable with `/crowley set`) and
  stored with every scan. The server never mixes data across houses.
- **Human-speed automated clicks.** Buying, posting and cancelling need a hardware event, which
  the GUI layer (`crowley.gui`) can supply. Automated clicks are hard-capped at one per second
  (at least 1.0 s between consecutive clicks), enforced in code and not configurable.
  Every automated button-down is rate-gated (`gui/click_gate.py`); release-only cleanup is exempt.
- **One assumed path, no fallbacks.** Each unknown about the Forever client has one assumed
  answer, written below. If an assumption is wrong, the code fails loudly and we fix that
  one path rather than carrying alternatives.

## Launch target

`src/crowley/data/targets/<targetId>.json` plus `selected` are the strict packaged
source for client/build, interface, product/executable, locale, region/realm/house
and scan policy. `tools/generate_launch_target.py` renders coordinated Python/Lua/TOC
artifacts and staged configuration; `--check` detects drift without writing.
Scan-loop and companion startup reject stale explicit identity, path or canonical
plan values. See [LAUNCH_TARGET.md](LAUNCH_TARGET.md). The selected target is the
measured WoW Forever target; the packaged beta target is retained for fixtures and
evidence, and Forever route provenance remains unmeasured. The exporter supports
enUS. Beta routes are refused on another target until genuinely remeasured.
Cadence remains the explicit 900-second policy.

## Client facts and remaining assumptions

| Assumption | Where | If wrong |
|---|---|---|
| Mutter IdleMonitor GetIdletime counts only physical input; XTEST key and pointer events do not reset it (verified 2026-11-09) | `src/crowley/scan/presence.py` | Presence would read as idle while a human types; the policy would send keys into their session |
| The AH opener's auctioneer is the NPC named `Auctioneer Jaxon` in Stormwind City; the addon reports its name in `auctioneer.targetName` | `src/crowley/scan/config.py` | `/targetexact` finds nothing or the pin never matches; the loop reports `not_in_stormwind` or `ah_open_failed` and sends no further input |
| The addon version the canvas reports (`context.addonVersion`) equals `## Version:` in the installed `Crowley.toc` | `src/crowley/scan/config.py` | Every frame fails the `pin_mismatch` check (`addon_version`); the loop holds and reports it |
| `xprop -root _NET_ACTIVE_WINDOW` prints `# 0x<id>`; focus is the game window or one of its X children | `src/crowley/scan/observe.py` | The loop reads the game as unfocused and activates it before each key; repeated failures become `window_activation_failed` |
| Commodity rows always carry an exact `timeLeft` (verified live on builds 70374, 70450, 70862) | `analysis/books.py`, `core/volume.py` | `ValueError`; the volume route returns a 5xx and the analysis loader raises |
| `client_items.bonding` is 0 none, 1 bind-on-pickup, 2 bind-on-equip, 3 bind-on-use, and null only when the item has no client row data; only 1 is unsellable on the AH | `analysis/rank.py` | Bonding 1 is excluded as `output_bind_on_pickup`, null as `output_bonding_unknown`; any other value raises `ValueError` |
| Item-kind sale evidence is only what `core/volume.py` accepts: scopes containing a grouped (non-individual) row, changed result ids, unqueried or over-long intervals give none, and a disappearance that could be an expiry is never a sale. Live results are mostly grouped, so item-kind evidence is sparse | `analysis/item_books.py`, `analysis/rank.py` | An item-kind output with no usable interval is excluded as `output_no_observed_demand` (the detail counts usable intervals), never priced from guessed demand |
| **Live observed:** an individual item-kind row of N auctions is reissued under a NEW `resultID` whenever one of its auctions sells (12 becomes 10 with the same owner and price), and a cancel-and-repost also changes ids and often the price; a vanished id is therefore not N sales | `analysis/item_books.py` | Rows are matched by (owner, unit price): only the owner's quantity decrease is consumed, an owner appearing at a new price ends the walk, and a fully gone owner counts every unit. Scopes with any grouped row stay without evidence (grouped rows can hide an owner); own confirmed sales or trial listings are the route to calibrating them |
| Interface 16001; a plain `Crowley.toc` loads | `addon/*/*.toc` | Addon missing from the list |
| Commodities and non-equipment use native `MakeItemKey` + `SendSearchQuery`; equipment uses an explicitly zeroed comparison key + `SendSellSearchQuery` | `Scanner.lua` | Native full-sweep verification includes recipe 2555, containers 4238/4240/4241/4245/5765, and equipment 873 |
| Item-search and commodity-search events follow the observed paths; freshness barriers reject stale, unrelated and foreign results | `Scanner.lua` | Full sweep `beta-normal-alliance-1792040351-Arthur-6643` verified 428 active IDs and native recipe, container, equipment and commodity results; item identity and freshness barriers remain required because result events have no request ID |
| **Live observed:** `GetCurrentRegion()` returns 110 and `GetCurrentRegionName()` is empty. **Code behavior:** the selected launch target requires its exact native region ID and realm | `Context.lua`, `house.py` | Other native IDs fail unless explicitly overridden |
| **Live observed:** realm is `Classic Beta PvE 2`; `/crowley status` reports `beta-normal-alliance(auto)` after the correction. **Code behavior:** auto-attribution requires the selected target's exact realm and native region, then uses its declared region/ruleset | `Context.lua`, `house.py` | Avoids merging unverified beta realms |
| **Launch pending:** realm, native region and ruleset must be verified and entered in the launch target; realm-name heuristics are absent | `game_target.json`, `Context.lua` | Unverified target identity is refused |
| English zone names identify neutral houses | `Context.lua` | Use `/crowley set faction neutral` |
| Probe observed `C_AuctionHouse`, `C_EncodingUtil`, and `C_Item`; `C_RealmInfo` absent and `C_GameRules` present | `Probe.lua` | Presence only; behavior remains to verify |
| **Live observed and fixed:** empty Lua tables serialize as `[]`; the native contract accepts that only for empty collections, requires objects for nonempty keyed collections, normalizes them to dictionaries, and rejects `{}` empty / nonempty arrays | `Scanner.lua`, `scanformat.py` | The earlier 422 row/count mismatch is historical; the original scans now POST 200. No archive payload format changed |
| Native commodity `SendSearchQuery` and class-aware item search dispatch produce matching result events | `Scanner.lua` | Verified with retained commodity, recipe, container and equipment prices in full sweep `beta-normal-alliance-1792040351-Arthur-6643` |
| Full source-complete coverage at exactly N retained levels uses an unbounded cutoff and unknown witness; bounded coverage needs a strictly higher N+1 witness | `Scanner.lua` | The earlier producer defect was rejected with HTTP 422; the correction is accepted by strict validation in sweep 4 |
| Non-commodity search results group identical one-unit auctions; `buyoutAmount` is one auction's buyout and `quantity` is units available in that result | `Scanner.lua`, `scanformat.py` | Item unit prices use the per-auction buyout; grouped quantity is retained as availability |
| Exact `timeLeftSeconds` in the uint32 window `2^32-300..2^32-1` decodes as `seconds - 2^32`; format 3 exact seconds are `[-300,172800]` | `Scanner.lua`, `scanformat.py` | Values outside the understood window fail through the existing incomplete/numeric-range path |
| Crowley has no external addon dependencies; the bundled `Crowley_Data` addon supplies companion data | `Crowley.toc`, `Crowley_Data/Data.lua` | Crowley loads without optional AH UI integrations |
| **Assumption:** after `ITEM_DATA_LOAD_RESULT`, `C_Item.GetItemInfo` returns sell price, stack size, binding and class; `C_AuctionHouse.CalculateCommodityDeposit` returns integer copper for a commodity while the AH is open and nil for a non-commodity | `Reference.lua` | Per-item `invalid_native` or `deposit.kind = "error"`; never coerced |
| **Assumption (client data A1):** hotfix-cache status-3 entries are deletes | `client_data.py` | 38 base Item rows would reappear |
| **Assumption (A2):** a create-item effect quantity of 0 means 1 | `client_data.py` | Yields of 107 recipes (e.g. Transmute: Arcanite) would be wrong |
| **Assumption (A3):** green skill = floor((yellow + grey) / 2) | `client_data.py` | Skill-up planning only |
| **Assumption (A4):** `ItemSparse.BuyPrice` is for `VendorStackCount` units | `client_data.py` | Vendor reagent costs off by the bundle factor |
| **Assumption (A5):** items without an `ItemSparse` row have no price/name until the client caches them | `client_data.py` | Recorded as `no_sparse_row` |
| **Observed:** a recipe item teaches through `ItemXItemEffect` → `ItemEffect` with `TriggerType = 6`, whose `SpellID` is the recipe (12958 → 17187, 2555 → 2335) | `client_data.py` | Teaching links missing |
| **Observed:** recipe availability on Forever is **not** determinable from client data (Season of Discovery recipes ship in the shared client); only learn evidence is stored, availability needs in-game evidence | `client_data.py` | Phantom recipes; models must gate on market evidence |
| **Observed (export 2):** `SkillLineAbility.MinSkillLineRank` is 1 for tailoring, leatherworking, engineering, enchanting, cooking and first aid; the real learn requirement of an item-taught recipe is the teaching item's `required_skill`/`required_skill_rank` (spell 20855 via item 17025 needs 300, min_rank 1). Trainer requirements and teaching items without item data have no requirement in client data, so the account gate classifies them `requirement_unknown` | `trading/account_gate.py`, `trading/data.py` | Recipes would be classified learnable at skill 1 |
| **Observed:** the addon records a skill line's learned recipes only while that profession's window was open; an empty `account_recipes` does not mean the character knows nothing. Only skill lines present as keys of the capture's `raw->'recipes'` count as captured; recipes of other held professions classify `recipes_not_captured` and the `--account-character` gate fails loudly if a strategy's profession was not captured | `addon/Crowley/Account.lua`, `trading/account_gate.py` | A character would look like it knows no recipes and all crafting would silently vanish |
| **Observed (export 2):** 14 First Aid manuals (e.g. item 6454 → spell 7935) require skill line 2942 while their recipes and the captured profession use 129; client data does not map one to the other, so a teaching requirement outside the recipe's own skill lines classifies `requirement_unknown` | `trading/account_gate.py` | Those recipes would always read `needs_skill` |
| **Model contract:** recipe exact tools, tool categories and spell focus must all be explicitly supplied in immutable capabilities; empty sets mean unavailable. Exact tools are pre-held outside the tradable ledger, without inferred cost or travel | `trading/requirements.py`, `strategies/crafting.py`, `execution.py` | Unobserved station/tool access is never granted; launch capabilities need evidence |
| Executor registers only events listed in the WoW Forever source: `AUCTION_HOUSE_SHOW`, `AUCTION_HOUSE_CLOSED`, `MERCHANT_SHOW`, `MERCHANT_CLOSED`, `MERCHANT_UPDATE`, `MAIL_SHOW`, `MAIL_CLOSED`, `MAIL_INBOX_UPDATE`, `TRADE_SKILL_SHOW`, `TRADE_SKILL_CLOSE`, `COMMODITY_PRICE_UPDATED`, `COMMODITY_PURCHASE_SUCCEEDED`, `COMMODITY_PURCHASE_FAILED`, `AUCTION_HOUSE_AUCTION_CREATED`, `AUCTION_CANCELED`, `UI_ERROR_MESSAGE`, `BAG_UPDATE` and `BAG_UPDATE_DELAYED` | `Executor.lua`; `wow-ui-source@forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/{AuctionHouseDocumentation,MerchantFrameDocumentation,MailInfoDocumentation,TradeSkillUIDocumentation,ContainerDocumentation,SystemDocumentation}.lua` | Source verification does not yet prove event delivery on the launch client; qualify the pinned set at runtime before arming the executor |
| `MERCHANT_CLOSE` is not a WoW Forever event; the event catalog names `MERCHANT_CLOSED`, and the merchant implementation uses that spelling | `wow-ui-source@forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/MerchantFrameDocumentation.lua`; `wow-ui-source@forever/Interface/AddOns/Blizzard_UIPanels_Game/{Mainline,Vanilla}/MerchantFrame.lua` | The removed alias must not be registered |
| `PROFESSION_SHOW` and `PROFESSION_CLOSE` are not WoW Forever events; the profession API and frame use `TRADE_SKILL_SHOW` and `TRADE_SKILL_CLOSE` | `wow-ui-source@forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/TradeSkillUIDocumentation.lua`; `wow-ui-source@forever/Interface/AddOns/Blizzard_Professions/Blizzard_ProfessionsFrame.lua` | The removed aliases must not be registered |
| `MAIL_UPDATE` and `TRADE_SKILL_UPDATE` are not present in the Forever event catalog or FrameXML usage; only `MAIL_INBOX_UPDATE`, `MERCHANT_UPDATE` and the trade-skill show/close events are registered here | `wow-ui-source@forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/MailInfoDocumentation.lua`; `wow-ui-source@forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/TradeSkillUIDocumentation.lua`; `wow-ui-source@forever/Interface/AddOns/Blizzard_MailFrame/MailFrame.lua` | Update notifications remain unmodeled until a source-verified event is identified |
| Battle.net 2.53.8 rejects a single invocation containing repeated `--exec` arguments | `deploy/launcher/crowley-world-of-warcraft.sh.in` | Keep each Battle.net command in its own invocation |
| Battle.net requires `Client.SingleInstance` to be `"true"` for the launcher to reuse the logged-in instance; changing it while Battle.net runs is unsafe because it rewrites its config from memory | `deploy/launcher/crowley-world-of-warcraft.sh.in` | The launcher refuses to run before invoking Wine |
| `launch_uid wowf` selects the WoW Forever product but does not start the game | `deploy/launcher/crowley-world-of-warcraft.sh.in` | Product selection and Play activation remain separate |
| `focus play` selects the product page; a human-like click (`crowley-ops battle-net-play`) on the located blue Play button starts the selected game (retried up to 2 more times after 20 s) | `deploy/launcher/crowley-world-of-warcraft.sh.in` | The launcher fails if no Battle.net window appears or the owned game does not start |
| Battle.net launches `_forever_/Wow.exe -launcherlogin -uid wowf`; direct game startup cannot issue the login ticket | `deploy/launcher/crowley-world-of-warcraft.sh.in` | Direct `Wow.exe -launcherlogin -uid wowf` startup fails with `WOW51900308` |
| **Live observed 2026-11-09:** after a fresh Battle.net start the embedded Chromium view has no keyboard focus: `focus play` does not move focus and Return does nothing (Tab reaches the logo menu, which contains Log Out), so Play is clicked, not typed; the click also needs the window un-maximized, so the launcher un-maximizes it first. The real window is the visible `battle.net.exe`-class window named `Battle.net` (mutter's frame window shares the name) | `deploy/launcher/crowley-world-of-warcraft.sh.in` | The owned game never starts and the launcher fails |
| **Live observed 2026-11-09:** Wine can map the game with fixed-size `WM_NORMAL_HINTS` and `_MOTIF_WM_HINTS` limited to move/close, so mutter ignores fullscreen; the launcher clears both hints, then requests and verifies fullscreen with a display-sized child | `deploy/launcher/crowley-world-of-warcraft.sh.in` | The launcher fails on the fullscreen verification |
| **Host requirement:** GNOME animations are disabled and the session never idles to a blank screen (`idle-delay 0`) | `ops/doctor.py`, `GAME_SETUP.md` | `crowley-ops doctor` refuses the host; a screenshot flash or blank screen corrupts the captured HUD frame |
| **Live observed:** XTEST key input reaches the game up to about 1 s after dispatch, and goes to the compositor-focused window even when the X "active window" is the game | `scan/policy.py` (readback waits, `STEP_WAIT_S`), `scan/observe.py` | Typed commands are read back from a fresh frame before Return; a window that is not focused is activated first |
| A server disconnect (`WOW51900308` "Unable to reconnect") needs a Battle.net relaunch | `scan/policy.py` (row 2), launcher | The loop sees no frame for 10 min and relaunches |
| After `-launcherlogin` the client reaches character select; one Return enters the world; the addon, and so a decodable frame, exists only in the world | launcher (`crowley-scan probe-frame`) | The launcher repeats settle, probe and Return until a frame decodes, and fails after its timeout |
| `xdotool windowactivate --sync` gives the game window focus on this Xwayland session | `scan/act.py` | Three failed activations in 5 min become `window_activation_failed` |

`/crowley probe` saves the client's API surface for checking these. The scanner uses the
client's `hooksecurefunc` callbacks to flag an external or user search that overlaps an
active Crowley request. The Auction House result events have no request identifier, so a
foreign same-key query is conservatively recorded as incomplete rather than attributed to
Crowley; this correlation limit remains a live-client beta assumption.

## Data flow

```
WoW (Wine) ── timer sweep ──▶ CrowleyDB.scans (SavedVariables, compressed rows)
     │                                │ /reload or logout
     │                                ▼
     │                     crowley-companion (poll, parse, POST /v1/scans)
     │                                │                     ▲
     │  Crowley_Data/Data.lua ◀───────┘ POST /v1/addon-data │ HTTP over Tailscale
     │  (acked scan ids)                                     │
     └─ next /reload: prune acked scans ─────────────────▶ crowley-server ──▶ TimescaleDB
```

The addon keeps at most 6 unacknowledged sweeps. The companion acknowledges uploaded sweeps
through Data.lua, and the addon prunes them on the next load. At capacity it pauses and never
evicts evidence. Uploads are idempotent on the addon-generated sweep ID.

## Scan format (v2 and v3)

`CrowleyDB.scans[id]` holds an envelope with `format=3`, integer sweep bounds, house/context,
row and observation counts, and one raw-deflated/base64 JSON payload. The payload has exactly
`planHash`, canonical string `planJSON`, object-keyed `observations`, and object-keyed `owned`.
The server also accepts historical format-2 envelopes. Each planned item has a query window,
kind, coverage (`full`, `bounded`, `incomplete` or `unqueried`), source completeness, rational
cutoff/witness and retained `row:<n>` objects.
Commodity price is unit copper. A non-commodity result groups identical one-unit auctions: its
`price.copper`/`buyoutAmount` is the buyout of one auction, while `quantity` is the number of
units available in that result, so format 3 stores `unitPrice` as `{copper, 1}`. Historical
format-2 rows retain their old reduced `copper / quantity` wire value for validation and are
normalized to the corrected unit price before storage. Format-3 exact time-left values may be
negative: a client uint32 value from `2^32-300` through `2^32-1` is decoded by subtracting
`2^32`; all other exact values must be in `[-300,172800]`. Format-2 wrapped values are likewise
decoded during storage normalization. Stored observations and captured rows always use corrected
semantics; `market_sweeps.payload`, its content digest, and the raw archive keep the received
envelope/payload unchanged. Unsafe cross-products produce `numeric_range`; bid-only rows do
not become zero-price listings. Noncommodity keys preserve parsed link bonuses plus raw level,
suffix and pet identity. Successful full/bounded scopes prove every retained rational pair and
the higher witness; incomplete captures retain safe partial rows, sort them when comparisons are
safe, and leave the cutoff unknown otherwise. Unknown links/owners are tagged unknown rather
than guessed. Owned collection remains `unqueried` until its own query is sent; a house change
before completion makes that batch incomplete and prevents cross-house evidence.

### Cold tier

A catalog item with `pool: "cold"` is swept in rotation instead of every cycle. The plan body carries
`pools.cold` (ordered item IDs) and the integer `coldSlices` (N); both are part of `planJSON` and so of
`planHash`. N comes from the target's `scan.coldSlices` and must be positive exactly when the plan has cold
items, and at most the number of cold items (no empty slices). `forever-us-camwow7` is 6 since catalog revision 5 added the first
307 cold items (51 or 52 per slice). Cold item `i` of `pools.cold` belongs to slice `i % N`.

A sweep attempts every core/discovery item plus slice `k`, in plan order. `k` is a rotation counter, not a
clock function: the addon persists `CrowleyDB.coldSliceNext` (0..N-1, with `coldSliceCount` = N), a sweep uses
`k = coldSliceNext`, and `Scanner.Finish` sets it to `(k+1) % N` for completed, timed-out and aborted sweeps
alike. First use starts at 0. If the stored count differs from N or the value is out of range, the addon resets
to 0 and records it loudly (chat line, `CrowleyDB.coldSliceReset`, and a `cold_slice_reset` event in the
sweep's query trace); nothing is clamped. A saved cursor on an item outside the swept set resumes after the
last swept item at or below it.

The addon records `coldSlice = {index = k, count = N}` as an optional top-level envelope field (absent when N
is 0). `validate_envelope` requires it when N > 0, requires an explicit non-null object with `count == N` and
`0 <= index < N`, and requires the `observations` keys to equal exactly hot plus slice `index`. The attempted
set is that explicit key set, so "attempted, zero listings" (an observation with no rows) stays distinct from
"not attempted" (no key). Ingest stores `coldSlice` in `market_sweeps.context` (jsonb, no schema change).
Plans written before the cold tier (no `coldSlices`, no `pools.cold`) validate as N = 0.

In scan-loop frames `plan.count` is the whole plan; `scanner.planned` must equal hot + |slice j| for some j
(exactly `plan.count` when N = 0), idle included (the next sweep's size). `lastSaved.planned` may be any
positive count so a sweep saved under an earlier plan still decodes.

## Storage (TimescaleDB)

| Table | Contents |
|---|---|
| `houses` | one row per region/ruleset/faction |
| `scans` | one row per upload, with client context and the raw-archive path |
| `listings` | hypertable, every auction in every scan; compressed after 3 days |
| `item_stats` | hypertable, one row per item key per scan: min, p10, p25, median, market value, quantity |
| `items` | latest name, quality and level per item key |
| `client_probes` | `/crowley probe` reports |

Migration 002 adds `market_plans`, `market_sweeps`, `market_observations`,
`market_captured_rows`, `owned_observations` and deduplicated `own_sale_evidence`. Legacy
scans/listings/item_stats stay separate and are returned under `legacy_full_scan` sections;
the volume endpoint reads only the new observation tables. Public churn is an uncalibrated
proxy. A disappearing listing is never a confirmed sale, and own Sold evidence is reported
separately only after a full owned result.

Migration 003 rewrites stored format-2 item rows to the corrected per-auction unit price and
signed time-left; `market_sweeps.payload` and digests keep the received bytes. Migration 004 adds
`item_reference_captures`, `item_reference`, `item_deposits` and the `latest_item_reference`
view, filled by `/crowley reference` through `POST /v1/item-reference`; these live captures are the
source for AH deposits and a cross-check of client data. Migration 005 adds the client-data tables
(`client_data_exports`, recipes, recipe/skill-line associations, reagents, outputs, teachers, tool
requirements, `client_items`), loaded by `tools/client_data_export.py` from a private, offline,
read-only DB2 export of the local client (see the assumptions above). Client data is the single
source for recipes and static item facts; bulk game data is never committed.

`src/crowley/analysis/` holds the offline models' inputs: commodity time-to-sale and the market
metrics package (`python -m crowley.analysis.metrics`); see [ANALYSIS.md](ANALYSIS.md).
`src/crowley/forecast/` holds the walk-forward validated price and demand forecasts
(`python -m crowley.forecast validate`). `src/crowley/advisor/` ranks trading pathways from the
stored history (`crowley-advise`, `GET /v1/houses/{key}/advice`, the dashboard's Advice view) and
reads prices through either snapshot or forecast estimates; see [ANALYSIS.md](ANALYSIS.md).

`GET /v1/houses/{house}/items/{item_key}/volume?hours=24` accepts 1–168 hours and compares
only new observations whose end windows fall inside the request. It reports actual compared
seconds, common proved price scope, excluded intervals, commodity depletion/replenishment,
individual candidate units per observed hour labelled `UNCALIBRATED`, and a separate own-Sold
evidence section. Market actual-sales estimates and confidence intervals remain null.

Commodity depletion uses the single bottom-walk in `analysis/books.py` (the route and the
analysis/trading code share it). Commodity rows are grouped by persistent `result_id`; only
depletion of the cheapest groups counts (the walk stops at the first untouched or partially
bought group, and a vanished group that could have expired is skipped, not counted).
Replenishment is new groups plus quantity increases within the common price ceiling. An
interval needs a start-to-start gap in (0, 1260] s (`MAX_GAP`, the binding rule; the 1800 s
end-to-start check still applies to all kinds). A commodity row without an exact `timeLeft`
raises and fails the report rather than being skipped.

Accepted envelopes are archived as gzip JSON under the retained content-addressed
`data_dir/raw/format2/<digest-prefix>/<digest>.json.gz` so observations can be replayed without
trusting a UID path. Legacy archives remain where they were written. New depth and volume
reports never use legacy whole-market statistics.

## The scan loop

`crowley-scan run` (unit `crowley-scan.service`, code in `src/crowley/scan/`) is one process with no database,
no journal and no IPC. Every 2 s (`loop.py`) it **observes**, **decides** and **acts**. The only memory is a small
rate-limit record (`scan-loop.json`, restored on start, a few hundred bytes), the decoder epoch, the presence
reader and the active typed-command transaction. A restart takes seconds and needs no reconciliation.

### Observe (`observe.py`)

- the owned `Wow.exe` (same uid, Windows argv0 and `WINEPREFIX` rule as the launcher, `gui/process.py`);
- the game window: exactly one visible `wow.exe`-class window at display size and origin, and whether it is
  focused (`xprop -root _NET_ACTIVE_WINDOW`);
- the HUD canvas: `gnome-screenshot`, cropped to `canvas_crop` from `host.toml`, decoded by `gui/frames.py`
  (schema 6, checksum, boot and sequence). The decoder is replaced when the process or window changes and accepts
  a boot change only after our own `/reload` (armed explicitly);
- disk: scan ids in SavedVariables, the companion's uploaded and permanently rejected ids, acked ids in `Data.lua`;
- presence (below).

A frame older than 10 s is not acted on. Character, realm, faction, house, plan hash, build, interface and addon
version from the frame must equal the selected target and host config (`config.py`).

### Decide (`policy.py`, pure, first match wins)

| # | Condition | Action |
|---|---|---|
| P | A human was active in the last 5 min, or presence is unreadable | `hold_human`: no input at all |
| 1 | No owned `Wow.exe` | relaunch |
| - | Window is not one display-sized game window | fault `window_invalid`; relaunch after 30 min |
| 2 | Client lived and produced frames, now none for 10 min | relaunch |
| 3 | Fresh client has no frame within 10 min | relaunch, fault `no_frames_after_launch` |
| 4 | Pin mismatch, `plan_invalid`, or a non-ordinary construction marker | fault, no input |
| 6 | `blockingUI` set (checked before chat so no key goes over a popup) | Game Menu: Escape at most every 30 s; otherwise fault `blocking_ui`; relaunch after 30 min |
| 5 | Chat edit box active or a transaction in flight | continue or abort our own transaction; foreign text is a fault |
| 7 | `scanner.phase` is `item` or `owned` | wait |
| 8 | A saved sweep is in memory but not on disk | `/reload` (save flush) if the AH is closed, the queue is blocked, or the next timer sweep is at least 90 s away |
| 9 | Queue full, and pending ids are uploaded and acked on disk but not loaded | `/reload` (ack load), at most once per 10 min |
| 10 | Queue full and nothing loadable | fault `queue_full` |
| 11 | In world, AH closed | open the AH (below) |
| 12 | AH open, no new saved sweep for 30 min | fault `no_progress`: close and reopen once, relaunch after another 30 min |
| 13 | Otherwise | wait; the addon's 900 s timer runs the sweep |

[REPEATING_SCANS](REPEATING_SCANS.md) lists every fault code with what to do.

**Presence** (`presence.py`): `org.gnome.Mutter.IdleMonitor.GetIdletime` over `gdbus` returns milliseconds since the
last physical input. XTEST input does not reset it, so our own keys and the launcher's Play click never look like a
human, and no device topology or drill is needed. Under 5 minutes idle, or any error or unparseable reply, counts as
a human being active. `crowley-scan presence-ok` exposes the same check to the launcher.

**Opening the AH** is a bounded transaction. Preconditions: zone Stormwind City, keyboard interaction mode, the
Ctrl-F12 binding free or already `INTERACTTARGET`, the auctioneer in range. Steps: `/targetexact Auctioneer Jaxon` if
the target is absent, the conditional `SetBinding` line if Ctrl-F12 is unbound, then Ctrl-F12, with `ahOpen` required
within 10 s. At most 3 attempts per 10 min, then `ah_open_failed` and a relaunch after 30 min. Not being in Stormwind
is a fault, never a walk or relaunch.

**Command transactions.** Only three commands are ever typed (`allowed_commands`): `/reload`,
`/targetexact Auctioneer Jaxon` and the Ctrl-F12 `SetBinding` line. A transaction opens chat (Return), waits for an
empty focused edit box, types the text with exact XTEST press and release transitions (`gui/keyboard.py`), reads
`chatEdit.text` back from a frame newer than the typing, and presses Return only on an exact match with a fresh
frame, a focused box and the reload still safe; otherwise it presses Escape. After Return the transaction is never
escaped: it ends when the box closes, or becomes `submit_unconfirmed` after 60 s. Window focus and presence are
checked immediately before every key, under the GUI flock.

**Reload timing and ack folding.** The addon re-arms its timer for `dueAt`. The loop reloads only with at least 90 s of
margin, so a timer-started sweep cannot be aborted. Upload, ack load and persisted cleanup are separate boundaries:
the sweep uploads about one companion poll after the save reload, the ack loads at the next reload, and the pruned
`scans={}` reaches disk one reload after that. Steady state holds 1-2 pending sweeps against a cap of 6. Every reload
closes the AH, and row 11 reopens it.

### Act (`act.py`)

Input actions take the exclusive GUI flock (`gui.lock` in the runtime directory), re-check presence under it and send
keys through the pinned XTEST tool (`gui/input_tool.py`, built by `crowley-native build`). The loop never moves the
pointer. Window activation is `xdotool windowactivate --sync`.

**Relaunch and backoff.** The client is ended (SIGTERM, then SIGKILL) and `crowley-world-of-warcraft` runs as a
polled child, killed after 10 min. Attempts are spaced 15 min for the first 3, then 60 min, with no cap; the count
resets when a new sweep is saved. The launcher takes the same flock and presence gate around each of its own input
steps: window activation, the Battle.net Play click (`crowley-ops battle-net-play`) and Enter World.

### Faults and status

Each change of decision is logged as a JSON line to journald. `scan-status.json` (runtime directory, atomic, 0600) is
written on change and as a 60 s heartbeat: `action`, `fault_code`, `detail`, an observation summary (never chat
text or frame contents), `pid`, `started_at`, `updated_at`. `crowley-ops health` reads it (stale after 5 minutes; any
fault code is an issue) together with companion upload freshness. Operational errors (capture, X11, input tool)
become `observe_failed` or `action_failed` for that tick; any other exception ends the process and systemd restarts it.

### GUI layer (`src/crowley/gui/`)

`pointer.py` (seeded, curved, Fitts-timed pointer plans and the motion chain), `click_gate.py` (the
one-click-per-second permit: at least 1.0 s between button-downs), `input_tool.py` (pinned XTEST tool and its
publication), `keyboard.py` (keysym map, exact transitions), `capture.py` (screenshot lock, complete-PNG wait),
`process.py` (owned-process matcher), `frames.py` (frame and snapshot decode). The only automated pointer click
is the launcher's Play click.

## Roadmap

The [first-run item catalog](WATCHLIST.md) defines the scanned universe; it is packaged on the server and delivered as
`scanPlan` through addon data. The scanner uses targeted queries, bounded depth, plan identity and per-item coverage.
Open work (tiered scanning, addon trim, timers) is tracked in [HANDOFF](HANDOFF.md).
