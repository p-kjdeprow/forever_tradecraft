---
name: local-game-control
description: Operate and diagnose a running game on a local Linux desktop, including Wine and launcher windows, when the user authorizes game input. Use for live addon checks and game-window troubleshooting; excludes addon implementation and trading automation.
---

# Local game control

Use this skill for authorized desktop interaction with a game. Preserve the user's
current objective and model preference. A diagnostic task does not authorize trading,
account changes, or unrelated display/streaming changes. Resolve routine interactions
within existing authorization; respect any current movement limits.

## Establish the actual target

- Reused workers can retain an earlier worktree as their working directory.
  Bind live diagnostics and source references to the current absolute repository
  path and accepted hashes before using their conclusions.
  Native fixture proofs must also bind the imported module and function code-file
  paths; a correct working directory can still load an installed older package.
  Reuse production identity/capture helpers. Keep standalone Python helper names
  distinct from imported standard-library modules, such as `inspect`.
- Check processes and map window IDs to PIDs before launching anything. A running
  game behind its launcher needs activation, not another click on Play. Match exact
  process comm/Windows argv0 and PID/start; broad command-text searches can match
  the diagnostic shell itself. Never infer
  that the game stopped merely because Battle.net is visible.
  Match fixtures to the exact command and flags: `wmctrl -lp` emits XID, desktop,
  PID, client machine, then title. Desktop can be `-1`; omitting it in a fixture
  conceals a broken process/window certificate parser.
  In `xwininfo -tree`, the queried window is named in the header; indented
  structural rows describe its children. Root/Parent diagnostic lines do not
  prove ancestry. Use captured output rather than inventing a root row.
  For an owned GTK target that must publish an XID, select GDK_BACKEND=x11
  in that child's environment; DISPLAY alone does not select GTK's backend.
  A published XID or wmctrl row does not prove mapping and WM reparenting have
  settled. Retain startup ownership through bounded readiness of the exact
  process/window. Root is never its certified frame; diagnostic capture must
  not enumerate unrelated root children when the owned frame is still absent.
  An undecorated GTK client can remain a direct root child with no frame extents.
  Pin the observed layout explicitly through owner, ticket and sealed evidence;
  never interpret failed framed verification as permission to switch layouts.
  Exclude InputOnly metadata helpers from event targets in either layout.
  Distinguish the Wine outer window from its rendering child before forcing a
  display size. Compare both measured geometries with the existing production
  resolver and profile; a larger outer window can contain the exact desired
  child. Inspect WM_NORMAL_HINTS and allowed actions when fullscreen is ignored.
  For owned pointer-probe windows, prove the observed pointer lies inside the
  actual mapped client after focus. Placement/decoration hints alone do not
  prove this; retain before/after queries without repositioning it for the proof.
  Rebind a measured supported outer profile rather than changing correct client
  dimensions or repeatedly requesting an unsupported window action.
  Validate published identity before treating missing companion facts as startup
  pending. Test mixed invalid-plus-missing cases at the actual lifecycle boundary;
  a broad base-exception assertion does not prove an immediate hard refusal.
  Verify installed command flags with a harmless actual invocation before
  encoding them in fixtures; xwininfo -stats prints geometry, while -geometry
  is not a supported option on this host.
  Keep Wine identity channels distinct: `/proc/<pid>/exe` can identify the Linux
  loader while `comm`/status `Name` identify the Windows program. Verify the
  Windows command line and prefix separately; do not loosen one check to fit another.
  Windows argv0 uses Windows path semantics; POSIX `Path.name` does not extract
  its basename. Prefer the production resolver over another local parser.
- Focus alone may leave a Wine window covered or minimized. Raise/activate the
  verified game window, then inspect a fresh screenshot before entering input.
  Launcher news, trailers, and broadcasts are not evidence of game state.
  A GNOME system modal can dim the binary HUD and ignore Xwayland XTEST input.
  Verify its installed Cancel binding before one owned native dismissal; uinput
  is system-wide and is not isolated by an Xvfb DISPLAY. Revalidate the game scene
  afterward: an AFK logout invalidates any previously staged chat command.
- Use current screenshot dimensions and visible controls. Coordinates from a
  previous resolution or window position are stale.
  A known prior safe world pose can guide a fresh observed approach after accidental
  movement; it is distinct from stale screen coordinates. Check actual range and
  clear floor before investigating another input device when NPC interaction fails.
  Floating NPC names locate a character but do not establish its clickable body.
  Verify the actual model/tooltip before interaction; an equivalent auctioneer
  in the same auction house is sufficient when the selected one is obscured.
- A worker shell can lack the graphical environment while the game is running.
  Obtain `DISPLAY` and `XAUTHORITY` from the verified game or graphical session
  and pass them to capture/query tools and fresh owner launches. Verify that a
  replacement owner inherits them or binds the exact authority path in its profile.
  Pass the production input environment to watchdog and emitter children too;
  configuring only the parent capture path does not configure those subprocesses.
  Do not dump the full process environment
  or treat a missing shell variable as evidence that the desktop is unavailable.
- Persistent desktop notifications are historical evidence. Before attributing a
  current game problem to memory pressure, check fresh available memory/pressure
  readings and correlate the event time; do not infer a current shortage from
  an old notification alone.

## Make input observable

- Verify the foreground screen before a click or command; inspect the result before
  the next dependent action. Tool exit code zero proves input dispatch, not success.
  A 1 Hz HUD can retain the previous phase or return NO_CHANGE after dispatch.
  Poll within the original deadline for fresh effect evidence; do not replay the
  action, restamp old evidence, or clean up merely because its first frame lags.
  Before starting an in-game calibration/recovery helper, inspect a fresh full
  scene as well as process/focus identity. A disconnected login screen can retain
  the same Wine identity; its background is not evidence of a chat classifier bug.
  A paused or restarted daemon does not prove the game has foreground focus.
  Verify configured pinned identity before calibration preflight; if focus differs,
  restore it through the guarded owned tool and verify before requesting admission.
  Resolve actual controls rather than guessing a keybinding from an action name.
  Stage and verify commands before acquiring short-lived recovery tickets;
  ticket expiry permits the existing public recovery retry, not a new approval
  requirement. Complete a bounded owned chain without model-turn delays.
  Recovery may intentionally have no telemetry surface. Use the public daemon
  recovery admission and fresh verified scene/chat/control evidence for that
  boundary; requiring an ordinary decoded HUD can make recovery unreachable.
  Never invent ordinary fields or treat a decoder error as a successful frame.
  Stage deferred reload confirmations before a timed no-addon baseline;
  checkbox selection does not load the addon until reload. Verify absence,
  then use a bounded helper to cross readiness without a model-turn delay.
  For xdotool key/type/click, an explicit `--window` uses XSendEvent; verified-focus
  calls without a window stack use XTEST. Calibrate and observe the same injection
  path used at runtime instead of treating those mechanisms as interchangeable.
  Absolute mouse motion is version dependent: installed xdotool may use
  XWarpPointer even while keys/buttons use XTEST. Verify the actual tool/library
  mechanism before blaming the desktop or changing event attribution rules.
  When binding a private input tool, cover calibration, runtime, ordinary release
  and the independent watchdog together. A changed executable path must not
  silently enter a generic helper branch that omits exact key/button releases.
  Parse retained command output in device-profile regressions too; separate
  axes can repeat the same class label. Do not invent a distinct class per axis.
  Build input-parser fixtures from a captured trace. The measured XI2 output
  identifies `device: <deviceid> (<sourceid>)`; the first ID may be an attached
  slave, so resolve its role from the measured hierarchy. Invented `sourceid`
  lines do not verify the host parser or takeover monitor. Attached XTEST slave
  Motion can carry the prior cursor in root/event fields and the new endpoint
  in delivered valuators; verify the source-defined transition and mapping,
  preserving both fields rather than treating raw data as endpoint evidence.
  Update every endpoint consumer together, including readiness predicates,
  proof-helper waits, calibration, runtime correlation and receipts. Tests must
  traverse the native argv gate and actual producer/seal/arm path, including
  the configured capture admission from paused startup; replacing those methods
  can conceal a broken command schema, state gate or incomplete evidence.
  Keep measured phase-start evidence immutable and separate from callback cursors.
  Exercise callback delivery before final validation; prepopulating only the
  initial state can hide a failure caused by the real lifecycle's state updates.
  Map each accepted refusal and ordering requirement to a regression at its
  actual boundary. Assert no input on refusal and record ordering explicitly;
  a matching exception alone does not prove it happened before dispatch.
  For sealed evidence, recompute the fingerprint in semantic-negative tests so
  a generic integrity failure cannot hide missing structural validation.
  Keep other invariants valid so the mutation reaches the intended guard;
  assert that guard's error rather than accepting an earlier unrelated refusal.
  A runner may save owner evidence before validating it. Assert the runner's
  terminal error as well as the nested owner error, so schema masking cannot pass.
  Preserve complete raw blocks including detail, flags and accelerated/raw
  valuator pairs. An early partial parse cannot prove that a field was absent.
  Reject malformed complete blocks at their native delimiter and retain verbatim
  observer text plus the exact parser error, including on failed proof runs.
  On a synthetic calibration timeout, retain the exact predicate rejection
  alongside that text before cleanup discards it. Window/process polling
  supplies lifecycle context; it cannot identify the rejected input event.
  A diagnostic's partial final block can already have yielded a semantic event.
  Replay its exact raw blocks plus partial tail through the production parser
  before attributing missing transitions to producer buffering.
  Use XI2's CARD32 time domain consistently in runtime and calibration consumers;
  test queued raw/primary interleaving as well as direct pairing helpers.
  An advertised XTEST device does not prove source attribution. Require an
  actual primary trace and strictly paired forwarded echoes; a stream containing
  only another device's motions cannot qualify by whitelisting that device.
  A corrected emitter can expose additional raw event classes. Diagnose the
  first rejected native record before attributing later coordinates to the
  compositor. Raw motion alone cannot establish a delivered target or endpoint.
  Exercise the live stream at idle: its final release must be delivered without
  waiting for a later event or EOF. Prove target ancestry from window-tree
  relationships; root/parent IDs appearing in diagnostic text are not descendants.
  Determine completeness per event class: XI2 crossing events can print their
  window and modifier fields after device and time. Do not emit a partial event
  merely because the fields needed by another event class have arrived.
  Root-level XI2 delivery can name the managed window frame enclosing a GTK
  client. Certify that frame's unique relationship to the owned client and its
  process; do not guess it is a descendant or permit arbitrary parent windows.
  A Mutter frame can also contain its advertised InputOnly user-time window.
  Verify the property, process and structure; that metadata helper must never
  become an allowed input target.
- Verify native process dispatch with a harmless real command before GUI input.
  Standalone helpers using the multiprocessing watchdog must put startup and all
  input under a guarded `main`; spawned children import the entrypoint again.
  Run them from an importable script file, rather than stdin or `-c`, so spawn
  can reload that entrypoint.
  A fake `Popen` misses exec-handshake deadlocks. Never wait for parent admission
  or apply simulated input effects inside `Popen` construction; effects belong
  after the real admission gate of an owned harmless child.
  Never wait in `preexec_fn`; an executed wrapper can wait after `Popen` returns. Close its
  admission pipe on cancellation and exit on EOF. Only an explicit admit token,
  sent after process registration and held-state publication, may permit exec;
  owner death closes the pipe too, so EOF cannot mean permission.
  Initial watchdog readiness is insufficient: require live state ownership at
  each admission, and preserve complete pipe-message framing through owner death.
  A live wrapper PID or reader thread is not executable readiness. Verify that
  the registered process has actually executed the intended observer before
  arming input, and retain ownership through confirmed termination and reaping.
  Live proof helpers use the production desktop lease; private lease paths are
  for isolated tests and cannot establish exclusive ownership of the desktop.
  Check ownership/mode on the actual configured producer after a container build;
  a user-owned fixture copy can conceal root-owned staged output. Correct install
  metadata with the pinned bytes unchanged; never relax the executable guard.
  A private D-Bus session can still inherit standard service directories and the
  host activation environment. Disable auto-start on fixture RPCs (for example,
  `busctl --auto-start=no`); a late call must fail without starting a host app.
  A private bus address alone does not isolate service activation from the GUI.
  Negative RPC checks must prove the expected D-Bus error, not an unsupported
  CLI command or usage failure; verify the same transport succeeds when owned.
  Before a broad namespace test run, prove the real sealed executable can exec
  there. Namespace-only EACCES can affect both capture and input images; retain
  the failure and compare an owned host fixture before attributing a regression.
  Check both process reaping and empty Guardian registries during cleanup.
  Retain handles and final summaries for long checks; collected test counts or
  silence after a tool session ends do not establish a completed passing run.
  Preserve physical left/right keysyms when serializing a sealed keyboard profile;
  collapsing them to a modifier alias can contradict exact held-key ownership.
  Planner and argv-builder tests do not establish executor behavior. Exercise
  the actual key/camera driver entrypoint and ordered observer completion;
  model the external process/events rather than replacing the ownership gate.
  Keep required calibration keyboard operations aligned with the current runtime
  vocabulary. An unused calibration-only chord can introduce native modifier
  transitions that no runtime action needs; verify exact vocabulary coverage.
  Named modifier keys can trigger xdotool's own modifier compensation even in
  explicit down/up chains. Verify the native trace; measured physical keycodes
  avoid that implicit compensation while preserving exact held-key ownership.
  Human takeover tests must start with the actual raw event, then delivered
  events. Guardian can revoke and stop the observer before the latter arrives;
  a delivered-only fixture cannot prove the real human step works.
  For diagnostic format changes, pass actual persisted producer receipts through
  the public evaluator with bindings derived independently from known fixture
  files before the run. Separate synthetic evaluator tests and bindings copied
  from the receipt cannot prove compatibility or detect false provenance.
  Motion-planner checks must include bounded over/undershoot, not only exact
  nominal travel. Minimum pulses and cumulative uncertainty can make an apparent
  in-range target infeasible; refuse it before input rather than increasing bounds.
  A failed owned-target bootstrap must reap and unregister its process so a
  fresh attempt on the same driver can start without rebuilding the session.
  Retain every owned resource before recording optional identity/diagnostics.
  Mirror native ctypes structures against the installed C headers, including
  field widths and alignment; verify read-only state queries on a private display.
  Exercise new receipt/image-stat code on retained frames before input. A failure
  after dispatch must preserve the completed action and never cause its replay.
  Build diagnostic helpers from the actual profile and cleanup interfaces;
  verify field names and resource ownership before a live run. A nonexistent
  cleanup method can mask an otherwise completed capture or button release.
  Test exceptions at the actual failed setup stage and after context return;
  a cleanup test at another stage does not prove that ownership transfer.
  Prove automatic executor cleanup through its actual lifecycle failure path;
  manually closing the input afterward cannot prove its finally block ran.
  Bounded diagnostic retention must also remain bounded when eviction fails;
  skip the new optional artifact rather than losing track of the old one.
  Before reporting quiescence, decode actual speed/current on advancing frames.
  Released controls and constant coordinates do not prove speed zero; a character
  can run against an obstacle. Refuse the boundary rather than infer a stopped pose.
  Accept movement measurements only from settled post-release telemetry.
  Check the reconstructed runtime direction against the independently measured
  vector at a nonzero facing; stored calibration values alone do not prove it.
  Keep capture time and receipt/admission time distinct; missing receipt evidence
  cannot be replaced by assuming the observation arrived when it was captured.
- Size a typing subprocess budget from actual text length and per-character delay,
  with bounded execution overhead. A timeout can interrupt a key between press
  and release; process exit alone does not prove released input. Reap the typer,
  release its owned emitted keys, cancel the editor, and verify a fresh clear
  state before retrying. Never submit a partial buffer after a timeout.
- For Wayland clipboard providers feeding Wine/XWayland, keep an owned foreground
  provider alive through verified paste. The installed wl-clipboard manual warns
  that `--paste-once` can break XWayland clients that make multiple requests;
  one GUI paste does not imply one protocol request. Restore prior clipboard bytes
  before reaping only the obsolete owned provider.
  Clipboard providers need the Wayland session environment (`WAYLAND_DISPLAY`
  and `XDG_RUNTIME_DIR`); an X11-only input environment is insufficient for them.
  Poll new selection readiness rather than checking immediately after startup.
  Use `wl-paste --no-newline` for exact bytes; default output appends a newline
  and cannot establish byte-identical clipboard preservation.
- When typing slash commands through a clipboard helper, inspect the final chat
  buffer. Opening chat with `/` and pasting a command starting with `/` can leave
  `//command`. Correct the actual buffer before pressing Enter. Do not risk sending
  commands as ordinary public chat.
  A buffer staged before a long wait is not current dispatch evidence. Immediately
  before Return, verify fresh addon `chatEdit` is active, focused and contains the
  exact command; then require the actual boot/file transition for a reload.
  A post-Return capture can still contain the old boot. Arm one owned decoder
  boot transition and test old-to-new through the real decoder; a no-op fake
  decoder cannot verify reload polling.
  Keep idle-scene admission separate from the owned chat phases: a guard requiring
  a closed editor cannot admit the expected focused empty editor after opening
  chat. Exercise adapted guards on retained phase snapshots before live input,
  including every context field the helper summary must retain.
  Load declared artifact pins from the verified machine-readable receipt and
  compare the original file bytes; avoid hand-transcribing hashes into helpers.
  Chat borders and insertion cursors can become OCR letters even in an empty
  editor. Retain original full frames and verify production cropping/OCR against
  empty buffers and every fixed command, including short commands; preserve
  refusal before input. Do not
  whitelist OCR junk to make a closed-buffer check pass.
  An inactive transparent editor can expose changing world graphics. Establish
  editor state before interpreting its body as text; preserve typed-command OCR
  when changing the closed-state guard. Check fresh short commands and moving
  backgrounds before promotion. Bind each artifact to its declared geometry;
  never crop an editor crop again as though it were a full frame.
  For a timed confirmation, stage a bounded local capture/recognition helper
  before Apply and complete Apply plus the measured confirmation within that
  helper. Model-turn latency can exceed the timer; an expired confirmation
  proves reversion rather than a committed change. Benchmark recognition against
  the actual saved modal before dispatch: full-screen OCR can consume the timer.
  Prefer a small measured visual template for a known unchanged confirmation,
  with foreground evidence and one-shot input; preserve a real negative match.
  A display change can move or resize the confirmation itself. Match its fresh
  measured transform, and retain per-poll capture times and match scores. A tiny
  final timeout may be the remaining overall deadline, not an initial capture
  failure; inspect the last image and elapsed time before changing the helper.
  Chain acquisition and consumption of a short-lived bootstrap proof in one
  bounded local helper. A model turn, commentary or unrelated screenshot between
  those commands can expire the proof; fix dispatch order rather than widening
  its freshness limit. Stage the helper before acquiring the proof.
  A readiness poll must stop waiting and dispatch the staged next action on its
  first matching signal. Do not finish a fixed polling loop before acting. Bind
  one action to each phase, retain its actual receipt, then wait for the next
  phase; a model turn cannot serve a short construction window.
  Exercise the helper's real entry path in a dry run as well as its pure guards;
  read-only preflight can pass while receipt initialization still prevents the
  first production command from being reached.
  Replay extra guards against the complete typed snapshot before a live attempt;
  summaries may omit context, and `context.clientBuild` is a string. When reusing
  functions loaded from a script, bind their actual module globals in that test.
  Bound diagnostic slash commands to both client and telemetry readback limits.
  Derive emitter and acknowledgement bounds from the pinned tool's actual
  per-character timing and fixed command length; an instant recorder does not
  prove real typing fits. Track the keysyms typing can hold before dispatch.
  Crowley's current `Agent.activeEdit` rejects text over 128 bytes by returning an
  inactive projection, even when the EditBox is visible; shorten the probe rather
  than diagnosing focus loss or weakening the proof. Desktop selection shortcuts
  and very fast typing
  are not established EditBox behavior. After malformed input, verify the whole
  buffer is empty before replacing it; Ctrl+U can leave a suffix after the cursor.
  Use verified paste or a measured typing rate and inspect both ends of a
  horizontally scrolled command before submission.
  Chat can double typed pipe characters. Keep probe markers plain; color escapes
  require actual client buffer evidence before use. Prefer retained visual readback
  over repeatedly redesigning a marker when OCR misses legible output.
  Slash-command success does not prove shifted punctuation works. Compare the
  entire actual buffer, including parentheses and quotes, before Return; cancel
  malformed input and retain the refusal instead of trying another live route.
  When a harness requires a new evidence directory, let the harness create it;
  store invocation logs beside that directory, not inside a pre-created one.
  Root pointer queries can return a certified outer window. Use the measured
  target_event_xids membership rule, not render-child equality. Normalize JSON
  coordinate lists and native tuples before comparing them.
  Pointer position can change during Wine capture. Compare actual positions before
  and after capture; a tooltip in the image does not prove the current pointer
  stayed there. Diagnose the change before any guarded final reposition/click.
  After the final scene capture/focus, reposition, query the current endpoint and
  target window, then press immediately; an intervening capture can reset it.
  A first post-click image can still carry the previous one-Hz addon payload.
  Wait for advancing, settled post-action frames before declaring failure or
  retrying an interaction.
- After a black capture, establish whether the game is loading, obscured, disconnected,
  or uncaptured. Compare capture paths when needed: on GNOME, scrot or XImage can be black
  while gnome-screenshot captures a healthy visible application. A failed
  capture path does not establish a hung client. Fresh capture and normal
  window switching can distinguish these.
  Do not keep clicking invisible controls or immediately restart the game.
  A screenshot CLI exit of zero does not prove the requested image is ready.
  GNOME's shared D-Bus screenshot service can return before file creation or PNG
  completion under competing requests. Verify a new fully decoded image, retain
  the original capture time, and serialize requests within the existing deadline.
  Give every retained poll a unique image path so repeated phase tags cannot
  overwrite evidence already bound to a receipt hash.
  Profile command, image completion and identity-query time separately before
  optimizing cadence. Screenshot UI effects can dominate even with zero delay;
  verify the installed implementation rather than guessing from total latency.
  For nested timings, subtract every included stage before labeling a residual;
  outer-minus-backend time can include identity checks as well as crop/decode.
  Count repeated identity and decode work across the complete consumer loop;
  a fast producer alone does not establish the publication sampling budget.
  Before retrying a skipped update, retain bounded per-attempt timing and the
  offending crop before normal cleanup removes transient evidence.
  Timing regressions must traverse configured backends and production hook
  call sites. Manual span contexts or stock-capture fixtures cannot qualify
  timing attribution for a configured producer.
  For modeled cadence tests, advance the publisher clock at actual production
  call boundaries. Subtracting a precomputed latency sum cannot prove the
  optimization works; the test must fail when that optimization is removed.
  Audit inherited fixture clock advances too; every virtual advance must be
  charged active work or an actual production wait, never hidden phase tuning.
- If an action has no visible effect, diagnose foreground, coordinates, and input
  delivery before repeating it. Repeated Play clicks can create duplicate clients.
  Another player's bar can physically hide an NPC label; OCR filtering cannot
  restore those pixels. For a stationary NPC interaction, a fixed `/targetexact`
  command with fresh exact chat readback can establish a target-frame name/level
  channel. Verify that fresh target frame plus the exact hover tooltip, and test
  positive/unrelated-target negatives before adopting it as the label alternative.
  Keep final identity/focus/pointer checks and owned button release unchanged.

## Recover without widening the task

- If an account-conflict dialog appears, check for duplicate local clients first.
  Map windows to PIDs and distinguish the original from an accidentally launched
  duplicate. Within existing authorization, close only the accidental duplicate
  normally and reconnect the original; do not kill every matching process.
  An expired in-client reconnect can show blank login fields while the launcher
  remains logged in. Within normal recovery authorization, verify the launcher
  session and close the old client gracefully; prove its PID/window absent before
  one cached Play launch. Ask for login only if that normal launcher path needs it.
  On cached reconnect, inspect the full scene between phases. A ruleset, realm or
  character chooser requires the already-authorized selection before world HUD
  polling can succeed; use bounded waits for actual loading transitions.
  A retained no-realms dialog describes an earlier connection attempt. After an
  agreed backoff, one authorized cached retry establishes current availability;
  a fresh screenshot of the old dialog does not renew the network observation.
  Correlate fresh launch logs with process/window lifetime. Token refresh or an
  automatically completed challenge is not evidence that user login is needed;
  establish that from the current visible login form or an explicit failure.
  Prefer the application's Quit control or `wmctrl -ic <verified-XID>`, which
  requests the window-manager close protocol (`WM_DELETE_WINDOW`).
  `xdotool windowclose` destroys a window without ending its client; it is not a
  graceful application exit, and a disappeared window does not prove process exit.
  A pre-existing conflict dialog can outlast the user's prior play session. When
  the latest handover authorizes normal login, one normal reconnect with the
  sole local client may resolve it. If the conflict recurs, report that fact
  rather than repeatedly displacing an unknown remote login.
- Honor the latest user control instruction. A temporary stop for revival or login
  ends when the user asks to resume. Continue already authorized routine steps;
  do not invent a new restriction against entering the selected character.
- Avoid navigation during stationary addon validation. If navigation is authorized,
  use short verified movements and stop on hazards or uncertain location. Never
  reuse movement plans based on a misidentified map or scene.

## Report and finish

Delegate an outcome such as scan, upload, and acknowledgement with clear boundaries
and completion evidence. Give the operator room to chain routine interactions,
helper repairs, and authorized recovery independently. The parent should review
milestones and resolve substantive blockers, rather than approving individual clicks
or repeatedly asking for updates while the worker is acting. Do not turn a temporary
precaution into a permanent restriction that prevents the requested outcome.
When only live execution is held, complete already-authorized offline integration,
verification and artifact freezing; do not stop unnecessarily at a draft candidate.
After promoting a candidate, check the canonical code and documentation against
every accepted review correction before freezing; draft-only fixes are unfinished.
Choose controller commands from the actual persisted state. A paused session can
require `resume`, with missing gates returning it to unarmed calibration; repeating
`start` with a new generation cannot fix a state-specific command refusal.
After native success, complete public pause/canonical ordinary recovery and prove
checkpoint clear before human-input startup. Read the durable journal checkpoint
when IPC status does not expose it; an absent field is not evidence of clearance.
Inspect actual target geometry and pointer at the failed boundary; post-cleanup
focus cannot establish its cause.

A planned collection cutoff should stop new scans while allowing authorized save/logout
cleanup. A user's explicit takeover or stop ends input immediately, including cleanup.

Execute the next justified action instead of repeatedly announcing plans. Send a
concrete result or exact blocker at least once a minute during active operation.
Give the parent the current screen, attempted action, observed effect, and next
diagnostic fact needed. Distinguish human availability from actual prompt readiness;
if startup fails, state that the human step never began and who owns desktop input. Do not end with an offer to continue work already requested.

For addon validation, distinguish command submission, an in-game success message,
saved output, server ingestion, and loaded acknowledgement. Report the stages
actually verified and preserve rejected data for the implementation worker.
An advancing HUD sequence does not make an old chat status line current. Confirm
a newly submitted status or decoded scanner fields before declaring a stalled scan.
Opening the AH can start Crowley's scheduled scan before a manual scan command.
Avoiding duplicate scan triggers does not forbid a bounded normal interaction
retry after a verified failed click with the AH closed and no active sweep.
Check status and actual start time before waiting or forcing another run; the
sweep timeout is a maximum duration, not a required wait after every command.
Before interpreting payload shapes or depth coverage, read
[crowley-client-contracts](../crowley-client-contracts/SKILL.md).

Maintain this skill by correcting guidance when an observed mistake reveals a
reusable lesson. Consolidate related lessons here; omit incident logs, transient
PIDs/coordinates, speculative rules, and one-file-per-mistake proliferation.
