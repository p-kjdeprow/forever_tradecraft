# Architecture

## Constraints that shape everything

- **No official data feed.** Blizzard's Game Data API has no Forever auction endpoint, so
  every price we have comes from our own in-game observations. Starts are 900 seconds apart
  while a character stands at an auctioneer; all 428 active watchlist IDs may not finish every sweep. Each item
  has a 30-second query budget, each sweep has a 600-second budget, and the client permits at
  most 100 throttled sends in any rolling 60 seconds.
- **Addons cannot do I/O.** The only way out is SavedVariables, written on `/reload`,
  logout or exit. The only way in is an addon file loaded at login or `/reload`. The
  companion bridges both directions; this is the same pattern as the TSM desktop app.
- **Realmless houses.** A house is `(region, ruleset, faction)`, where faction includes the
  goblin `neutral` houses (15% cut instead of 5%). The house key `us-normal-horde` is
  decided **at scan time** in game (auto-detected, overridable with `/crowley set`) and
  stored with every scan. The server never mixes data across houses.
- **Human-speed automated clicks.** Buying, posting and cancelling need a hardware event, which
  the session input driver can supply. Automated clicks are hard-capped at one per second
  (at least 1.0 s between consecutive clicks), enforced in code and not configurable.
  Every automated button-down, including right-button camera drags, is rate-gated; release-only cleanup is exempt.
- **One assumed path, no fallbacks.** Each unknown about the Forever client has one assumed
  answer, written below. If an assumption is wrong, the code fails loudly and we fix that
  one path rather than carrying alternatives.

## Launch target

`src/crowley/data/targets/<targetId>.json` plus `selected` are the strict packaged
source for client/build, interface, product/executable, locale, region/realm/house
and scan policy. `tools/generate_launch_target.py` renders coordinated Python/Lua/TOC
artifacts and staged configuration; `--check` detects drift without writing.
Session and companion startup reject stale explicit identity, path or canonical
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
| **Live observed (2026-10-22, 70450):** keyboard turning (Q/E and arrows) is π rad/s; run speed 7 yd/s; `UnitPosition` z is always 0 (jumps show only as `falling`) | `route_walk.py` | Turns overshoot; route walking must stay closed-loop |
| **Live observed:** XTEST key input reaches the game up to ~1 s after dispatch on the Wayland/Xwayland desktop, and goes to the compositor-focused window even when the X "active window" is the game | `daemon.py` route walk, operators | Settled fixes must wait ≥1.2 s; every input must be verified in telemetry |
| **Host requirement:** GNOME animations are disabled; the screenshot flash otherwise generates XI2 Leave/Enter/Motion crossing events on the fullscreen game window | `ops/doctor.py`, `GAME_SETUP.md` | `crowley-ops doctor` refuses the host; native capture is unsafe while animations are enabled |
| **Live observed (2026-11-08):** Xwayland 24.1.6 without EI delivers one RawMotion and one source-4 Motion for Crowley XTEST pointer motion, with no relative-pointer echo | `session/guardian`, takeover evidence | Forwarded relative-pointer echo is optional evidence and is never fabricated or blanket-accepted |
| **Live observed (2026-11-08):** GNOME/Mutter floats `xwayland-pointer:*`, `xwayland-relative-pointer:*`, `xwayland-pointer-gestures:*` and `xwayland-keyboard:*` while the Bluetooth seat has no pointer, then reattaches them on wake | `session/startup.py` | Durable takeover fingerprints canonicalize only those attachment fields; runtime readiness compares the raw hierarchy and revokes on every attach/float change; a sole floating relative-pointer relay supplies no echo source |
| **Live observed:** bindings JUMP=SPACE, TURNLEFT=Q, TURNRIGHT=E (arrows are secondary); there is no usable interact key for world objects | `Agent.lua` telemetry | Unbound actions fail telemetry construction |
| **Live observed:** hovering an NPC sets `UnitName("mouseover")`; hovering a world object shows a `UIParent`-owned `GameTooltip` whose first line is its name ("Mailbox"); a left click on the hovered mailbox or auctioneer opens it | `Agent.lua`, `daemon.py` world click | Hover-confirmed clicks refuse |
| **Live observed:** `MAIL_SHOW` fires on open but `MAIL_CLOSED` did not fire when the mail frame closed via Escape; `MailFrame:IsShown()` is reliable; `C_PlayerInteractionManager` exists (`MailInfo` = 17) | `Agent.lua` (`mailOpen`) | Mail state would stick open |
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
| The `-launcherlogin` client reaches character select with the last character selected within a fixed 45 s settle after fullscreen, and one Return there enters the world (no detector in the shell) | `deploy/launcher/crowley-world-of-warcraft.sh.in` | The session stays BLOCKED; the supervisor's next relaunch (30 min limit) retries |
| A server disconnect (`WOW51900308` "Unable to reconnect") needs a Battle.net relaunch; the supervisor relaunches only from an observed BLOCKED status held for 10 minutes, never PAUSED/STOPPED, never while status is unanswered, and never for blockers that name explicit reconciliation | `src/crowley/session/supervisor.py` | The outage persists until an operator acts; touching the client while BLOCKED requires `crowley-session pause` first |

`/crowley probe` saves the client's API surface for checking these. The scanner uses the
client's `hooksecurefunc` callbacks to flag an external or user search that overlaps an
active Crowley request. The Auction House result events have no request identifier, so a
foreign same-key query is conservatively recorded as incomplete rather than attributed to
Crowley; this correlation limit remains a live-client beta assumption.

## Data flow

```
WoW (Wine) ── /crowley scan ──▶ CrowleyDB.scans (SavedVariables, compressed rows)
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

## Natural interaction executors

The coordinated addon/session source uses strict [schema 6](EXECUTOR_TELEMETRY.md),
including actual executor queue/arm/readiness/progress facts and a physical-button
callback ordinal. Schema 5 is refused. The fixed CRS1 frame, CRC and 2,000-byte
compressed payload cap are unchanged; individual field limits do not override
that aggregate cap. The beta last ran an older source version; launch deployment
and native qualification remain pending.

The addon publishes a read-only256x256pixel status surface once per second.
`session/decoder.py` turns it into strict CRS1 frames with checksum, boot,
sequence and window-identity checks. Scan state, command-buffer readback and
movement feedback use this deterministic path. A larger screenshot feeds Luna
when semantic visual evidence is required. The installed assisted path measures
its auctioneer with Luna and refreshes that measurement after pointer movement.
The installed runtime targets Auctioneer Jaxon in Stormwind, verifies
native target/range/session-binding facts, dispatches CTRL-F12 through the
guarded keyboard executor, and requires fresh AH-open telemetry. Deployment and
live acceptance are tracked in [HANDOFF.md](HANDOFF.md).

The runtime separates passive startup from a persistent takeover drill.
Startup checks identity/focus, native held state, keymap, measured input hierarchy,
the registered observer and three advancing ordinary HUD frames without game
input. It mints fresh process-local input evidence for Guardian admission. A
one-time curved hover and genuine human input prove pause/release; that record
binds the stable input environment and survives ordinary reloads and controller
restarts. Typed invalidation causes distinguish telemetry loss from an actual
input-environment change. Durable intents still require reconciliation before
the driver can re-arm. Native memory and GTK diagnostics remain optional.

The accepted capture path obtains a desktop image before cropping it. The
default-off region producer under `tools/gnome-telemetry-region/` is native C,
requests only the status rectangle, and is still undergoing integration review.
Its diagnostic must use the already leased daemon through IPC, with no second
desktop owner. Original observation timestamps and absolute deadlines include
capture and identity work. Compiled isolated tests are not live timing evidence.
An offline replay measured strict Python decode at about55ms median; the prior
full-screen path measured1.67s overall. See
[the conditional runtime performance plan](../.archive/PLAN-deterministic-runtime-performance.md)
for attribution and the evidence required before a Rust migration plan.

`session/executor.py` plans deterministic pointer curves, click dwell, key holds,
camera drags and waits. `driver.py` dispatches finite XTEST command chains under
the existing Guardian admission fence; the Guardian owns process groups and the
exact held-input set. Clicks reobserve the target after movement and verify its
safe interior before pressing. Camera motion occurs while the right button is
held. Cancellation revokes the generation and releases owned inputs.

`session/input_tool.py` verifies Crowley's fixed private xdotool publication
against its vendored upstream source and executable manifest. The backend pins
the ELF bytes in a sealed Linux memfd before acquiring input ownership. Normal
dispatch, ordinary release and the independent spawned watchdog use that image;
the watchdog acknowledges FD readiness before input can be admitted. Disk
publication drift invalidates input readiness while the pinned image remains
available for releases. XI2 RawMotion is explicit metadata: shared forward-only
one-to-one pairing requires a following primary Motion at the same server time;
raw records alone cannot prove a target or endpoint. Complete raw records reach
the takeover monitor at the native delimiter even when the stream goes idle.
Window fingerprints and sealed driver versions include
the tool digest. The supported default X screen is zero. Build instructions are
in [REPEATING_SCANS.md](REPEATING_SCANS.md).

Native motion qualification requires completing and reviewing the attached-XTEST
coordinate contract. The captured slave Motion contains the prior pointer in root/event
coordinates and the endpoint in delivered valuators. Matching upstream DIX source
explains that order; the current failed seed remains unqualified. The reviewed
correction plan is [PLAN-xtest-motion-transition.md](../.archive/PLAN-xtest-motion-transition.md):
preserve both coordinates, verify an exact origin-to-endpoint transition, seal the
measured two-axis device mapping, and recheck the actual final pointer. Its source
and regression implementation is undergoing final verification; no fresh native
qualification is claimed. Human relative motion keeps its separate interpretation.

`session/motion.py` consumes decoded native position, facing, speed, combat/death
and primary bindings. The installed strict schema4 status body includes auctioneer facts; its
addon/controller transition used a staged one-shot reload. This
status-body version is distinct from CRS1 framing and saved scan formats.
Movement pulses are capped at 0.35 seconds;
waypoints are local, capped at two world units and a finite 20-second budget.
Waypoint admission supports one translation pulse whose full 2D endpoint
uncertainty fits the arrival tolerance, minimum duration, profile ceiling and
remaining travel budget. It includes speed uncertainty, the named heading
allowance, and the full position/facing drift allowed at final confirmation.
Requests outside that supported region are rejected before camera or key input;
being inside the two-unit outer cap alone does not imply reachability. The
minimum pulse creates a lower gap above the no-input arrival tolerance. At
speed7/minimum0.08, examples are roughly0.413–1.292 units straight ahead and
0.442–0.83 at the0.20-radian translation limit; exact admission is computed.
Movement calibration supports handedness+1; a reflected basis is refused before
additional role probes or sealing. Native capture-start uses the driver's clock;
settling validates each observation at receipt and the latest at decision time.
Waypoint execution requires measured camera response and a movement basis,
fresh visual route evidence and current native feedback. Interaction mode leaves
scan scheduling idle; the runtime uses passive startup and the matching
takeover drill for input admission. Camera and movement measurements remain
specific prerequisites for their respective motion actions.
The engine and interaction producers share one capture/action fence; stop
revokes without waiting for it. Calibration measures repeated forward movement
at two headings, backward and both strafe directions within the cumulative
two-unit bound. Its technical default is 0.03 seconds, independent of human
preference minimums. Each physical commitment reserves two later 1 Hz native
publications plus jitter/cleanup time. Server budgets are 30 seconds for key and
camera intents, 90 seconds for movement calibration and 20 seconds for a waypoint;
CLI timeouts add 5 seconds for response delivery.

The session config selects `pointer_profile`, `camera_profile` and `move_profile`
as `"baseline"` (also the policy when omitted) or an absolute profile-file path.
Selected files must validate and match their role. `motion-profile build/show/reset`
manage bounded summaries from explicitly supplied demonstrations; importing numbers
does not establish live human calibration. Raw demonstrations are not retained.
Profile documents use strict schema 2. Pointer/camera duration uses a bounded
intercept plus slope times `log2(1 + distance / safe_target_width)`; supplied
summaries fit these coefficients across difficulties. Curvature describes the
peak lateral deviation. MOVE summaries learn a 0.05–0.35-second pulse ceiling,
which combines with measured displacement when choosing a movement pulse.
Older profile schemas require an explicit rebuild.

Source tests and reviews do not establish native acceptance. Current live proof,
remaining calibration requirements and the paused deployment are recorded at the
top of [HANDOFF.md](HANDOFF.md).

## Installed deployment status (2026-10-15)

Game, companion, API and database run on the configured Linux host; the repository is at the
configured checkout. WoW 1.60.1 build 70374/interface 16001 runs locally through
Lutris/Wine. `Crowley` is a repo
symlink and generated `Crowley_Data` has the curated 428-active-ID, 900-second, 20-level plan.
The house is `beta-normal-alliance`; Sunshine and the fullscreen watcher are disabled. The
companion is active. Compose uses the package-owned file from
`crowley-ops compose-file` and `--env-file "$XDG_CONFIG_HOME/crowley/secrets.env"`.

The database contains six accepted sweeps, 2,568 observations and 5,726 captured price rows.
Full native verification sweep `beta-normal-alliance-1792040351-Arthur-6643` ran from
2026-10-15 04:59:11 to 05:07:15 UTC (484 seconds), queried all 428 active IDs with zero
incomplete or unqueried items, and captured 2,889 rows across 271 row-bearing IDs and
125,395 units. That includes 306 full commodity observations (1,328 rows), 23 bounded
commodity observations (460 rows), and 99 full item observations (1,101 rows), all under
`beta-normal-alliance`. Native item examples include equipment 873 (`873:l40`), recipe 2555
(`2555:l15`), and containers 4238, 4240, 4241, 4245 and 5765.

A later scheduled sweep, `beta-normal-alliance-1792041251-Arthur-0169`, ran 05:14:11–05:19:36
UTC and stored 1,599 rows. It attempted 293 items (292 successful; one active query was
interrupted by AH closure) and left 135 unqueried when the AH closed after 325 seconds. It is
the latest scan by time; the preceding 428-active-ID sweep is the full verification result.
After the second reload,
the addon reported two uploaded scans cleared and loaded plan `0712de248d42` with 428 active
IDs. A further normal reload persisted the cleared queue; the parent independently verified
zero pending scans on disk. Arthur is parked at Buckler with the AH closed. The malformed
961-row sweep remains intact in private quarantine and was never ingested. The class-aware
query correction is now verified natively. Sol approved the change; 118 addon/core tests and
the 149-pass/15-DB-skip normal suite passed. Full DB testing used a disposable
TimescaleDB container. See the [live validation record](../.archive/live-client/real-validation.md)
for query traces, exact coverage, chronology and preserved payload hashes. The 544-entry
catalog has 428 active IDs in the scan plan; this curated watchlist is not a full-market
claim. Each sweep has a 600-second cap and may leave tail items unqueried. A scan begins every
900 seconds while AH-open. SavedVariables upload after `/reload` or logout, and a subsequent
reload loads the companion acknowledgement.

## Roadmap

The [first-run item catalog](WATCHLIST.md) defines the selective-scanning universe. It is
packaged on the server and delivered as `scanPlan` through addon data. The scanner uses
targeted queries, bounded depth, plan identity and per-item coverage. Partial depth is not
ingested as legacy whole-market statistics.

1. **Beta (until Oct 21):** continue targeted real scans, verify the remaining native
   recipe/container/equipment query paths, confirm event ordering and field layout, and
   keep real scans as test fixtures. Commodity retained-price ingestion is verified.
2. **Pre-launch:** inferred sales (listings that disappear before expiring), the strategy
   engine (vendor flips, deals below market, faction-vs-neutral arbitrage, crafting
   margins), and strategy output in Data.lua.
3. **Launch week:** continuous scanning, an automated trade executor (within strategy limits
   and the one-click-per-second cap), and a dashboard.
4. **Weeks 2–4:** demand ranking, anomaly detection, price forecasting once there is
   history, alerts.
