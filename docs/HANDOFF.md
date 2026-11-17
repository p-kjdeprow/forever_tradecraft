# Current handoff

Updated 2026-11-17. The user authorized the repository refactor, parallel Luna implementation
and independent Sol review loops, commits on main/origin/main, and task artifact cleanup.
This supersedes the normal commit-confirmation rule for this task. It does not authorize a
real trade. Exact immutable batch authorization and a separate current-session arm are required.

The urgent typing/dashboard repair is installed. The user's latest instruction
authorizes resuming collection and requires a fully functional website. Host and
companion are now active: the host launched one client, entered the world, targeted
the auctioneer, installed the binding and opened the AH. Fresh sweep 906 completed
and reached the website through the normal save/upload/ACK path. All 470 planned
items were queried successfully; 10,825 positive-price rows were retained. Actual
collection ran 18:58:08–19:08:02 UTC (594 seconds), similar to the preceding build
70959 sweep's 583 seconds. The original saved payload is preserved privately.
Do not stop collection based on the earlier stop instruction; it was superseded.
No screenshots or game input were used during the preceding offline repair.
See [the recovery record](URGENT_RECOVERY_PLAN.md) for causes, checks and limitations.
User-managed alerts are paused; their unfinished changes are preserved privately.

The cold-start refactor introduced two full captures and a Python child per typed
character. Commands now use one explicit key transition chain through one monitored
child, with cheap stop/presence/maintenance/session/process/focus checks during typing.
Fresh screen checks and exact complete text remain mandatory before the separate
Return action. This removes capture/helper cost proportional to command length.
Live startup now confirms the 123-character binding step completes approximately
10 seconds after the preceding chat action (including the next observation/admission),
versus approximately 397 seconds before the repair. Exact keystroke-only duration
is not independently measured. The local host/scanner/startup runtime makes no model
calls; agent diagnostics are separate.

All five coordinated desktop packages 0.2.0 are installed from the current repair
wheels, with installed namespaces verified byte-for-byte. `crowley-ops doctor` passes.
The repair passed 134 focused tests, Ruff check/format and Pyright (zero errors).
The final batching design was completed by the parent after Luna workers reached
their usage limit; it has not received an independent final Sol review.
`crowley-host` replaces legacy `crowley-scan`; pre-existing disabled timers remain
disabled. Both addons remain 0.3.0, scanner schema 1 and automation schema 3.
See [STARTUP](STARTUP.md) and [STARTUP_PLAN](STARTUP_PLAN.md).

The server now runs image
`sha256:e6230b4abd20bd383ff51e2e03abc07eb1548ef4a1375f1c5562253d7f8b5994`.
The original database container and DB/raw mounts are unchanged. All twelve applied
migrations retain their original bytes. The supported client-data exporter and loader
imported genuine build 1.60.1.70959 data (export 5: 2,282 recipes, 4,556 items).
Trading-summary now returns HTTP 200 for sweep 906: 470 queried and 470 successful.
A headless browser verified automatic refresh from 905 to 906, matching summary and
watch identities, a fresh scan indicator, no JavaScript errors, and HTTP 200 on all
API calls. Profit preferences persist across reload and invalid house input is
rejected. No browser screenshots were taken. Current filters produce zero qualifying
opportunities; this is a visible empty result, not a loading failure. Served dashboard
bytes match source; refresh a tab loaded before deployment to obtain the repaired
recipe-watch validator. Resume evidence is in `~/.local/state/crowley/resume-20261117/`.
The health command's only remaining issue is the previously deferred stale backup
record; neither scanner nor website health is currently flagged.
Historical 70938 item/export facts are preserved; no schema change or trade occurred.

The current selected target, operational catalog and generated addon target are all
1.60.1.70959, preserving revision 9, 807 IDs/pools, cadence and caps. Plan hash:
`814c25b46d4f5fc2d24355ca2e712bf07cdd2bcf2dd39852b006ba1f220285ee`.
See [build promotion](BUILD_70959_PLAN.md). This does not qualify selling or movement.
Normal cold-start previously adopted one client, entered Dandune, verified fullscreen
and native readiness, and opened the AH. The first 70959 sweep ran from approximately
17:25:36 to 17:27:02 UTC (86 seconds); its saved payload was accepted with 2,002
positive-price rows and an ACK was written. A later loaded ACK is not independently
verified. A subsequent accepted sweep (905) was the dashboard's latest before
resumption; the current browser now shows 906. ACK 905 was independently observed
loaded in the resumed client. ACK 906 is written; its later loaded state remains
separate from server acceptance.
The original Battle.net instance was preserved through recovery; do not duplicate it.

Earlier build promotion passed 1497 tests / 31 skipped. The backup interpreter fix
passed 129 affected tests / 1 skipped with Sol review; see
[the bounded fix](BACKUP_IMAGE_RUNTIME_FIX.md). These are historical checks, separate
from the final urgent repair's 134-test focused run.

A completed legacy snapshot has validated original dump/raw bytes. Its disposable
restore is NOT qualified: after correcting migration inspection, an isolated attempt
failed at create_database and cleaned up only its own disposable resources. The user
deferred further restore investigation to prioritize scanner recovery. Do not label it
restore-verified or reset policy history. Rollback copies and exact receipts remain in
`~/.local/state/crowley/startup-rollout-20261117/`; the snapshot is in market-backups.
Current repair receipts/wheels and paused alert patches are in
`~/.local/state/crowley/urgent-recovery-20261117/`.

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
copies, not links, and the earlier refactor was source-only. The cold-start task subsequently
installed the coordinated desktop set described above.

The [continuous seller](SELLER.md) is implemented in source through parallel Luna/Sol loops.
See the [completion record](SELLER_IMPLEMENTATION.md) and [approved plan](SELLER_PLAN.md).
It posts small commodity batches, checks each item every 60 seconds, always matches price without
undercutting, and recycles only managed active listings of the exact item when another batch is
needed and admitted bag stock is exhausted. The user approved one matched-price batch per item
per 60 seconds during tied-price competition despite unobservable queue rank, and exact-commodity
mail collection up to outstanding cancelled quantity without an auction-to-mail link.
No priority/source identity is invented. Session authorization binds the full immutable policy
digest and explicit current boot; runner-derived children preserve stock, expiry, spending and
session-total recycle limits across explicit resume. Native automation schema is 3, addon version
0.3.0. Scanner schema and scheduling are independent. Source changes do not update installed CLI,
addons or services. All three independent Sol source reviews are READY with no remaining findings.

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

Earlier seller-only verification: full default suite 1323 passed / 31 skipped; parent automation suite
283 passed, plus Store 35 passed after the final message-only edit. Ruff check and format pass,
Pyright reports zero errors, and generated target validation passes. The full suite includes
isolated wheel/SDIST rebuilds, exact package resources, installation of all five wheels outside
the checkout, forbidden-import checks, all eight CLI help commands and every seller subcommand.
Both updated skills validate. All 12 applied migration files retain their original bytes;
this seller work changes no market SQL. The earlier refactor separately passed 37 disposable
Timescale checks and one real packaged-migration backup inventory check.
No production database, real game input, deployment or live trading was used. No task-owned
temporary artifacts remain; main is the only worktree. Pre-existing private .archive evidence
and distribution artifacts are retained.

Operational docs: [AUTOMATION](AUTOMATION.md), [GAME_SETUP](GAME_SETUP.md), [REPEATING_SCANS](REPEATING_SCANS.md),
[LAUNCH_TARGET](LAUNCH_TARGET.md), [WATCHLIST](WATCHLIST.md), [MARKET_RECOVERY](MARKET_RECOVERY.md)
and [TESTING](TESTING.md). Read the local-game-control skill before any game contact, and
client-contracts before native schema/API changes. Never interpret a submitted input, callback,
saved file, server ingestion and loaded acknowledgement as the same stage of completion.
