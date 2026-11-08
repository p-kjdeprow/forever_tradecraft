# Forever Tradecraft handoff

Updated 2026-10-24. This is the active handoff: current state, rules, and what remains before launch.
The beta-era handoff (1,837 lines of chronology and proofs) is preserved verbatim in git
(`git show 7fd2e1d:docs/HANDOFF.md`) and in `.archive/handoff-history-before-launch-prep-20261023.md`.
Technical reference: [ARCHITECTURE.md](ARCHITECTURE.md) (including the client assumptions table).

## Status: between beta and launch

- **The beta has ended.** WoW Forever launches **2026-11-04** with a fresh economy. Nothing touches the game
  until launch: no collection, reconnects, calibration or trading.
- **Off-season service state** (all user systemd units are symlinks into `deploy/`):
  - `crowley-session.service` stopped and disabled.
  - `crowley-paper-trading.timer`, `crowley-health.timer`, `crowley-market-backup.timer` disabled.
  - `crowley-companion.service` running and idle (no game); it writes `Queue.lua` with no queue.
  - Docker `crowley-db` and `crowley-server` up; migrations 001–008 applied.
- **Beta data retained:** 298 sweeps / 127,544 observations for house `beta-normal-alliance`
  (builds 1.60.1.70374 and 1.60.1.70450, 2026-10-15 → 2026-10-23). Backups: 7 daily snapshots plus 14
  restore-drill receipts in `~/.local/state/crowley/market-backups` (final snapshot
  `beta-20261023T215111Z-8dd827fad91d`).
- **Code:** launch preparation is verified on local MAIN; the user authorized committing and pushing it
  to `origin/main` on 2026-10-24. At task start MAIN was two commits ahead of local origin/main.
  The existing suite-green worktree/branch remains untouched as read-only reference. No worktree removal,
  production migrations, deployment or game input were performed.
  Current execution checkpoint: `.archive/launch-readiness-20261023/STATUS.md`.

## Objective

Make gold on the WoW Forever auction house with several concurrent, independently profitable strategies:

1. **Collect** the market: a 428-item scan plan every 900 s, cheapest 20 distinct unit-price levels per item,
   never mixing houses or item identities (`itemKey`).
2. **Model** it: demand, time-to-sale, price metrics, recipe economics from the client's own data.
3. **Decide** with the trading engine (backtest → paper trading → queues).
4. **Execute** through the guarded addon executor (dry run unless doubly armed), with automated input at human
   speed and human motion (see Rules), plus route walking between the AH and the mailbox.

## Rules

- `CLAUDE.md` rules apply (no fallbacks, `itemKey` only, new migrations only, ≤1 automated click/s, full simulated
  human pointer motion, ask before committing/pushing/destroying data).
- **Real gold:** nothing is armed without the user's explicit authorization for that specific queue
  ([executor contract](EXECUTOR_CONTRACT.md), [runbook](LIVE_TRADING_TEST.md)).
- **One GUI owner at a time.** Before any automated input, confirm the game window has focus and nobody is using
  the desktop; verify each action through addon telemetry (XTEST keys go to the Wayland-focused window, not
  necessarily the X "active" window).
- **Workflow:** implementation goes through the Luna (implement) / Sol (review) loop; the parent independently
  verifies and maintains worktree status. Commits/pushes require separate approval. Origin:
  https://github.com/p-kjdeprow/forever_tradecraft.
- **Privacy:** raw frames, receipts, SavedVariables, recordings and backups stay private under
  `~/.local/state/crowley` (dirs 0700, files 0600). Never edit game SavedVariables. Secrets live only in the
 the private `$XDG_CONFIG_HOME/crowley/secrets.env`. Stage explicit files only; never `git add .`.

## Components

| Component | Code | Docs |
|---|---|---|
| Addon (scanner, telemetry HUD schema 6, account capture, executor, reference) | `addon/Crowley`, `addon/Crowley_Data` | [ARCHITECTURE](ARCHITECTURE.md), [EXECUTOR_CONTRACT](EXECUTOR_CONTRACT.md) |
| Companion (SavedVariables ↔ server, Queue.lua) | `src/crowley/companion` | — |
| Server + TimescaleDB | `src/crowley/server`, package-owned Compose resource | [MARKET_RECOVERY](MARKET_RECOVERY.md) |
| Session daemon (collection driver, guarded input, route walking) | `src/crowley/session` | [REPEATING_SCANS](REPEATING_SCANS.md), [RUNTIME_PERFORMANCE](RUNTIME_PERFORMANCE.md) |
| Analysis and client data | `src/crowley/analysis`, `src/crowley/core/client_data.py`, `tools/client_data*` | [ANALYSIS](ANALYSIS.md) |
| Trading engine, paper trading, queues | `src/crowley/trading`, `src/crowley/core/queue.py` | [TRADING](TRADING.md), [LIVE_TRADING_TEST](LIVE_TRADING_TEST.md) |
| Live configuration and routes | `deploy/config/` (linked from `~/.config/crowley`) | [deploy/config/README](../deploy/config/README.md) |
| Game setup | — | [GAME_SETUP](GAME_SETUP.md), [WATCHLIST](WATCHLIST.md) |

## What the beta established

- **Collection** worked operator-assisted (last fully verified sweep: scan 290). Autonomous qualification was
  never reached: native eventual-echo delivery remains unqualified, and the driver moved to passive startup with
  takeover qualification (`.archive/PLAN-autonomous-startup-takeover-20261022.md`). Zero autonomous cycles counted.
- **Strategies** (20 g start, demand share 0.5, see [TRADING](TRADING.md)): crafting dominates — alchemy and
  blacksmithing (≈148 g of ≈218 g closed-trade profit over the beta), then engineering and leatherworking;
  Coarse Grinding Stone alone was ~¼ of profit (concentration risk). Scalping lost money in-model. 58 % of listed
  units expired (deposit losses). A character has only **two** primary professions, so the launch portfolio must
  be cut to two. Station/tool/category/focus requirements are now enforced in both strategy and execution.
  Bronze Tube (spell 3938/output 4371) requires focus 1 and category 162, with no exact tool item row;
  empty explicit capabilities reject it. The older ungated tables in TRADING are historical evidence.
- **Live trial facts** (build 70450, 2026-10-22): keyboard turning is π rad/s, run 7 yd/s; XTEST input lands up to
  ~1 s late, so open-loop walking drifted 13 yd while closed-loop stepping worked. JUMP=SPACE, TURNLEFT=Q,
  TURNRIGHT=E. The mailbox and auctioneers open with a left click; object hover shows in a UIParent-owned
  GameTooltip ("Mailbox"); `MAIL_SHOW` fires but `MAIL_CLOSED` did not, so `MailFrame:IsShown()` is the signal.
  These are recorded in the ARCHITECTURE assumptions table.

## Built but never exercised live

- **Executor and queue pipeline:** account capture, `/crowley arm`, dry-run queues, execution uploads, queue
  reconciliation (migrations 007/008). Test plan: [LIVE_TRADING_TEST](LIVE_TRADING_TEST.md).
- **Route walker:** `crowley-session route-walk` (closed-loop, Guardian-checked key timelines, holds ≤0.45 s below
  autorepeat) and `world-click` (hover-confirmed, rate-gated mailbox click). Routes in
  `deploy/config/routes/stormwind.json` (Stormwind AH ↔ mailbox, from hand-walked laps).
- **Telemetry schema 6 source** adds exact executor identity/arms/readiness/progress and a button-only callback
  ordinal to the auctioneer/blockingUI/hover/mailOpen surface. Schema 5 is refused. The addon and session daemon
  must deploy together; this source has not been qualified live. See [EXECUTOR_TELEMETRY](EXECUTOR_TELEMETRY.md).

## Launch preparation

1. **Suite refactor and final feature integration complete.** Parent bare `uv run pytest`:
   **2455 passed, 25 skipped, 33 warnings in 153.78s** externally measured wall time, excluding
   lock wait (2026-10-24). Default `-n16 --dist worksteal`, all retained native cases included,
   all 83 frozen source hashes unchanged; shared-DB parallel invocation correctly refused.
   Five duplicate tests and one forwarding helper were removed with retained-oracle mappings;
   a fragile local statistics timing assertion was deleted while keeping full-size correctness
   coverage. Final parent disposable DB run: **178 passed, zero skips, 34.08s**, including all
   25 default DB-gated cases. See [TESTING](TESTING.md) and the final-r4 checkpoint artifacts.
2. **Launch parametrization complete, Sol READY:** strict packaged target plus deterministic generation,
   drift checks, coordinated Lua/TOC output and staged configuration; session/companion startup reject stale
   identity/path/plan values. Beta hashes remain intact. See [LAUNCH_TARGET](LAUNCH_TARGET.md). Launch values
   and route remeasurement remain pending; current target stays beta. Generator `--check` and the
   installed mail-loop CLI passed; target/DB/trading checks are included in the verification above.
3. **Offline strategy work complete:** station-aware gates and immutable paper capability metadata are reviewed
   READY (new migration 009, applied only to disposable test DB). Comparable empty-capability replays over
   290 retained build70450 sweeps provisionally favor alchemy+engineering: +60.79g realized, 3512 modeled
   clicks, 6.55g lost deposits, 52.9% drawdown; 637.56g of final equity is marked unrealized inventory.
   This is beta evidence, not a fresh-launch forecast. Optional smaller-post/expiry policies passed
   Luna/Sol review and same-data comparison; all tested variants returned less realized profit with
   more clicks. Keep the existing 24h listing/exposure defaults and repricing off. See [TRADING](TRADING.md).
4. **Offline mail/executor work complete, Sol READY:** schema 6 producer/consumer, guarded daemon
   click, ACK-bound scheduling, immutable terminal/recovery evidence, control races and absolute
   deadline propagation are implemented and independently reviewed. Both retained whole-cycle tests
   exercise actual Lua publications/pixel decoding, public IPC, Guardian/input boundaries, return/AH
   reopening and ordinary reconciliation to `WAIT_DUE`: automatic unarmed dry run and two separately
   confirmed armed messages, including asynchronous completion without an extra click. Scheduling is
   process-local, opt-in and off after restart; uncertain work stays blocked. Parent driver/pointer
   verification: 71 passed in 3.41s; the final integrated default gate is recorded above.
   Native delivery, measured button bounds, protected APIs and live cycles remain launch-unqualified.
   No game input or gold arming is authorized by this offline work.
5. **Collection qualification at launch:** native and human input calibration, takeover qualification, then three
   autonomous 900 s cycles before claiming autonomy.

## Launch-day checklist

These steps apply at launch, after the relevant deployment/input authorization.

1. Install the actual launch client and record full/native build, interface, native region/realm, house,
   executable and installation roots. The current exporter supports enUS. Preserve all beta evidence.
2. Update the packaged [launch target](LAUNCH_TARGET.md), export the new client's own data, regenerate the
   item reference/scan plan and coordinated addon/TOC artifacts, and run generator `--check`. Render
   configuration into a staging directory and review it before installing; never relabel beta data/routes.
3. At a quiet authorized boundary, back up the market, deploy reviewed server changes and apply outstanding
   migrations (production is currently 001–008; 009 adds paper capability metadata). Verify health and beta
   retention. Deploy/reload addon and daemon together on [schema 6](EXECUTOR_TELEMETRY.md), with no active queue.
4. Re-enable backup/health timers. Verify native client identity, house/plan, input/focus/camera facts and
   takeover qualification. Complete three autonomous 900-second collection cycles before claiming autonomy.
5. Start a new paper run for the new house/build after its first accepted sweep. Verify actual learned recipes
   and held tools/categories/focus; empty capabilities remain unavailable. Use the provisional two-profession
   portfolio and retained 24h listing defaults only as modeling starting points, not launch-profit forecasts.
6. Remeasure kinematics and the AH/mailbox routes on the actual client. Review the route admission gate with
   that new evidence; it currently accepts only the verified beta build/provenance. Validate one supervised
   round trip, native mailbox close truth and guarded AH reopening before scheduling trips.
7. Qualify measured executor-button bounds/scale and XTEST callback delivery with an unarmed queue. Confirm
   actual dry-run results, no protected calls, causal private receipts and normal companion execution upload.
   Keep mailbox scheduling disabled until its deadline, ownership and recovery checks are qualified live.
8. Only with explicit authorization for the exact queue, run one armed micro-test and reconcile actual
   balances/items/results per [LIVE_TRADING_TEST](LIVE_TRADING_TEST.md). Uncertain effects remain blocked;
   preserve their journals/receipts and never reset evidence to manufacture retry permission.

## Entrypoints

- Python: the installed package's Python entry point; session CLI `~/.local/bin/crowley-session` (installed
  from the package); config `$XDG_CONFIG_HOME/crowley/session.toml`.
- Tests: `CROWLEY_RUST_TOOLCHAIN=<reviewed-toolchain>/toolchain/bin uv run pytest` (the default
  uses sixteen bounded xdist workers with work-stealing; add `CROWLEY_TEST_DATABASE_URL` only for the documented serial DB run in
  [TESTING](TESTING.md)).
- Backups: `systemctl --user start crowley-market-backup.service`; restore drills via `crowley-ops drill`.
- Pinned binaries: the paths reported by `crowley-ops doctor` under the XDG cache directory. Stale
  diagnostic builds should be removed only through the package's normal cache-management procedure.
