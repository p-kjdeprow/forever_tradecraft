# Forever Tradecraft handoff

Updated 2026-11-09. Current state, rules, decisions and open follow-ups. History lives in git
(tag `pre-simplify` is the last commit with the old session stack).
Technical reference: [ARCHITECTURE.md](ARCHITECTURE.md) (including the client assumptions table).
Operator guide: [REPEATING_SCANS.md](REPEATING_SCANS.md).

## Objective

Make gold on the WoW Forever auction house with several independently profitable strategies:

1. **Collect** the market: a 428-item scan plan every 900 s, cheapest 20 distinct unit-price levels per item,
   never mixing houses or item identities (`itemKey`).
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

## Current state (2026-11-09)

- WoW Forever launched 2026-11-04. Selected target `forever-us-camwow7`, client build 1.60.1.70862, interface
  16001, house `us-normal-alliance` ([LAUNCH_TARGET](LAUNCH_TARGET.md)).
- **Running:** `crowley-scan.service` (the only game-facing process) and `crowley-companion.service`, both
  enabled. Docker `crowley-server-1` and `crowley-db-1` up; migrations 001-012 ship in the package.
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

## Open follow-ups

- **Tiered scanning** (proposed by the analysis session): a hot tier of about 350 items every sweep plus 1/N of a
  rotating cold tier, so recipe outputs can be priced inside the 600 s sweep cap (the flat plan is limited to about
  430 items at 1.0-1.31 s each). It needs an addon plan and server validation change, after the loop is stable.
  A gap-free cadence comes first: P(sale) evidence only counts books with gaps of 21 minutes or less.
- **Addon trim (phase 3) deferred:** remove the movement, hover and unused executor snapshot fields from Agent
  telemetry, which needs the addon producer and the decoder schema (`gui/frames.py`) bumped together. `Queue.lua`
  and the companion's execution/probe upload and queue fetch stay by owner decision.
- **Timers:** enable `crowley-health.timer` and `crowley-market-backup.timer` again once the owner approves
  (backups were disabled during the cutover).
- **Executor driver:** nothing automates the in-game executor button or mailbox trips now. Reintroducing it
  needs a design that obeys the click rules in `CLAUDE.md` and the small-loop rule.
- **Launch target:** route provenance is `unmeasured on client build 70862`; routes are not shipped.
