# Current handoff

Updated 2026-11-17. The user authorized the repository refactor, parallel Luna implementation
and independent Sol review loops, commits on main, and cleanup of task worktrees/temp files.
This supersedes the normal commit-confirmation rule for this task. It does not authorize a
real trade. Exact immutable batch authorization and a separate current-session arm are required.

Cold-start source fixes are independently reviewed and installed as the five component
packages at version 0.2.0, code commit `8ff9912`. `crowley-host` replaces the retired legacy
`crowley-scan`; companion remains enabled and active. All pre-existing disabled timers remain
disabled. See [STARTUP](STARTUP.md) and [STARTUP_PLAN](STARTUP_PLAN.md).

Live qualification reached a normal cold Play, automatic recovery of a real initial window-restore
timeout, fullscreen repair, verified `Dandune Hillmont` entry, and independent fresh scanner/native
publications on build 70938. Both game addons are 0.3.0; scanner frame schema is 1 and automation
schema is 3. The selected display label is independent of native Dandune and the pinned native realm;
no realm is displayed on character selection. Battle.net identity uses exact executable argv0,
UID, prefix and start ticks because its mutable comm is CrBrowserMain.

End-to-end collection is NOT yet qualified. Long binding input exposed a native observation refusal;
the measured stale case now gets one fresh capture within the unchanged observation deadline, and
AH postcondition clocks begin at admitted Return. A later aggregate observation refusal remains
unclassified: thirty read-only samples passed, so do not label it process drift or elapsed-time
exhaustion, or change a limit without evidence. No new sweep or trade occurred on this boot.

Current recovery is blocked by one headless WoW process. The old recovery implementation used
xdotool's XDestroyWindow command, removing its main window without ending its process. The corrected
source uses an exact-XID normal `wmctrl -i -c` request and rejects destructive close/kill forms.
A separately verified Win32 WM_CLOSE-only request flushed SavedVariables but did not end the
remaining process. Host is deliberately stopped; companion and the original Battle.net remain
running. Explicit user authorization for termination of only the verified remaining process is
pending. Do not send signals, start a duplicate client, reset policy history, or reuse old GUI proof.
Private exact identities, pidfd termination proposal, backups and receipts are under
~/.local/state/crowley/startup-rollout-20261117/qualification.

Source verification passed 1497 tests / 31 skipped, Ruff check and format, and Pyright with zero
errors. All five installed namespaces match their reviewed wheels byte for byte; package tests
also exercise isolated distributions. All twelve applied migrations retain their original bytes.
No server image, schema, manual database write or live trading was performed. Remaining live gates
are safe client recovery, the actual AH sweep, original saved payload acceptance and retained prices,
ACK write versus loaded ACK on a real boot, and a quiet host restart preserving the game process.
Private release/rollback artifacts remain at ~/.local/state/crowley/startup-rollout-20261117.

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
