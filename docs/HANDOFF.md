# Forever Tradecraft handoff

Updated 2026-11-13. Current state, rules, decisions and open follow-ups. History lives in git
(tag `pre-simplify` is the last commit with the old session stack).
Technical reference: [ARCHITECTURE.md](ARCHITECTURE.md) (including the client assumptions table).
Operator guide: [REPEATING_SCANS.md](REPEATING_SCANS.md).

## Objective

Make gold on the WoW Forever auction house with several independently profitable strategies:

1. **Collect** the market: revision 7 is the last deployed runtime plan for client build 1.60.1.70938 with 806 active IDs
   (488 core, 1 discovery and 317 cold), a 900 s cadence and 20 retained distinct unit-price levels per item;
   never mix houses or item identities (`itemKey`). Two full revision-7 sweeps now pass exact-set, successful-query,
   upload/ack-write and two-sweep cadence checks. Their 733 s and 626 s runtimes are two measurements, not a p95 or
   robust long-term guarantee. Twenty complete revision-6 sweeps (IDs 564–583) remain the broader historical runtime
   baseline. Revision 8 is prepared in source with all 806 active IDs (402 core, 0 discovery, 404 cold); it is
   pending deployment and has no live qualification yet.
2. **Model** it: demand, time-to-sale, price metrics, recipe economics from the client's own data.
3. **Decide** with the trading engine (backtest, paper trading, queues).
4. **Execute** through the guarded in-game addon executor (dry run unless doubly armed). No program drives the
   executor today; see follow-ups.

## Rules

- `CLAUDE.md` rules apply (no fallbacks, `itemKey` only, new migrations only, at most one automated click per
  second, full simulated human pointer motion, ask before committing/pushing/destroying data).
- **Real gold:** nothing is armed without the user's explicit authorization for that specific queue
  ([executor contract](EXECUTOR_CONTRACT.md), [runbook](LIVE_TRADING_TEST.md)).
- **One GUI owner at a time.** `crowley-scan` and the launcher share a flock; neither sends input while a human
  was active in the last 5 minutes. Do not touch the game while a hold is not what you intend.
- **Privacy:** raw frames, receipts, SavedVariables and backups stay private under `~/.local/state/crowley`
  (dirs 0700, files 0600). Never edit game SavedVariables. Secrets live only in
  `$XDG_CONFIG_HOME/crowley/secrets.env`. Stage explicit files only; never `git add .`.
- Origin: https://github.com/p-kjdeprow/forever_tradecraft.

## Host state snapshot (2026-11-13)

### Scan catalog and runtime qualification

**Revision 8 source candidate; deployment pending.** The source catalog, selected target and generated addon target
now match candidate catalog hash `3e40d87ecee894e1f5c8d1cc46620b56660bbb9d0c73ec96f10322c086669dc5` and plan hash
`fa2230b79abfc38a1bf05d1428cc069b4f6969845848888c6e0e3d95aa93f473`. It preserves all 973 catalog entries and
806 active IDs. The exact approved delta moves 87 hot IDs to cold, with 402 core, no discovery and 404 cold; no new
exclusions are made. The 390 protected crafting IDs and 12 unresolved eligible dependencies remain hot. Six cold
slices contain 68/68/67/67/67/67 items and plan 470/470/469/469/469/469 queries per sweep, 13.38% fewer on average
than revision 7. Cadence, cap, slice count and depth remain unchanged. Local source checks passed; the candidate has
not been deployed or runtime-qualified. The revision-7 receipts below document the last deployed runtime only.

Revision 6 is the historical deployed baseline. It had 957 catalog entries and 790 active IDs (421 core, 1 discovery,
368 cold), with a 900 s cadence, 750 s configured cap, six cold slices and retained depth 20. Its hashes were catalog
`082e567cf1c41e53306118cb0ad8cfbcd61b829e1861f9f076feafe5d5292147` and plan
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`.

The first accepted revision-6 sweep was UID `us-normal-alliance-1794550375-Dandune-3791` (database sweep 538), cold
slice 3. Its database interval was 2026-11-13 06:12:55–06:23:30 UTC (635 s); the exact 483-ID set matched the plan,
with 483 `query.kind=queried`, 483 `reason_code=ok` and 418 priced items. It was saved and uploaded, and its UID was
written to `Data.lua` acknowledgements; client-loaded ACK was not independently verified and no reload was forced.
None of 279943 Spinning Wheel, 279972 Faction Banner or 279973 Faction Banner was in slice 3, so live eligibility,
turnover and profitability for those items remain unverified. Private historical receipt:
`.archive/scan-rebalance-20261112/run/parent-first70938-sweep.json`.

Revision 7 is deployed. Package, generated addon target, server and companion `Data.lua` agree on revision 7, catalog
hash `06bfde47588c8eab8a5a51bb46891828c4e5f32f03427782549827071050ca8e` and plan hash
`49f26420e3f174abfade44e63a212189bef2b2e8009e785974cb10d176ec8b05` for build 1.60.1.70938/interface 16001 and
house `us-normal-alliance`. The active plan has 806 IDs (488 core, 1 discovery and 317 cold), 489 hot IDs, 541–542
planned queries per 900 s sweep, a 750 s cap, six slices and depth 20. The current server image is
`5aef916d9cde916301e924e2dc22b8bd7a31d82e31ce0581060b337f3b552e27`.

The export-4 census uses 2,282 recipes and 4,556 item facts. Its skill-225 set covers 297 eligible fixed,
client-fact tradeable Alchemy/Tailoring outputs and 111 reagent graph IDs. All 297 outputs and 91 eligible AH inputs
are protected hot; 19 vendor-cost inputs remain excluded, and BoP reagent 213378 is excluded. The Alchemy census
accounts for all 196 recipes: 99 known-eligible recipes (94 fixed tradeable outputs hot, four without sufficient
output item facts, and one Philosopher's Stone output that is Bind-on-Pickup), 74 above skill 225, and 23 with
unknown skill requirements. The 44 AH reagents belong to the known-eligible Alchemy graph. Separately, 12
unknown-skill Alchemy outputs with known tradeable facts and their dependencies remain hot for research. The revision
delta has 171 cold-to-hot promotions, 16 additions and 120 capacity
moves from hot to cold; moved IDs remain active. Seven unknown-rank outputs are research coverage, not claims of
craftability by skill 225. The old 635 s revision-6 sweep and 20-sweep replay are historical evidence; the replay
estimates 690.5 s at median query cost and 697.79 s under mean-cost sensitivity. Revision-7 sweeps 586 (slice 3, UID
`us-normal-alliance-1794603735-Dandune-7353`) and 587 (slice 4, UID
`us-normal-alliance-1794604635-Dandune-9842`) ran 2026-11-13 21:02:15–21:14:28 UTC (733 s) and
21:17:15–21:27:41 UTC (626 s), respectively. Each had the exact full 542-item set for build 70938/interface 16001,
house `us-normal-alliance` and revision-7 plan hash; all 542 items were observed, queried and `ok`, with 451 and 442
priced items. All 187 new hot IDs were queried successfully in both. Both sweeps were saved and uploaded, with
acknowledgement writes independently captured; client-loaded ACK was not independently verified, and no extra
reload was forced solely to verify it. Sweep starts were exactly 900 s apart. All 489 hot IDs appeared in both full successful sweeps; their query
gaps were 793–899 s (median 882 s), within the 21-minute evidence limit, not exactly 900 s for every item. These two
runtime samples do not establish a p95 or robust long-term performance. Empty price results do not establish
available listings, sales or profit.

Private proof receipts: `.archive/tailoring-hotfix-20261113/ops/parent-sweep-586.json`,
`parent-sweep-587.json`, `parent-two-sweep-proof.json` and `parent-all-hot-cadence-proof.json`.

At the current checkpoint, both services are active. The scanner has Stormwind/Dandune at the Auction House. The two
full sampled sweeps passed planned-set, successful-query, upload/ack-write and cadence checks. Current successful
query coverage is verified; live listings, sales and profit remain unverified for unpriced or unlisted items.

- WoW Forever launched 2026-11-04. Selected target `forever-us-camwow7`, client build 1.60.1.70938,
  interface 16001, house `us-normal-alliance` ([LAUNCH_TARGET](LAUNCH_TARGET.md)). The prior source pin was
  1.60.1.70888, re-pinned 2026-11-11 after the 2026-11-10 patch from 70862.
- **Active:** `crowley-scan.service` and `crowley-companion.service`; the revision-7 server is healthy and the database
  is up. Stormwind/Dandune is at the Auction House. Sweeps 586 and 587 are saved and uploaded, with acknowledgement
  writes captured; client-loaded ACK was not independently verified.
- **Disabled:** `crowley-health.timer`, `crowley-market-backup.timer`, `crowley-paper-trading.timer`.
- The old beta data (house `beta-normal-alliance`) is retained in the database and under
  `~/.local/state/crowley/market-backups`.
- Not exercised live: the executor and queue pipeline, account capture and reference capture
  ([LIVE_TRADING_TEST](LIVE_TRADING_TEST.md)). Route walking and the mail loop were deleted with the session stack.

## Components

| Component | Code | Docs |
|---|---|---|
| Addon (scanner, telemetry HUD schema 6, account capture, executor, reference) | `addon/Crowley`, `addon/Crowley_Data` | [ARCHITECTURE](ARCHITECTURE.md), [EXECUTOR_CONTRACT](EXECUTOR_CONTRACT.md) |
| Scan loop (observe, decide, act every 2 s) | `src/crowley/scan` | [REPEATING_SCANS](REPEATING_SCANS.md) |
| GUI primitives (pointer, click gate, XTEST tool, keyboard, capture, frames, process matcher) | `src/crowley/gui` | [ARCHITECTURE](ARCHITECTURE.md) |
| Launcher (Battle.net login, human Play click, fullscreen, Enter World) | `deploy/launcher/crowley-world-of-warcraft.sh.in`, `src/crowley/ops/battle_net.py` | [GAME_SETUP](GAME_SETUP.md) |
| Companion (SavedVariables to server, Queue.lua) | `src/crowley/companion` | [ARCHITECTURE](ARCHITECTURE.md) |
| Server + TimescaleDB | `src/crowley/server`, package-owned Compose resource | [MARKET_RECOVERY](MARKET_RECOVERY.md) |
| Analysis and client data | `src/crowley/analysis`, `src/crowley/core/client_data.py`, `tools/client_data*` | [ANALYSIS](ANALYSIS.md) |
| Trading engine, paper trading, queues | `src/crowley/trading`, `src/crowley/core/queue.py` | [TRADING](TRADING.md), [LIVE_TRADING_TEST](LIVE_TRADING_TEST.md) |
| Ops (install, doctor, health, backup and restore drill) | `src/crowley/ops` | [GAME_SETUP](GAME_SETUP.md) |
| Live configuration | `deploy/config/` (linked from `~/.config/crowley`) | [deploy/config/README](../deploy/config/README.md) |

## Decision: simplification, 2026-11-09

The `crowley.session` stack (daemon, controller, driver, journal, guardian, calibration, supervisor, takeover
drill, mail loop, route walking, native construction; about 45k lines plus 75k of tests) was replaced by one small
loop, `crowley-scan`. Why:

- **Outages came from the recovery machinery, not from scanning.** Restart-uncertain intents blocked every
  reconcile, deadline overruns under load blocked the controller, and the supervisor never resumed
  non-allowlisted blockers and ran out of relaunch budget.
- **Restarts cost about 13 minutes and 5 GB.** Journal replay re-parsed about 1.1 GB of archive manifests.
- **Operator drills gated autonomy.** The takeover drill was bound to Xwayland device names and needed a
  forwarded pointer; a reboot invalidated it.
- **Launch was fragile.** Battle.net keyboard focus assumptions failed; the launcher now clicks Play.

What replaced it: stateless decisions from fresh observations each tick, presence from Mutter's IdleMonitor
(no drill), a bounded relaunch with backoff, and a status file. Rollback means reinstalling the `pre-simplify` tag.

## How scanning works now

The addon sweeps the AH on its own 900 s timer while the AH is open and saves each sweep into SavedVariables.
The companion uploads sweeps after SavedVariables are written (on `/reload`) and writes acknowledgements into
`Crowley_Data/Data.lua`; the next `/reload` loads them and the addon prunes. `crowley-scan` runs next to the game
and every 2 s observes the owned `Wow.exe`, its window, the addon's HUD frame (screenshot, cropped and decoded)
and the files on disk, then picks one action from a fixed policy table: relaunch the client, type `/reload` to flush a saved
sweep or load acks, open the AH with `/targetexact Auctioneer Jaxon` and Ctrl-F12, or wait. Anything unexpected
(wrong identity, popup, foreign chat text, queue full) is a reported fault with no input. A human at the keyboard
holds all input until 5 minutes after their last physical input, then it resumes by itself. Details:
[ARCHITECTURE](ARCHITECTURE.md) and [REPEATING_SCANS](REPEATING_SCANS.md).

## Operations quick reference

```sh
systemctl --user status crowley-scan          # the unit; journalctl --user -u crowley-scan -f for the log
crowley-scan status                           # scan-status.json: action, fault_code, observation summary
crowley-scan presence-ok                      # exit 0 when no human input in 5 min, 1 otherwise
crowley-scan probe-frame                      # capture and decode once: 0 frame, 1 verified none, 3 error
crowley-ops health                            # freshness and fault check (timer is currently disabled)
crowley-ops doctor                            # installation and host checks
crowley-ops install                           # render and copy units, launcher, addon, config
systemctl --user restart crowley-scan         # safe: restart takes seconds and leaves the game running
```

Backups: `systemctl --user start crowley-market-backup.service`; restore drills via `crowley-ops drill <snapshot>`.
Tests: `uv run pytest` (add `CROWLEY_TEST_DATABASE_URL` for the DB tests, see [TESTING](TESTING.md)).

## Strategy findings (beta, 20 g start, demand share 0.5; see [TRADING](TRADING.md))

Crafting dominated (alchemy and blacksmithing, then engineering and leatherworking); Coarse Grinding Stone alone
was about a quarter of profit. Scalping lost money in model, and 58 % of listed units expired. A character has only
two primary professions, so the portfolio must be cut to two; provisional replays favor alchemy plus engineering.
This is beta evidence, not a forecast for the launch economy. Station, tool, category and focus requirements are
enforced in strategy and execution; empty capabilities mean unavailable.

The user reports Alchemy 225 and Tailoring 150. Evaluate both professions through skill 225, including Tailoring
targets that require leveling from 150; the planning ceiling does not change the reported current skill. Assume all
recipes are owned and ignore recipe-acquisition costs. The 10 g starting budget is unconfirmed. Dandune is the scanner character
on a different account; no trading-character, recipe or inventory capture establishes skills, recipe ownership or stock.

The trading dashboard is deployed at `/dashboard` on the healthy loopback server. Open
`http://127.0.0.1:8080/dashboard` on this host. For laptop access, forward the loopback listener with
`ssh -N -L 18080:127.0.0.1:8080 <user>@<host>` and open `http://127.0.0.1:18080/dashboard` locally. Its
latest-scan and trading-summary GETs are token-free and read-only; write endpoints still require bearer
authentication. Keep the server on its existing loopback binding. The page defaults to house `us-normal-alliance`
and shows scan status before strategy results. The deployed strict observed-turnover gate (server source commit
`58a8206`, image `5aef916d9cde916301e924e2dc22b8bd7a31d82e31ce0581060b337f3b552e27`) excludes UNKNOWN-turnover
and zero-observed-activity rows. Candidate recipes are selected dynamically; there is no five-recipe whitelist.
Qualification requires 70% valid interval coverage in both 2-hour and 6-hour windows; the 6-hour threshold means
4.2 hours of valid coverage, not a guaranteed ETA. The page reports observed listing depletion, not confirmed sales.
Strategy rows show batch cost, if-sold net proceeds and profit, list price, observed activity, reagents and access
requirements. Shared cooldown rows are alternatives, not a combined portfolio forecast. The page links to the prior
research UI at `/dashboard/advanced`.

**Recorded live check, 2026-11-13 21:29:47 UTC.** The strict-gate summary returned HTTP 200 and zero qualified
opportunities. This is an evidence-coverage result, not evidence of zero market supply, sales or profit. Proof:
`.archive/tailoring-hotfix-20261113/ops/parent-final-live-summary.json`.

**Historical dashboard HTTP verification, 2026-11-13 18:33 UTC.** This predates revision 7 and the strict turnover
gate. It used deployed commit `a15d388` (image
`e81b402cab34ef5e10bc8277877310463a7f6a7beba17f76a7f437a3d7ec8d1a`). The unauthenticated read-only summary
returned HTTP 200 with ten strategies for house `us-normal-alliance`, build `1.60.1.70938`, latest accepted sweep
583 (17:47:03 UTC): 484 planned, 484 queried, 484 successful; response time was 8.03 s. Profile, budget and
cost/proceeds/profit arithmetic were checked. Both `/dashboard` and `/dashboard/advanced` returned HTTP 200; a
protected queue write without credentials returned 401. The server remained loopback-only; scan and companion
services were active. These strategy rows are historical and do not describe current strict-gate recommendations.
Potion of Predatory Senses (2 crafts / 2 units) showed 3,238 copper cost, 10,885 copper net
proceeds if all outputs sell, 7,647 copper if-sold profit, and 12.57 observed listing-depletion units/hour.
Shadow Oil (4 crafts / 8 units) showed 8,237 copper cost, 12,478 copper net proceeds, 4,241 copper if-sold
profit, and 49.17 observed units/hour. Listing depletion is not confirmed sales; sale probability stays unknown.
The assumed 10 g budget, recipe ownership, and excluded acquisition/leveling costs are called out in the page.
Source evidence remains in `.archive/dashboard-trading-20261113/live-source-summary.json` and
`live-source-check.log`.

## Open follow-ups

- **Tailoring through 225:** deployed revision 7 covers all 297 eligible fixed, tradeable outputs from export 4,
including the 15 previously missing Runecloth, Brightcloth, Ghostweave and Earthenweave pieces. All 165 eligible
cold outputs move to core; the 91 auction-house reagent inputs are already hot. Seven additional skill-unknown
Alchemy outputs are included for research coverage, not asserted craftable by skill 225; five such outputs were
already hot. The 120 capacity demotions exclude the complete set of 12 known-fact skill-unknown outputs and their
AH dependencies. The new items are unmeasured opportunities, not negative margins; live auction eligibility and
sale activity remain unverified.

- **Scan catalog follow-up:** two full revision-7 sweeps verify exact planned sets, successful queries for all 489
hot IDs (including all 187 new hot IDs), upload/ack-write receipts and the 900 s sweep-start cadence. Continue
monitoring later sweeps to measure runtime variation; these two samples are not a p95. The six-sweep cold rotation
provides periodic price discovery only and cannot supply gap-free books with gaps of 21 minutes or less for P(sale).
Empty price results do not establish available supply or sales. Grouped gear needs own-sale trials. Keep cadence,
slice rotation and protocol unchanged; a dynamic scheduler is not part of this catalog rebalance.
- **Addon trim (phase 3) deferred:** remove the movement, hover and unused executor snapshot fields from Agent
  telemetry, which needs the addon producer and the decoder schema (`gui/frames.py`) bumped together. `Queue.lua`
  and the companion's execution/probe upload and queue fetch stay by owner decision.
- **Timers:** enable `crowley-health.timer` and `crowley-market-backup.timer` again once the owner approves
  (backups were disabled during the cutover).
- **Executor driver:** nothing automates the in-game executor button or mailbox trips now. Reintroducing it
  needs a design that obeys the click rules in `CLAUDE.md` and the small-loop rule.
- **Launch target:** route provenance is `unmeasured on client build 70938`; routes are not shipped.
