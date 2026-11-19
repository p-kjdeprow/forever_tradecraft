# Current handoff

Updated 2026-11-18. The user authorized the repository refactor, parallel Luna implementation
and independent Sol review loops, commits on main/origin/main, and task artifact cleanup.
This supersedes the normal commit-confirmation rule for this task. It does not authorize a
real trade. Exact immutable batch authorization and a separate current-session arm are required.

The Merchant's Favor tab now leads with an actionable **profit-first recipe route**
at `/dashboard/merchant-favor`, with read-only API
`GET /v1/houses/us-normal-alliance/merchant-favor/route`. The user explicitly removed
gold/Alchemy-skill restrictions; default character progress remains Neutral 2,150,
30 Favor, level 35. Rank positive current per-produced-item margins, plan the next
unowned recipe only, and show the full profitable roadmap. A bounded market-only
integer optimizer jointly chooses actual finite-depth fills, forged returns and
11 qualified daily writs (1,025 base rep/reset). It protects shared stock, excludes
completed quests and redundant farming, enforces Friendly before receipts, and
returns an explicit partial plan if supply cannot finish the unlock. Original query
timestamps expire purchase instructions after 30 minutes; failed/profile-changed
refreshes clear them. Current matched-price margins deduct materials and the AH
cut; successful-sale deposits are refunded. Missing prices remain distinct from
losses. A final dependency audit admitted sourced Imbued Vial vendor supply only
for this route, using native 30,000c/five-vial facts. Vendor material value is
prorated by consumption, with whole-pack upfront cost disclosed; other views
retain their existing vendor assumptions. All 28 recipes now have either core
queries or validated vendor sources for every reagent. No purchases/game trading are performed. See [feature record](MERCHANT_FAVOR.md)
and [scope](MERCHANT_FAVOR_ROUTE_PLAN.md).

Catalog/target revision **11** retains all prior IDs, adds 11 writs and promotes
16 slower recipe outputs: **509 core + 344 cold**, 853 total, with six cold slices
and 566/567 full-sweep queries. Cadence/cap remain 900/750 seconds. Compatible
installed wheels change only two common data files and GeneratedTarget.lua;
unqualified input/listing changes remain undeployed. Source and bounded cutover
helper received Sol READY. 160 focused tests pass (15 optional DB cases skipped),
Ruff/Pyright and generation checks pass. The server image is
`sha256:05b0e637ec8cd6fb54f00d000caa78c60cace5bf6692118623c6a77167592cbe`.
The guarded normal close verified old sweep 1010 accepted, exact process exit,
forward Data.lua field set/canonical hash and doctor before automatic resume.
The host relaunched Dandune and opened the AH without manual intervention.
Live browser checks pass for 28 recipes/11 writs, saved ownership/daily completion,
reset, failed-profile refresh clearing, main page and 390px layout; no errors/images.
First expanded sweep 1011 queried all 567 planned items successfully, including
all 176 route dependencies, in 729 seconds. Its live page shows 23 positive margins
and a complete first-unlock shopping/turn-in plan; browser checks pass against
the actual checklist. Two consecutive full-sweep cadence qualification is pending.
The optimizer is capped at one CPU thread; three fresh-server requests returned
the identical route in 0.832–0.886s. Thirty independent exhaustive small-stock
cases also match the optimizer. No model calls are added to collection. Private rollback wheels, source
research and deployment/browser receipts are under
`~/.local/state/crowley/favor-route-20261118/`.

Previous revision 10 qualification: sweeps 1005/1006 queried all 542 items including
126 crate/fill dependencies, in 745/633 seconds, starts 900 seconds apart. These are
historical baseline evidence, not qualification of the expanded revision 11 plan.
Previous server image `sha256:f173a7ae1dc3cecd1f16d97cfe12e9795a378b6c1b2b6f0a30934f60c65f8940`.
Older deployment receipts: `~/.local/state/crowley/merchant-favor-20261118/`.

The dashboard now separates crafting strategies into Alchemy and Tailoring sections,
each showing up to 25 results after the saved skill and minimum-profit filters.
Ranks restart within each profession; each section shows its matching count and
an explicit empty state. The complete candidate pool and existing API contract
are unchanged; filtering still makes no additional requests. Long training notes
wrap on mobile. The website-only deployment keeps the exact installed common
wheel and changes only the market package's dashboard HTML. Scanner/companion
processes, database and mounts are unchanged. Verification: 55 focused tests pass;
live browser checks pass for nine skill/profit combinations, saved preferences,
independent limits and 390px layout without errors or screenshots. That deployment's server
image: `sha256:1bf35121ab746d728d6e97be69240ad9cdec26f7e0c82dc1d51c1abcdf8a3e2d`.
Browser results and deployment receipts are retained privately under
`~/.local/state/crowley/profession-groups-20261118/`.

Scanner recovery on Nov 18 found the host alive but permanently latched in
`startup_failed/desktop_observation_failed` since Nov 17 20:42 CST. The last accepted
sweep was 937 (21:06 CST), and the existing client showed WOW51900319. A guarded
normal WM close verified exact process exit; the existing Battle.net instance
relaunched the client. Initial character-entry revalidation refused a changed
control before press; a fresh attempt entered Dandune. Another single observation
error latched during binding despite a later healthy native read.

A narrow startup hotfix now treats ObserveError like existing capture failure:
release owned controls, invalidate stale proof, and retry observation under the
original episode/phase deadlines. Expiry remains terminal and unreaped controls
retain ownership. Sol reviewed READY after the phase-expiry regression; 148 focused
tests, Ruff and Pyright pass. The deployed automation wheel was built from the
installed 8b755b9 baseline plus only startup.py; all 47 package resources were checked
and only that module differs. Installed bytes match the wheel, doctor passes, and
listing/uinput changes remain undeployed. Recovery sweep 939 finished Nov 18 at
00:36:53 CST and uploaded at 00:37:27: all 471 planned items queried successfully,
14,038 listings accepted. The website, trading summary (123 candidates, ten
opportunities) and recipe watch all serve that sweep with HTTP 200. Private receipts
and exact rollback wheel are in `~/.local/state/crowley/scanner-recovery-20261118/`.
Do not equate unit activity/website HTTP success with successful collection. See
[the incident record](SCANNER_RECOVERY_20261118.md).

New listing policies and manual batches now default to **two hours** when
`durationHours` is omitted; canonical inspection makes that value explicit before
hashing/authorization. Forever durations are 2/8/24 hours (native indices 1/2/3),
verified from the installed Forever-specific Auctionator 341 constants and radio
mapping. The old Retail 12/24/48 assumption is removed from the automation model,
policy schema, Lua staging/post/deposit mapping, receipt checks and shared frame
reader. Old 12/48-hour policies refuse rather than being reinterpreted. Per-item
`batchSize` independently limits each posting, clipped to admitted bag stock; total
`quantity` remains the separate stock cap. Existing policies/grants and installed
runtime are unchanged. See [listing defaults](LISTING_DEFAULTS.md).

The natural-input refactor is implemented in source, independently reviewed READY
by Sol after two fix rounds, and remains separate from the installed runtime.
Final verification: 575 affected tests, Ruff check/format and Pyright pass; all five
matching wheels and SDISTs build with required input modules/resources. It shares bounded timed typing across host and standalone input,
allows stationary clicks within safe bounds, and adds explicit calibrated uinput
with exact physical-device monitoring and independent hold expiry/cleanup. Forward
continuity is confined to one admitted waypoint; stationary turns and per-waypoint
ledger/readback remain. Host/scanner transport stays XTEST. Cold short manual uinput
arms need positively qualified keyboard/mouse idle coverage; other topologies need
full idle observation in a foreground session. No authorization is renewed for this.
See [input implementation and qualification](HUMAN_INPUT.md) and the
[reviewed plan](HUMAN_INPUT_REFACTOR_PLAN.md). New device delivery, current-client
movement and protected trades are not live-qualified. No game input, screenshots,
service/config changes or new device permissions are part of this source task.

The user cancelled Steam Deck deployment and selected this desktop for selling.
The exact Deck addon and staged wheels were verified and removed; no other addons,
SavedVariables, SSH keys or services were changed. The Deck account question is
obsolete. Do not resume the Gamescope port or deploy anything further to the Deck.
WoW was running at the final removal check; the user was told to `/reload` or quit
normally to unload any in-memory copy. No remote game input was sent, and an actual
in-memory unload is not yet verified.

Desktop preparation passes doctor and 283 automation tests. The installed coordinated
addon's eight files and pinned input helper are verified. A fresh supported native
observation confirms Dandune, house us-normal-alliance, build 70959, open AH, no pending
operation and the expected loaded schema. A content-addressed AH NPC profile draft is
saved privately; it is not installed into active configuration pending character choice.
The active target/route directories contain no profiles. No trading policy/session is
prepared or armed, and current-client posting/cancellation/mail effects remain unqualified.
The environment is prepared for a controlled first listing trial once the user supplies
character, exact stock and batch sizes. The full unattended recycle loop still requires
measured mailbox routes and bounded live qualification. Read
[desktop preparation](DESKTOP_SELLER_PREPARATION.md); private preflight/uninstall
receipts are under `~/.local/state/crowley/desktop-seller-prep-20261117/`.
SSH/non-graphical shells must use the authenticated user-manager environment for
standalone CLI calls, as shown in that guide; a missing ambient XAUTHORITY does not
establish a broken game session. Scanner and companion remain active.

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

The user subsequently removed the market-activity eligibility filter: profitable
current-price strategies must remain visible even with unknown, zero or slow activity.
The audit found 100 positive if-sold recipes in sweep 906, all hidden solely because
the new build had no usable activity intervals. Do not interpret an empty filtered
list as proof that no profitable recipes exist. The server and dashboard now admit
these candidates, retain activity as informational evidence, and keep the user's
minimum-profit preference. Old filtered browser caches are isolated by the changed
policy key. See [the change record](PROFIT_FILTER_REMOVAL.md).

The dashboard now also has a saved **Maximum profession skill** control: 150, 225
(default), or 300, applied to both Alchemy and Tailoring. The server computes a full
pool through 300, and the browser applies skill and profit preferences before the
top-ten limit without another API call. Exact learning ranks and verified yellow
upper bounds remain distinct; unknown learning ranks retain their warning. The new
cache profile prevents reuse of the previous 225-limited pool. Live sweep 912 has
72/125/149 profitable candidates at the three skill limits before the minimum-profit
preference. All selections, reload persistence, composition with profit and mobile
layout passed real browser checks; 52 focused tests, Ruff and scoped Pyright pass.
Only the server was redeployed; scanner/companion PIDs and DB/raw mounts are unchanged.
See [the filter record](PROFESSION_SKILL_FILTER.md). Private deployment and browser
receipts are in `~/.local/state/crowley/profession-skill-20261117/`.

A subsequent user report exposed a real ranking defect: all recipes marked as
leveling targets were placed below all ready recipes, regardless of their economics.
At sweep 914, selecting 300 added 22 candidates at the 50c profit threshold but
left the displayed ten identical. That unconditional training tier is now removed.
Ranking uses estimated profit rate, then batch profit and spell ID; training labels
remain visible without changing order. At 300 the live ten now include Greater
Arcane Elixir (285), Distilled Firewater (250) and Elixir of the Grizzly (300).
All 146 candidate records matched exactly before/after for the same sweep; only
ordering changed. Client-export teaching evidence independently confirms the
285 requirement for Greater Arcane Elixir. The correction passed 56 focused tests,
Ruff, scoped Pyright and a live browser/cache check. Scanner and companion processes
were left running. See [the correction record](STRATEGY_RANKING_FIX.md); private
receipts are in `~/.local/state/crowley/strategy-ranking-20261117/`.

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

The earlier profit-filter removal deployed image
`sha256:9ff7338ea59d7ca52a57a672bb4c09df76bd6f6b98c74b96a1b1b28d6dbea0e1`.
The original database container and DB/raw mounts are unchanged. All twelve applied
migrations retain their original bytes. The supported client-data exporter and loader
imported genuine build 1.60.1.70959 data (export 5: 2,282 recipes, 4,556 items).
Resumption verified HTTP 200 for sweep 906: 470 queried and 470 successful.
A headless browser verified automatic refresh from 905 to 906, matching summary and
watch identities, a fresh scan indicator, no JavaScript errors, and HTTP 200 on all
API calls. Profit preferences persist across reload and invalid house input is
rejected. No browser screenshots were taken. Following the activity-filter removal,
sweep 906 returned 100 profitable candidates and a populated top ten. Unknown activity
is explicitly labeled. Served dashboard bytes match source; refresh a tab loaded
before deployment to obtain the updated profit-only view. Resume evidence is in
`~/.local/state/crowley/resume-20261117/`; filter audit, deployment and browser proofs
are in `~/.local/state/crowley/profit-audit-20261117/`. The filter removal was deployed
to the market server only; installed desktop packages and both running services were
left intact. Its 49 focused tests, Ruff checks and scoped Pyright pass.
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
