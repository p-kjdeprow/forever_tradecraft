# Startup and recovery refactor

Authorized 2026-11-16: understand and robustly fix cold startup, including Battle.net UI blockage,
existing WoW windows, world entry and return to scanning. Unlimited time means resolve the design
and failure matrix before implementation, not add a generic workflow framework. Use bounded Luna
implementation / independent read-only Sol review loops; parent owns contracts, integration and git.

## Observed failures and source defects

- A Battle.net What's New modal hides/dims Play; the launcher has only a blue-button detector.
- A manually started game has a display-sized client buffer inside a decorated/oversized or
  maximized outer window. The launcher returns when it sees an existing process, skipping repair.
- Host treats invalid geometry as a fault for 30 minutes and can terminate/relaunch a usable client.
- World entry uses absence of a decoded addon frame as permission to press Return repeatedly.
  Missing HUD is not proof of character selection, login identity, or safe input.
- The shell launcher owns asynchronous processes while systemd preserves game descendants.
  Failed/stopped startup can leave input-capable orphan control children. Host polls a monolithic
  child instead of reporting the actual startup phase, hold reason and verified progress.
- Presence is intentionally held for five minutes after physical input. Preserve that rule;
  distinguish a hold from failure/backoff. Never park a lease-holding helper at an interactive read.

## Required behavior

One concrete observation-driven startup/recovery path in crowley_automation, used by the public
launcher and scanner-aware host. Scanner remains independent; no scanner imports in automation.
Observe current state on every transition; an existing healthy game must be reused without a kill
or a second game/Battle.net. Process identity binds uid, exact argv0, Wine prefix and process start
identity, not PID/class/name alone. Multiple/foreign/ambiguous candidates block with a clear reason.

Reconcile these states with bounded deadlines and fresh postconditions:
1. Desktop session/presence/operation ownership available; do not guess Xauthority or bypass holds.
2. Existing verified game: repair outer/client fullscreen geometry in place, regardless of who
   started it. Repeat only the same idempotent WM request within a short verification deadline.
3. No game: reuse exactly one matching Battle.net, or start it once with saved login. Never create
   a second instance or reconnect a conflicting account. Distinguish login required, account
   conflict, update/install/busy, recognized dismissible news, Play ready and unknown UI.
4. Dismiss only positively identified benign news UI with a freshly located close control. Do not
   click arbitrary X/blue regions, dialogs, login, account conflict or purchases. Verify dismissal.
5. One guarded human-style Play click, then observe process appearance. A missing callback or slow
   start must not cause blind repeated clicks. Bound retries to observed still-ready state.
6. Explicitly identified expected character/realm selection enables one guarded Enter World action;
   loading/queue/dialog/unknown/no-HUD alone never enables Return. Observe change before another
   action. Native current identity/build/frame establishes in-world readiness.
7. Host resumes existing target/binding/AH/scan/save/upload/ACK policy after readiness. A fresh valid
   in-world client must never pass through unnecessary startup input or have an active sweep reset.

Every input and WM/process-changing action checks the canonical GUI lease, physical presence,
current owned process/window/focus and global automation maintenance/uncertainty interlock at the
actual action boundary. Clicks retain existing seeded curved Fitts motion, real press/release,
>=1 second global rate gate and durable evidence. Never perform protected market effects. Input
capable children must be bounded, noninteractive, safely cancelled on stop and unable to outlive
ownership; preserve actual game/Battle.net processes on host stop. No broad process-group kills.

Status must explain current stage, reason for waiting, elapsed/deadline, retry timing and native
readiness separately from dispatch. Sanitize account text and screenshots. Hold/stop/failure and
session/PID replacement invalidate action evidence. Do not renew or circumvent seller authorization.

## Design decisions to resolve before editing

- Prefer replacing the procedural shell with a thin exec wrapper for one Python startup path.
- Identify a concrete trustworthy source for pre-world/Battle.net UI facts. Tesseract is installed;
  if used, make it one explicit checked dependency, with fresh image provenance, strict exact text
  and geometric corroboration. Do not add multiple interchangeable OCR/vision engines or depend
  on a network LLM. Synthetic tests alone do not establish live UI recognition.
- Keep existing strict standalone automation configuration. Add explicit product facts only if
  needed; host renders them from common target without scanner catalog resolution in automation.
- Keep host lifecycle ownership small. Prefer a short deterministic recovery step/status over a
  new supervisor/service/queue. Define public API and state ownership in this document before
  parallel workers edit producer/consumer files.
- Preserve applied SQL, scanner wire/scan plan, seller behavior and original SavedVariables.
  New UI recovery must not weaken automation-busy or native ambiguity interlocks.

## Work order and review gates

1. Parent inspects source; separate read-only Sol review of this plan and source failure matrix.
   A read-only Luna audit checks installed/source mismatch, local UI evidence availability and a
   minimal reversible deployment path. No service/game mutation during investigation.
2. Parent records final API, exact disjoint implementation ownership and acceptance tests here.
   Luna automation lane implements concrete startup/UI/process/window behavior; Luna host lane
   integrates policy, loop, stop/status and rendered launcher/config/unit ownership in parallel.
3. Each lane runs focused pytest -n0, scoped Ruff/format and full Pyright. Parent reproduces; separate
   Sol reviewers inspect actual diff, invariants, behavior and tests. Fix concrete findings and
   re-review substantive changes until READY; never substitute a route-only success for review.
4. Parent runs full default suite, artifact/import/installed CLI help checks, Ruff/format/Pyright,
   target generation and relevant process/desktop recovery simulations. Build rollback and reviewed
   coordinated desktop artifacts. Installed legacy crowley-scan is not fixed by source edits.
5. Prepare a concrete reversible cutover/qualification recipe from actual installed config. Keep
   running scans undisturbed during development. Do not change production DB/server or trade.
   Before a live cutover preserve accepted saved scan/ACK at a quiet boundary and use only the
   local-game-control workflow; live operation is delegated as one bounded outcome. Do not reboot
   the machine or log out/change accounts as a test without explicit user authorization.
6. Verify and report source tests separately from installed behavior and live qualification.
   Retain private historical .archive/dist artifacts. Clean exact task temp files/worktrees only.

## Acceptance matrix

No game/no Battle.net; reused online Battle.net; benign news modal; login/account-conflict/unknown
modal; update/download/busy; delayed Play/process start; existing decorated/maximized game;
reapplied Wine hints; valid already-fullscreen world/AH/sweep; expected/wrong/ambiguous character
and realm; character selection/loading/queue/disconnect/no-HUD/malformed frame; stale captures;
PID reuse/process/window/focus change; human takeover before and during every action; GUI contention;
active/uncertain seller effects; desktop session/auth absence; stop/timeout/parent death during
startup; control-child lifetime/orphans; phase-specific bounded retries; restart must reobserve,
not replay old input. Unknown UI is a visible bounded refusal, never a guessed click or long silent
relaunch cycle.

## Frozen implementation contract (after independent Sol design review)

The design review identified all three observed paths plus four-glyph Play false positives,
window/process replacement races, lazy graphical-session initialization and incomplete control-child
cleanup. The concrete solution below supersedes tentative suggestions above. Keep exactly one
in-process recovery controller, one explicit OCR engine, and the existing canonical GUI lease.
No new daemon, workflow framework, startup-owner lock or second execution ledger.

### Measured UI correction (2026-11-17)

The real selected-build character-selection capture shows the central selected label
`Dandune Hillmont`, repeated in the highlighted right-hand row. It displays no realm label.
Do not interpret `Hillmont` as a realm or infer its meaning. The required explicit field is
therefore `character_select_name`, replacing the provisional `character_select_realm` in
both HostConfig and AutomationConfig. The classifier accepts `expected_selected_name` and
requires that full literal at the central selected-name position, with the verified enabled
Enter World control. A matching name elsewhere in the list does not authorize entry.
Native `character` and `realm` pins remain unchanged and mandatory before world readiness or
host input. Pair the captured selected label with the independent native identity from the
same process after entry before live qualification. There is no UI-realm bypass or invented alias.
This measured correction supersedes earlier provisional requirements for a visible realm.

### Measured informational-window contract (2026-11-17)

A single application-menu inspection opened a separate, same-process `Battle.net Updates`
window. A later read-only XWD capture of that exact window shows a fully loaded release-information
page: `Battle.net Updates` heading, `PATCH 2.53.4`, release date, article, and the normal unique
upper-right close X. It is a 900x600 window, distinct from the `Battle.net` main XID, same exact
PID/start/prefix/UID and class. WM_DELETE_WINDOW is supported; WM_TRANSIENT_FOR is absent.
Do not assume a transient-parent hint. Do not equate arbitrary update/progress dialogs or a
bare What's New heading with this qualified informational page.

Handle this measured informational window before attempting the main page's Play control.
Require exactly one owned main and at most one exact-title information window; foreign or
multiple candidates block. Activate and freshly capture the information window if needed.
Require the measured panel geometry, exact heading, PATCH version/date layout and unique
close control before one guarded Fitts close click, then observe that exact window disappear.
Keep the main Battle.net process/window alive; never click content controls or an embedded
article screenshot. The same canonical lease, parent-death control lifetime, stop checks,
post-motion evidence and episode deadlines apply. A loading/unknown panel waits visibly within
its existing budget and authorizes no guessed close or Play behind it. A replacement information
XID after a dispatched close is a changed postcondition, not permission to replay.

The original incident's popup was not retained. This actual measured release-information window
is the qualification target; unsupported future dialogs remain explicit holds. Keep the already
open information window in place for the coordinated cold-start installed qualification.

### Shared automation producer API

`crowley_automation.gui.process.ProcessIdentity` is an immutable dataclass:
`pid:int, start_ticks:int, uid:int, argv0:str, wine_prefix:str`. `OwnedGameMatcher.identities()` returns
all exact current matches as a tuple; existing pids() remains available. A mutation verifies the
entire originally observed identity immediately before effect. Multiple matches always block.

`crowley_automation.desktop_session.DesktopSessionProvider(display)` exposes `current()` returning
an immutable `DesktopSession(environment:dict[str,str], fingerprint:str)` or raises a typed
`DesktopSessionUnavailable(reason)` with a sanitized stable reason. Resolve the current graphical
allowlist from structured user-manager D-Bus Environment (`busctl --user --json=short get-property`),
validate configured DISPLAY/current readable owned Xauthority and a harmless X query. Never source
show-environment shell text, log its full result, glob auth files or freeze startup on old process
environment. The host and controller share this provider. Environment/fingerprint changes retire
all action evidence and pending chat transactions; adapters refresh lazily. No session at boot
means an explicit held desktop_session state and automatic reobservation, not process exit loops.

`crowley_automation.startup.StartupRecovery(config:AutomationConfig, *, stopping:Callable[[],bool],
session_provider:DesktopSessionProvider|None=None, ...test injection...)` exposes:
- `active:bool`: this object holds the canonical episode GUI lease.
- `step()->StartupStatus`: fresh observation, at most one bounded transition/action; never sleeps
  through phase waits and never launches a supervisory child.
- `request_restart(identity:ProcessIdentity, reason:str)->StartupStatus`: host's explicit established
  scan-failure path only. Freshly guarded normal WM close of that exact client, then observe exit
  before ordinary startup. No SIGTERM/SIGKILL of game/Battle.net and no close of a replacement.
  Invalid geometry/unknown UI are never reasons to request this. A close that does not complete
  within its bounded deadline fails visibly; no forced-kill fallback.
- `close()->None`: idempotent exact control-child cleanup; leave real game/Battle.net alive.

`StartupStatus` immutable dataclass fields (no raw OCR/account/chat text):
`outcome:Literal['ready','waiting','held','blocked','failed','stopped'], phase:str, reason:str,
action:str, started_at:float, deadline_at:float|None, retry_at:float|None, native_ready:bool,
process_identity:ProcessIdentity|None, window_xid:int|None`.
Times in status are wall-clock timestamps; control deadlines use monotonic time internally.
`action` means dispatched, never inferred completion. Keep phase/reason fixed and documented.
`wm_close_dispatched` is the exact action value enabling the host to commit its restart attempt.
The normal-close reason allowlist is no_frame_after_running (only with fresh independent native
world proof), ah_open_failed and no_progress. Missing frames before world entry and failed focus
activation remain recovery/hold states; neither authorizes closing an unknown or healthy client.
Suggested phases: desktop, ownership, battle_net_login, battle_net_ready, news_modal, play_ready,
game_start, game_window, character_select, world_loading, native_ready, game_close.
`native_ready` is a fresh identity/build-matching independent automation publication, not scanner
readiness. Promote desktop_cli's private native-frame observation into reusable automation code.

The controller owns the existing GUI lease across a bounded, noninteractive startup episode,
including process/world waits. Initial acquire is nonblocking/short; a competing caller reports
held/gui_busy. Release on ready/held/blocked/failed/stopped, after cancelling/reaping controls and
retiring evidence. Hold consumes no action attempt or relaunch backoff. Overall episode deadline
600s; individual window repair <=30s, Play process-start wait >=20s before a still-ready retry,
login <=180s, world loading <=300s, close <=30s. At most three positively revalidated Play attempts;
Enter World requires the verified selected display name/control and a postcondition, not blind
Return polling. Observed UI oscillations cannot renew the episode deadline indefinitely.

### Child lifetime and actual input

Add a small private `gui.control_child` Python exec shim and synchronous cancellable runner.
Direct child sets Linux PR_SET_PDEATHSIG(SIGKILL), then verifies original parent PID/start ticks,
then execs the reviewed nonforking control executable in the same PID. No preexec_fn. Pass the
pinned memfd and GUI lease fd; the child retains the lock until it dies on parent death. Poll
stop/deadline while communicating. Cancel and reap that exact child before unlocking; never kill
its process group. The runner is restricted to known control commands, not a public arbitrary
GUI command surface. Real Wine/Battle.net applications use ordinary detached Popen with no lease
fd or parent-death signal. Retain systemd KillMode=process; prove controls disappear separately.

Motion and press/sleep/release remain separate pinned XTEST emissions. On graceful cancellation,
attempt only releases for controls conservatively recorded as possibly down, under the still-held
lease and a small independent cleanup deadline. Record cleanup failure. Stop authorizes no new
press, pointer motion, activation or retry. Do not claim SIGKILL releases keys or retracts already
queued events: supported guarantee is no later command in a delayed control helper after parent
death, plus bounded release-only cleanup on graceful stop.

### Host consumer integration

Remove LauncherRun and the host's blind launcher polling branch. Host calls recovery for no game,
invalid window or no fresh native world readiness, including a manually started client. While
recovery.active, run only its next step and report status; never ordinary scan input. After ready
releases the lease, reobserve and resume existing scan policy without resetting LoopState.
A missing scanner canvas with a fresh matching automation world publication is a scanner fault,
not character-selection evidence or permission for Return. Preserve independent native-busy guards.

Bind ordinary host actions to observation identity/window/native boot and revalidate under the
GUI lease immediately before effect. If presence/session/process/window/scan readiness/automation
ownership changes, do not send the action or consume its policy attempt. Retire stale chat tokens;
do not retimestamp old frames. Introduce only the small additional Observation facts needed for
this invariant. Invalid geometry requests repair; established prolonged scan failures can request
the exact guarded normal restart path. No unowned fresh PID enumeration passed to termination.

X11Window validates outer and single client rectangles including offsets and FULLSCREEN atom.
Host adapters must lazily use the shared current session provider and discard stale captures on
its fingerprint change. Status exposes startup's typed sanitized phase/reason/deadline/retry/action.

The rendered launcher is only `exec crowley-desktop startup`; all logic is in the same producer.
Strict AutomationConfig adds required `product`, `product_dir`, `character_select_name`. Host
renders product facts from common target and the selected-name literal from an explicit required
HostConfig `character_select_name` measurement. Keep existing native `realm` pin unchanged. Do
not infer/normalize a visual alias; capture and qualify the actual value before live cutover.
The wrapper resolves standalone config, so old target/path interpolation shell logic is deleted.
Doctor verifies exact launcher/config/unit/installed resource bytes and Tesseract+English support.

### UI evidence and qualification

Use the existing locked fresh capture, one explicit installed Tesseract TSV reader, exact words,
confidence and positive layout/control geometry. Reject malformed/oversized TSV, missing engine/
English, stale capture, ambiguous controls or unsupported layouts. No network vision service.
Replace four-glyph Play acceptance; reject Stop/Help/Open/News despite button resemblance.
Bind BNet windows to exact matching main process start identity/prefix/uid, even with --window.
Revalidate process, geometry, focus, control text/layout and target after pointer motion before
press. Only the verified What's New modal panel+heading+unique close control is dismissible;
unknown/account-conflict/login/update UI cannot become a guessed Play or X click.

Character selection needs the expected SELECTED display name (the center-bottom selected-name element,
not a name merely elsewhere in the character list) and enabled Enter World control. The Forever GlueXML source explicitly positions selected name above Enter World;
the observed UI has no realm label. Require the independently measured full selected label,
then verify native character and realm before declaring ready. Qualify the actual screen before hardcoding layout.
Use the verified enabled Enter World button with the same guarded Fitts click path as other UI
controls. Do not use Return as a default-dialog shortcut. The supervised legacy capture operation
may use its separately visually verified one-shot Enter World key; that is not production policy.

No reliable current startup image fixtures exist yet. Perform a bounded supervised capture at a
quiet saved/accepted/ACK-written boundary, preserve correct build/native identity, capture real benign
news/normal Play/expected character-selection evidence, restore the existing scanner afterward.
No logout/account change/machine reboot. Keep raw account-containing screenshots private; retain
only sanitized relevant crops or OCR/control fixture data with original capture digest/provenance.
No generative alteration of evidence. Require real-image detector checks and paired character-
selection/native-identity evidence before calling auto-dismiss/Enter World live-qualified. Source
implementation may be reviewed before this capture, but this task remains open until qualification
and a reviewed installed-runtime cutover are actually resolved. Do not ship a bypass/trust toggle.

Latest ACK loading may legitimately await the next reload/boot; do not wait indefinitely or force
an extra reload to load it before closing. Record that stage separately and verify it after the
relaunch. A completed non-sweeping AH-closed state is also a quiet boundary because its timer
cannot issue new queries; a due_at of zero during AH reestablishment is not itself a failure.

Primary sources consulted for implementation contracts:
- https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_GlueXML/Mainline/CharacterSelect.xml
- https://github.com/Gethe/wow-ui-source/blob/forever/Interface/AddOns/Blizzard_GlueXML/Mainline/CharacterSelect.lua
- https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html
- https://man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html
- https://docs.python.org/3/library/subprocess.html#subprocess.Popen

### Exact disjoint ownership

Automation Luna lane may edit only:
- packages/automation/src/crowley_automation/startup.py (new), startup_ui.py (new), desktop_session.py (new)
- packages/automation/src/crowley_automation/gui/control_child.py (new), gui/process.py
- packages/automation/src/crowley_automation/desktop_observe.py, desktop_input.py, desktop_cli.py, config.py, battle_net.py
- packages/automation/src/crowley_automation/resources/launcher/crowley-world-of-warcraft.sh.in
- tests/test_startup.py, tests/test_startup_ui.py, tests/test_control_child.py, tests/test_desktop_session.py (new)
- tests/ops/test_battle_net.py, tests/test_desktop_cli.py
- tests/automation/test_backend.py (AutomationConfig fixture required fields only)
No host/scanner/common/Lua/native-vendor/SQL/docs changes. Public contract changes require parent update.

Host Luna lane may edit only:
- apps/host/src/crowley_host/scan/{policy,act,loop,observe,cli,config}.py
- apps/host/src/crowley_host/config/host.py
- apps/host/src/crowley_host/ops/{install,doctor}.py
- apps/host/src/crowley_host/ops/package_data/config/host.example.toml
- apps/host/src/crowley_host/ops/package_data/systemd/crowley-host.service.in (only if needed; preserve game lifetime)
- tests/test_scan_{policy,act,loop,observe,cli}.py (existing files only), tests/ops/test_{install,doctor}.py
- tests/config/test_resolver.py, tests/support/config.py, tests/packages/test_host_distribution.py if new strict host fields require it.
No automation/scanner/common/Lua/SQL/docs changes. Coordinate producer APIs through this contract.

Parent owns docs/skills, final fixture sanitization/provenance and integration checks/git. A separate
live operator owns only its specifically delegated bounded capture/cutover outcome, never source
implementation. Workers run focused pytest -n0, scoped Ruff/format and full Pyright; distinguish
concurrent integration failures. No commits, stashes, resets, worktree/production changes by coding
workers. Do not revert others' edits. Real code and regressions, no placeholders or broad scaffolds.
