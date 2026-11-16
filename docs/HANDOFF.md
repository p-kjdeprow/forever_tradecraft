# Current handoff

Updated 2026-11-15. The user authorized the repository refactor, parallel Luna implementation
and independent Sol review loops, commits on main, and cleanup of task worktrees/temp files.
This supersedes the normal commit-confirmation rule for this task. It does not authorize a
real trade. Exact immutable batch authorization and a separate current-session arm are required.

Retain the main dashboard and recipe watch, existing AH collection, scan/probe transport,
health/diagnosis and backup/restore. Delete every inactive workflow, then build fresh automation
for posting commodities/items, cancellation, mail collection and movement. No buying, crafting
or vendor automation. Dashboard crafting economics remain a retained calculation.

The repository now has common, market, scanner and automation component packages and a thin
host application. See [ARCHITECTURE](ARCHITECTURE.md), [RESTRUCTURE_PLAN](RESTRUCTURE_PLAN.md)
and [RESTRUCTURE_API](RESTRUCTURE_API.md). Scanner and automation are separate addons and
runtimes; only host composes them. The old executor, queue/Queue.lua pipeline, account/reference
capture, advanced dashboard/advice/forecasts, paper/backtests and unused lifecycle machinery
are removed. Historical SQL tables and all applied migrations remain unchanged.

The source refactor is complete. Cleanup, independent packages, strict operation ledger,
measured movement, host cutover and concrete native/desktop execution passed independent Sol
reviews after Luna implementation/fix loops. The canonical source is in packages/ and apps/host/;
the old src/crowley and root addon/deploy resource copies are removed. Installed addons are
copies, not links, and this work did not deploy packages or change running services.

The next feature is the [continuous seller](SELLER_PLAN.md), currently planning only. The user
requires small posting batches, 60-second market checks, always matching price without undercutting,
and recycling only the managed active listings for the exact item that runs out of bag stock.
Native priority observations, durable selling-session authorization and selective return collection
are planned extensions, not capabilities of the current released automation CLI.

Before this refactor, the deployed scanner used catalog revision 9 on Forever build 70938,
interface 16001. Its periodic scan plan is unchanged by this task. The dashboard's minimum-turnover
patch was already deployed over 5f2cc00, image
sha256:bb5dc1fe200cff70f596f80ca1a7798985cf4b7b1fa87a635c5c0efa08e39230.
These are recorded prior deployment facts, not proof that the new packages have been installed.
The current running state must be checked from installed configuration before any cutover.
Private historical receipts remain in .archive and must not be deleted during source cleanup.

The dashboard uses a rolling 24-hour activity window and requires at least 6 observed market
units/hour, tested exactly before rounding. Scheduled cold cadence is read from each historical
plan; outages never become zero demand. Positive margin assumes sale and listing depletion is
not confirmed revenue. Per-item profit filters apply before the top-ten slice and retain cached
recommendations through refresh failure. See [ANALYSIS](ANALYSIS.md).

Reported skills are Alchemy 225 and Tailoring 150, with planning through 225. Recipe ownership
and a 10 g starting budget are assumptions. Dandune is the scanner on another account, not proof
of the trading character's inventory/skills. A character has two primary professions. Do not
promote historical beta backtest results to forecasts or live trade authorization.

No route is qualified for the selected current build. Historical measurements establish raw
coordinate/heading conventions only. New immutable profiles must bind the actual build, instance,
house, character, calibrated signed turn rates and measured waypoints. Do not reuse old beta
coordinates. Stored native API probes establish 70862 availability, not 70938 protected behavior.
Current-client qualification is still required before a real operation is armed.

Final parent verification: full default suite 1203 passed / 31 skipped; disposable Timescale
migration/ingestion/item-loader/house/client-data checks 37 passed; real packaged-migration
backup inventory 1 passed. Ruff check and format pass, Pyright reports zero errors, and generated
target validation passes. The full suite includes isolated wheel/SDIST rebuilds, exact package
resources, installation of all five wheels outside the checkout, forbidden-import checks and
all eight CLI help commands. All 12 applied migration files retain their original bytes.
No production database, real game input or live trading was used. Task-owned test resources are
removed at completion; main is the only worktree. The pre-existing private .archive is retained.

Operational docs: [AUTOMATION](AUTOMATION.md), [GAME_SETUP](GAME_SETUP.md), [REPEATING_SCANS](REPEATING_SCANS.md),
[LAUNCH_TARGET](LAUNCH_TARGET.md), [WATCHLIST](WATCHLIST.md), [MARKET_RECOVERY](MARKET_RECOVERY.md)
and [TESTING](TESTING.md). Read the local-game-control skill before any game contact, and
client-contracts before native schema/API changes. Never interpret a submitted input, callback,
saved file, server ingestion and loaded acknowledgement as the same stage of completion.
