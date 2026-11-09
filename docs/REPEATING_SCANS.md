# Repeating Crowley scans

**Current operating status: collection stopped.** The user has directed us to
assume the beta is over. The local session service is stopped and disabled;
do not follow the startup instructions below until collection is authorized
again. Existing data and evidence remain preserved. Live launch acceptance
of the autonomous cycle is still pending; see HANDOFF for the exact limits.

The local session controller collects the curated 428-item watchlist every 900
seconds, anchored to actual scan starts. It preserves the original SavedVariables,
waits for the companion upload and exact Data.lua UID acknowledgment, reloads,
verifies that UID in the loaded HUD, and reopens the AH for the next cycle.
Normal scanning and acknowledgment use deterministic routines. Luna handles
bounded visual recovery when the game disconnects or presents an unexpected scene.

See [HANDOFF.md](HANDOFF.md) for current deployment and live acceptance status.
This runbook describes the new passive-startup flow. The previous native/GTK
calibration instructions are preserved as
[historical diagnostics](../.archive/REPEATING_SCANS-legacy-calibration-20261022.md).
They are not prerequisites for normal scanning.

## Human play and the telemetry display

Pause the session controller before taking over the game. The upper-left square
is the addon telemetry display; `/crowley hud` toggles it. Use `/crowley hud hide`
or `/crowley hud show` for an explicit choice, and `/crowley hud status` to check.
The choice is saved across reloads. Hiding stops display updates; showing
publishes fresh telemetry at the original position and size.

This controls the display only. It does not pause the external controller or
change the addon's scanner settings. Agent operation requires the display to be
shown. Visibility changes are refused during an active native diagnostic
transaction. New Lua commands become available after the addon next loads;
loading this change does not authorize automation to resume.

## Installation and profile

Create and review `$XDG_CONFIG_HOME/crowley/host.toml` from
`deploy/templates/host.example.toml`. It contains the measured client window
geometry, account, character, display, game root, Wine prefix, and server URL.
The tracked `session.toml` and `companion.toml` files contain tunables only.
The resolver derives SavedVariables, companion state, Data.lua, Queue.lua,
the journal, runtime socket/captures, and packaged routes. Keep the journal under
the target/account/character state namespace; it holds durable
scan intents, acknowledgment evidence and the takeover record. Do not replace it
with an empty journal to clear a blocker.

The current client is Arthur, Classic Beta PvE 2, Alliance, build70450/interface
16001. The deterministic AH opener expects Auctioneer Jaxon in Stormwind City
within native interaction range. Positioning/navigation is separate from this
routine. The schema4 HUD publishes the target, range and CTRL-F12 binding facts
needed to verify that interaction.

Enable the game's **Enable Interact Key** option during setup. Forever's
[controls implementation](https://raw.githubusercontent.com/Gethe/wow-ui-source/forever/Interface/AddOns/Blizzard_SettingsDefinitions_Frame/Controls.lua)
sets `SoftTargetInteract=3` for that checkbox; its
[native enum](https://raw.githubusercontent.com/Gethe/wow-ui-source/forever/Interface/AddOns/Blizzard_APIDocumentationGenerated/SoftTargetConstantsDocumentation.lua)
defines1 as gamepad-only and2 as keyboard/mouse. The controller reads this mode
and refuses a disabled or unavailable keyboard interaction before pressing the
chord. Keep the prior setting in the setup receipt when changing it.

Install the package and render the reviewed runtime files into the XDG
configuration directory:

```sh
crowley-native build xdotool
uv tool install .
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
crowley-ops install
# Review host.toml, the tunable session.toml/companion.toml and secrets.env.
# Keep CROWLEY_API_TOKEN, CROWLEY_DB_PASSWORD and CROWLEY_DATABASE_URL only in secrets.env.
crowley-ops doctor
```

The input-tool builder needs `cc`, `make`, `readelf`, `objdump`, `libx11-dev`,
`libxtst-dev`, `libxinerama-dev` and `libxkbcommon-dev`. It verifies the source and
published executable. The input owner and independent release watchdog share its
sealed executable image.

The model adapter pins Codex CLI `0.162.1` and `gpt-6-luna`. The session loader
requires the provisioned executable through the absolute `CROWLEY_CODEX_PATH`
runtime environment value; it is not a target or TOML identity fact. A mutable
global installation can update beyond that pin. The configured version must
match; the controller does not select another model or executable on failure.

The host contract owns the display, window title, Wine prefix, and measured
client geometry. `XAUTHORITY` remains an environment fact supplied by the
graphical session rather than a tracked configuration value.

## Startup and one-time takeover drill

Log into Arthur, leave the game idle near the auctioneer, and read status:

```sh
crowley-session --config ~/.config/crowley/session.toml status
```

Each mutating command requires the current generation from status. For the first
start with no pending scan work:

```sh
crowley-session --config ~/.config/crowley/session.toml start --mode repeat --generation <current-generation>
```

Start checks identity/focus, the measured input environment and native keymap,
held controls, the registered XI2 observer, and three advancing ordinary HUD
frames. These checks send no game input. Native memory measurement, addon resets,
GTK targets and typing the command vocabulary are optional diagnostics.

If status requests a takeover drill, run it while the operator is present:

```sh
crowley-session --config ~/.config/crowley/session.toml takeover-drill --generation <current-generation>
```

The drill makes a small curved hover without clicking and then waits for genuine
input. Poll status from another terminal; `takeoverDrill.state = awaiting_input`
and `nextAction = provide_human_input` mark the accepting window. The operator
taps and releases Shift once. A passing drill proves pause/release, saves the environment-bound record
and leaves automation paused. Start repeat mode using the returned generation:

```sh
crowley-session --config ~/.config/crowley/session.toml start --mode repeat --generation <after-drill-generation>
```

The record survives controller restarts and ordinary addon reloads. A changed
input hierarchy, native tool, keymap, display or takeover contract requires a new
drill. Process IDs and routine addon boot changes do not invalidate the stored
human evidence. Each new owner still collects fresh passive readiness.

Fresh passive readiness also carries the addon's current boot epoch. If an idle
logout and world re-entry changed that boot while Crowley was paused, `start` or
`resume` adopts the new epoch, resets the durable frame sequence/CRC baseline,
and archives an `external_boot_change_adopted` intervention with the old and new
boots. This is allowed only when no action or recovery fence is active; scan
work, native calibration or construction recovery, reload waits, qualification,
and an unresolved `save_reload`, `ack_reload`, `enter_world`, or `logout`
transition remain fenced and block an unexpected boot exactly as before. Valid
native-construction checkpoints remain fenced until their explicit recovery path
consumes them; malformed checkpoints remain fenced fail-closed. An inherited
`open_ah` preparation is observed against the fresh scene and is never replayed
blindly.

If a fresh owner inherits an unresolved action intent (for example,
`current.action = "open_ah"` with `actionResult = "preparing"`) and the stored
drill no longer matches the newly observed environment, `start`/`resume` first
returns `nextAction = takeover_drill`. The drill is allowed while the controller
is PAUSED or STOPPED, including when `stopRequested` is already true, only after
the owner proves that no local action worker can be crossing its dispatch
boundary: the motion fence, Guardian lease/held input, active process groups,
calibration/native workers, executor continuation, and mail cycle must all be
idle. A live in-process action is refused with the existing unresolved-action
error.

The drill performs only the bounded hover and human-input observation. It does
not reconcile, replay, clear, or complete the intent, and it does not clear
`reconciliationRequired`, `restartUncertain`, `stopRequested`, or the action
result. A passing drill leaves the controller PAUSED (a STOPPED controller is
paused for the drill) and leaves the stop request set. Run `resume` afterward;
fresh scene evidence then enters the ordinary reconciliation path. In
particular, an inherited `open_ah` intent with result `preparing` is observed
against the current scene and is never blindly replayed.

## Pause, handback and failures

Physical input revokes automation. For an explicit pause:

```sh
crowley-session --config ~/.config/crowley/session.toml pause --generation <current-generation>
```

When the game is handed back, use resume to preserve any pending scan or reload:

```sh
crowley-session --config ~/.config/crowley/session.toml resume --generation <current-generation>
```

Resume checks current readiness and reconciles durable effects before sending
another action. Never use start to discard unresolved work. A service restart
recovers an active repeat session automatically; an existing operator pause or
stop remains held. If the restart readiness check fails, status records a BLOCKED
reason. Resolve that condition and use resume with the current generation.
A drill can be canceled from another terminal with
`takeover-drill-cancel --generation <current-generation>`.

Status exposes `startup`, `takeoverDrill`, `runtimeReadinessInvalidation`, `nextAction`,
the current scan intent and the exact blocker. Invalid telemetry, observer loss,
identity changes and input-environment changes have explicit causes; message
wording does not determine which proof is retained. Unknown input fails closed.
Readiness failures do not authorize another native benchmark or a fresh journal.

The AH opener targets Jaxon, checks native range, and verifies the session-only
CTRL-F12 action. It can set only an empty or already-identical INTERACTTARGET
binding, verifies the result, and never calls SaveBindings. It requires a newer
HUD confirming AH-open before scanning. Automated clicks elsewhere retain the
one-per-second limit and the curved, timed full press/release executor.

A server disconnect can leave a collection gap. Retain the last original scan and
its exact upload/loaded-ACK evidence, recover the current scene, and reconcile.
Do not repeat a submitted command merely because a later receipt failed.

## Verification

Run the focused startup, public restart, takeover, native keyboard/pointer,
protocol and addon tests for this change, then lint/format checks and
`git diff --check`. Review the frozen bytes before promotion. A schema3-to-schema4
cutover needs an owned staged reload because both the installed addon and editable
Python source resolve to this checkout.

Live acceptance consists of passive startup, the genuine one-time drill, a complete
autonomous save/upload/ACK/reopen cycle and a takeover/resume test. Record observed
900-second start intervals and actual gaps. Isolated Xvfb fixtures and model
snapshots do not establish live Mutter/Xwayland acceptance. Native memory
benchmarks and three validation-mode sweeps are not startup gates. Validation mode
remains available separately and closes out after three qualifying sweeps.

## Natural interaction commands

The motion executors use a separate interaction mode after passive startup and a
matching takeover drill pass. Enter it from the paused controller using its current generation;
read the returned status for the generation used by later requests:

```text
crowley-session --config ~/.config/crowley/session.toml interaction-start --generation <current-generation>
crowley-session --config ~/.config/crowley/session.toml motion-camera-calibrate --generation <interaction-generation> --delta-pixels 4
crowley-session --config ~/.config/crowley/session.toml motion-movement-calibrate --generation <interaction-generation>
crowley-session --config ~/.config/crowley/session.toml motion-key --generation <interaction-generation> --role move_forward --duration 0.08
crowley-session --config ~/.config/crowley/session.toml motion-camera --generation <interaction-generation> --delta-pixels 4
crowley-session --config ~/.config/crowley/session.toml motion-waypoint --generation <interaction-generation> --x <nearby-world-x> --y <nearby-world-y>
crowley-session --config ~/.config/crowley/session.toml interaction-stop --generation <interaction-generation>
```

Calibration measures actual camera response, then repeated forward/backward and
both strafe roles using fresh telemetry and visual route evidence. The technical
movement probe defaults to 0.03 seconds. Camera calibration accepts signed 4–8 pixel
drags. Waypoints require the measured mappings and remain within a cumulative
two-world-unit travel budget. Each physical action reserves fresh post-release
feedback; no progress, chat focus, stale evidence or cancellation stops it.
`interaction-stop` revokes without waiting behind an active motion request.
Scan scheduling remains idle in interaction mode.

The optional `pointer_profile`, `camera_profile` and `move_profile` configuration
fields select `"baseline"` or an absolute schema 2 profile path. The
`motion-profile build --demonstrations <numeric-json> --output <path>`,
`motion-profile show --profile <path>` and `motion-profile reset --profile <path>`
commands manage deliberately supplied summaries. See the
[executor architecture](ARCHITECTURE.md#natural-interaction-executors) for the
timing model. Numeric imports do not establish live human calibration. Current
live acceptance and deployment status are recorded at the top of
[HANDOFF.md](HANDOFF.md).

### Idle logout and inherited AH opens

WoW logs an idle character out to character select after about 30 minutes. An
inherited, unresolved `open_ah` intent is cleared when the fresh scene shows the
auction house closed in world, or shows character select: opening the AH is
idempotent, so the normal cycle (login recovery, then the cadence-anchored reopen)
continues. An open AH, or an unproven scene, keeps the intent unresolved.
