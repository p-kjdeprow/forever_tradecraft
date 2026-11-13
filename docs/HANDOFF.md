# Forever Tradecraft handoff

Updated 2026-11-13. Current state, rules, decisions and open follow-ups. History lives in git
(tag `pre-simplify` is the last commit with the old session stack).
Technical reference: [ARCHITECTURE.md](ARCHITECTURE.md) (including the client assumptions table).
Operator guide: [REPEATING_SCANS.md](REPEATING_SCANS.md).

## Objective

Make gold on the WoW Forever auction house with several independently profitable strategies:

1. **Collect** the market: catalog revision 6 is deployed for client build 1.60.1.70938 with 790 active IDs
   (421 core, 1 discovery and 368 cold), a 900 s cadence and 20 retained distinct unit-price levels per item;
   never mix houses or item identities (`itemKey`). Its first complete 70938 sweep was accepted, uploaded and
   acknowledged in `Data.lua`; gather more sweeps before treating one run as a runtime distribution.
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

Revision 6 has 957 entries: 790 active (421 core, 1 discovery, 368 cold), 116 research and 51 excluded. Each sweep
plans the 422 core/discovery IDs plus one of six cold slices of 61 or 62 IDs (483–484 total); cadence is 900 s,
configured cap 750 s and retained depth 20. The selected target is client build 1.60.1.70938/interface 16001,
house `us-normal-alliance`. The deployed catalog hash is
`082e567cf1c41e53306118cb0ad8cfbcd61b829e1861f9f076feafe5d5292147`; package, generated addon target, healthy
server and companion `Data.lua` agree on plan hash
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`.

The first accepted full sweep was UID `us-normal-alliance-1794550375-Dandune-3791` (database sweep 538), cold slice
3. Its actual database interval was 2026-11-13 06:12:55–06:23:30 UTC (635 s); the 483-ID set matched the exact
planned set, with 483 `query.kind=queried`, 483 `reason_code=ok` and 418 priced items. It is saved, uploaded and its
acknowledgement is written to `Data.lua`. Client-loaded ACK was not independently verified; no reload was forced.
None of 279943 Spinning Wheel, 279972 Faction Banner or 279973 Faction Banner was in slice 3, so their live Auction
House eligibility, turnover and profitability are unverified. Client export 4 for build 1.60.1.70938 was loaded on
2026-11-13 at 08:26:38 UTC, with 2,282 recipes and 4,556 item facts. Historical admission records retain their
build-1.60.1.70888 export-3 provenance. The private acceptance receipt is
`.archive/scan-rebalance-20261112/run/parent-first70938-sweep.json`.

The 635 s duration is one measured sweep, not a p95. A separate linear item-count projection from the historical
24-sweep p95 of 652 s estimates 593.3 s p95 (58.7 s lower, about 1,148 fewer queries across 24 sweeps); response
latency and GUI waits may change actual runtime. More complete sweeps are needed to characterize runtime.

- WoW Forever launched 2026-11-04. Selected target `forever-us-camwow7`, client build 1.60.1.70938,
  interface 16001, house `us-normal-alliance` ([LAUNCH_TARGET](LAUNCH_TARGET.md)). The prior source pin was
  1.60.1.70888, re-pinned 2026-11-11 after the 2026-11-10 patch from 70862.
- **Active:** `crowley-scan.service` and `crowley-companion.service`; the server is healthy and the database is up.
  The accepted sweep's ACK is written in `Data.lua`; client-side ACK load was not independently verified.
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

Current-build Advice is available at `http://127.0.0.1:8080/dashboard` on this host. Select Advice, house
`us-normal-alliance`, Alchemy and Tailoring, Crafting, Forecast and Live. The authenticated endpoint returned 200
after export 4 loaded; at sweep 546 (2026-11-13 08:22:10 UTC) it had 0 ranked and 38 unmeasured pathways.
Unmeasured profit/hour assumes P(sale)=1 and is an upper bound, not income. The current interface filters professions
but does not enforce the skill-225 planning ceiling. Default ranked crafting needs six usable hours of commodity
demand and four independent two-hour sale trials; additional data alone does not solve grouped-gear sale identity.
Matching-build research, current margins and early descriptive flow are in `.archive/advice-refresh-20261113/`.

## Open follow-ups

- **Tailoring through 225:** fifteen additional eligible output IDs lack catalog coverage, including Runecloth,
  Brightcloth, Ghostweave and Earthenweave pieces. They are unknown price-discovery opportunities, not negative
  margins; the current shortlist uses observed outputs only. The matching-build census records the gaps.

- **Scan catalog follow-up:** revision 6 has one accepted full 70938 sweep. Measure additional complete sweeps
  before characterizing runtime. The six-sweep cold rotation provides periodic price discovery only and cannot supply
  gap-free books with gaps of 21 minutes or less for P(sale). This first sweep did not query the three new Tailoring
  outputs, so their live eligibility remains unverified. Evaluating a selected eligible commodity with such evidence
  would require a separately reviewed promotion into a stable hot cohort; no promotion is included here. Grouped gear
  needs own-sale trials. Keep cadence, slice rotation and protocol unchanged unless a separately reviewed change is
  needed; a dynamic scheduler is not part of this catalog rebalance.
- **Addon trim (phase 3) deferred:** remove the movement, hover and unused executor snapshot fields from Agent
  telemetry, which needs the addon producer and the decoder schema (`gui/frames.py`) bumped together. `Queue.lua`
  and the companion's execution/probe upload and queue fetch stay by owner decision.
- **Timers:** enable `crowley-health.timer` and `crowley-market-backup.timer` again once the owner approves
  (backups were disabled during the cutover).
- **Executor driver:** nothing automates the in-game executor button or mailbox trips now. Reintroducing it
  needs a design that obeys the click rules in `CLAUDE.md` and the small-loop rule.
- **Launch target:** route provenance is `unmeasured on client build 70938`; routes are not shipped.
