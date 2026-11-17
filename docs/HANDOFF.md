# Current handoff

Updated 2026-11-17. The user authorized the repository refactor, parallel Luna implementation
and independent Sol review loops, commits on main/origin/main, and task artifact cleanup.
This supersedes the normal commit-confirmation rule for this task. It does not authorize a
real trade. Exact immutable batch authorization and a separate current-session arm are required.

The coordinated desktop packages 0.2.0 are installed from reviewed code commit `5164928`.
`crowley-host` replaces the retired legacy `crowley-scan`; both host and companion are active.
All pre-existing disabled timers remain disabled. Both game addons are 0.3.0; scanner frame
schema is 1 and automation schema is 3. See [STARTUP](STARTUP.md) and [STARTUP_PLAN](STARTUP_PLAN.md).

The user explicitly authorized termination of the stranded headless client. One exact-process
SIGTERM ended it; no SIGKILL was needed. The original Battle.net instance remains intact.
A separate native Linux crash-notification dialog ignored the one guarded XTEST Cancel click.
After verifying its exact identity and installed response behavior, one ordinary SIGTERM
ended only that notification without opening or sending a report. Its crash files remain.

Battle.net had independently updated wowf to 1.60.1.70959 while the old client was headless.
The selected target, operational catalog build and generated addon target are now promoted
together, preserving revision 9, all 807 item IDs/pools, cadence/caps and historical item/export
facts. [Build promotion plan](BUILD_70959_PLAN.md). New plan hash:
`814c25b46d4f5fc2d24355ca2e712bf07cdd2bcf2dd39852b006ba1f220285ee`.
Historical 70938 item/export facts and the separately qualified seller build are unchanged;
this promotion does not qualify selling, movement or a new client-data export.

The market server now runs reviewed image
`sha256:5d336ea2487bf8821a1b91f167d8806846e4fc145cf5a05afa8de7db90798e1b`.
Its health and authenticated canonical-plan response pass. The original database container,
DB/raw mounts and all twelve migration checksums are unchanged. No schema change, manual
production database write or trade was performed. Companion wrote the exact canonical
three-field Data.lua and the matching 70959 plan before normal host restart.

Normal host cold-start successfully dispatched Play, adopted exactly one new client, entered
`Dandune Hillmont`, repaired/verified fullscreen and reached matching native world readiness
with an independent scanner ready publication. Strict expected build 70959/interface 16001
passed native readiness. Normal guarded targeting and binding completed, and interaction
opened the AH. The first 70959 sweep began at approximately 17:25:36 UTC; fresh status at
17:25:38 reported AH open, scanner phase item and sweep_running. Collection is active and no
user intervention was needed. First saved-payload acceptance, retained prices and loaded ACK
are still unverified; the existing read-only observer is following the normal save/upload
boundary. Leave collection running and honor the standard human-presence hold.

The 70959 promotion passed the full suite: 1497 tests / 31 skipped. The subsequent one-line
backup verifier fix passed 129 affected tests / 1 skipped, with independent Sol READY reviews,
Ruff check/format and zero Pyright errors. All five installed namespaces byte-match reviewed
wheels. The backup verifier now inspects migrations with the packaged server interpreter;
see [the bounded fix](BACKUP_IMAGE_RUNTIME_FIX.md).

A completed legacy snapshot has validated original dump/raw bytes. Its disposable restore
is NOT qualified: after correcting migration inspection, a fresh isolated attempt failed at
create_database and verified cleanup removed only its own disposable resources. The user
then explicitly prioritized immediate scanner recovery, so the remaining restore investigation
was deferred before the reviewed server-only cutover. Do not weaken restore checks, label the
backup restore-verified, or reset scan policy history. The original server image, secret/config
rollback copy, saved-data backups and exact receipts are retained privately under
~/.local/state/crowley/startup-rollout-20261117; the snapshot is under market-backups.

Remaining collection evidence is completion of the first AH sweep, original saved payload acceptance,
retained positive prices, ACK publication versus later loaded ACK, and a quiet warm host
restart preserving the game. A previous aggregate native-observation refusal during long
binding input remains unclassified; do not widen freshness/deadlines without actual evidence.

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
