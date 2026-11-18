---
name: local-game-control
description: Operate and diagnose the running WoW Forever client on this Linux desktop (Wine, Battle.net, the Crowley host loop). Use for live addon checks, game-window troubleshooting and any manual contact with the game; excludes addon implementation and trading automation.
---

# Local game control

`crowley-host` (unit `crowley-host.service`) already owns the game: it relaunches, enters the world, opens the AH
and reloads. Read [REPEATING_SCANS](../../../docs/REPEATING_SCANS.md) first. Older deployed packages use `crowley-scan`; verify the installed version
and use its matching command/unit until the coordinated v0.2 cutover. Source edits do not deploy it. A diagnostic task does not authorize
trading, account changes or unrelated display changes.

## Before touching the game

- Look first, do not act: `systemctl --user status crowley-host`, `crowley-host status` (action, fault code,
  observation summary), `journalctl --user -u crowley-host -n 50`, `crowley-desktop probe-frame`
  (exit 0 frame decoded, 1 verified none, 3 error).
- An active service and HTTP 200 do not establish collection. Check the host fault/detail,
  fresh native/scanner progress and a newly accepted sweep before reporting the scanner
  online. A failed startup can leave its process alive while collection is stalled.
- Any physical input holds the loop for 5 minutes (Mutter IdleMonitor), so a human using the desktop is safe.
  For longer manual work stop the loop: `systemctl --user stop crowley-host` (the game keeps running) and start it
  again when done.
- Your own synthetic input (XTEST) does not register as a human, so it will not hold the loop. Stop the unit
  before sending any, and obey the click rules in `CLAUDE.md` (at most one automated click per second, curved
  human pointer motion through `crowley_automation.gui`, never warp the pointer).
- Validate helper imports, CLI argument formats and restoration paths before stopping the owner. Bounded
  focus-only inspections need automatic restoration in `finally`, with fresh ownership checks and stop honored;
  do not depend on a later model turn to restore focus or resume collection.
- Check processes and map windows to PIDs before launching anything. Match the owned game by uid, Windows argv0
  `Wow.exe` and `WINEPREFIX` (`crowley_automation.gui.process`), not by broad command text. Battle.net visible does not mean
  the game stopped. Never start a second client or a second Battle.net (it deletes the saved login token).
  Match control helpers by exact executable/argv tokens and process start identity; inline shell/Python source can
  mention launcher names without being a launcher. Persist dispatch results before another action or handoff.
  A positively verified selected product needs only the guarded Play action; do not add a Wine product-selection
  launch first. Cold application launch and bounded desktop-control children have different lifetimes.

## Environment

- SSH access does not establish graphical control. Inspect a remote host's actual
  session and required services first; Gamescope/KDE cannot use GNOME capture or
  Mutter presence merely because Xwayland exists. Record missing capabilities and
  qualify the target path rather than guessing authority or reporting false idleness.
- An mDNS name can lack a known-host record while its resolved IP is already trusted.
  Verify and use that existing identity; never disable host-key checking to fix an alias.
- Derive the server address, addon-data path and container names from installed configuration and the package-owned
  Compose project. Do not treat a failed check against guessed defaults as a deployment fault.
- A worker shell may lack the graphical environment. Take `DISPLAY` and `XAUTHORITY` from the graphical session or
  `host.toml`; do not dump the whole environment. `CROWLEY_WINE` is in `secrets.env`.
- Use current screenshot dimensions and visible controls; coordinates from another resolution or window position
  are stale. Verify outer and client geometry against the configured display; a manually started client can retain
  a decorated or maximized window even when its client buffer matches the display size.
- Verify tool flags with a harmless invocation before relying on them (`xwininfo -stats` prints geometry;
  `-geometry` is unsupported). `wmctrl -lp` rows are XID, desktop (can be -1), PID, machine, title.
- Wine identity channels differ: `/proc/<pid>/exe` is the Linux loader and argv0 may use Windows path semantics.
  Chromium can rename Battle.net's `comm`; bind it to exact executable argv0, UID, prefix, start ticks and window
  PID instead of a fixed process name. Reuse the producer matcher rather than duplicating it in an operator script.
  Exact title/PID can also match Wine's unmapped override-redirect helper; inspect map state and override-redirect
  before treating hidden candidates as duplicate application windows. Preserve visible and managed minimized candidates.

## Plan and build cutovers

- Before staging a client update, compare the active `.build.info` product version, the running HUD build and the
  packaged target. A warm client can stay on its old build after the files on disk change; a clean relaunch activates
  the update.
- After a coordinated addon cutover, verify the companion's actual client-data field set before the next game
  load. Server response metadata and client Lua have separate contracts; a parseable Data.lua can still abort addon startup.
- Run a preserved legacy tool with its own venv interpreter; a copied CLI can retain the original installation's
  shebang. Wheel-based server images install modules in `/app/.venv`; inspect package resources with that image's
  `/app/.venv/bin/python`, not its system interpreter or a guessed source-tree path.
- For a reviewed plan cutover, verify the old sweep was saved, uploaded and ACKed, then stop the loop. Hold the GUI
  lease across fresh frame, disk and server checks, post-capture process/window/focus/presence checks, one normal
  window-manager close and exact process exit. Release the lease before installing. A reviewed helper can carry a
  narrow idle window directly through close when model delay could consume its safety margin. If close fails or its
  outcome is uncertain, leave the loop stopped and diagnose; do not retry or kill.
- `Observer._sample` timestamps before screenshot capture. Require a newly decoded current-call frame within the
  installed `STALE_FRAME_S` using its original timestamps, and refresh window focus and presence after capture. Treat
  the status file as a heartbeat; never retimestamp a cached frame. Finish blocking database/upload checks before the
  final capture and post-capture ownership/focus/presence checks; do not insert them between those checks and input.
- Client-data export requires a finalized `DBCache.bin` matching the target build. A window-manager close may leave
  only a temporary cache; use a separately approved guarded normal `/quit` when needed, never rename or edit the
  cache to fit validation. Resume normal collection before the offline export.

- For a hot-cohort promotion, verify each promoted ID was actually queried in two consecutive accepted full sweeps
  and compare query timestamps with the configured cadence. Plan membership alone does not establish live cadence.

## Make input observable

- Keep command typing bounded: admit once, send explicit measured key transitions
  through one guarded child, monitor stop/presence/session/process/focus without
  screenshots, then require fresh exact full-text readback before Return. Capturing
  images or spawning a helper per character makes typing unusably slow. Local pixel
  decoding/OCR is separate from model usage; inspect the actual call path before
  attributing quota consumption to screenshots.
- Exit code zero from an input tool proves dispatch, not effect. Read the result from a fresh HUD frame
  (`crowley-desktop probe-frame`, `crowley-host status`) before the next dependent step; XTEST keys can land up to about
  1 s late and go to the compositor-focused window, which is not always the X "active window".
- A capture error does not establish a missing window. Preserve its original bounded command/cause diagnostic;
  reproduce once with the unmodified installed runner and session provider before changing capture behavior.
  Time outside the call first; a replacement runner can change environment or subprocess arguments and hide the bug.
  GNOME Screenshot can block during toolkit portal settings initialization before its Shell screenshot RPC.
  Trace the screenshot sender's portal calls too; the qualified runner disables that theme-only portal in its child.
- Do not replay an action because its first frame lags.
- Qualify normal and hovered controls before a production click. Pointer motion can change OCR segmentation
  without moving the target; keep confidence, semantic identity and post-motion geometry checks intact.
- A GNOME system modal can dim the HUD and ignore Xwayland input. A disconnected login screen keeps the same Wine
  identity; read the screen, not just the process.
- Recheck the current PID and screen after a user returns or restores the game. An earlier login or ruleset screen
  does not describe a replacement process; retire recovery authorizations bound to the old process.

## Recover without widening the task

- Do not park an already-authorized recovery helper at an interactive review/read checkpoint while holding the
  GUI lease. Execute the bounded guarded step and report its result; request approval only when the task requires it.
- Account-conflict dialog: check for duplicate local clients first and close only the accidental duplicate normally.
  Prefer the application's Quit or `wmctrl -ic <XID>` (window-manager close); `xdotool windowclose` does not end the
  client.
- A disconnect or maintenance needs nothing from you: the loop relaunches (15 min apart, then hourly). Stop it only
  if a human needs the desktop.
- Honor the latest user instruction. Stop forbids new presses, pointer motion, window changes and retries.
  Only bounded releases of this run's possibly held keys/buttons may precede GUI-lease release; this does not
  authorize other cleanup actions.
- Older `crowley-scan` units do not cancel their launcher child on stop. Prove that child ended or quiesce only
  its exact recorded control group before manual input, with real game/Battle.net outside it. The new host uses
  in-process recovery and reaps exact control children before unlocking; verify the installed implementation.
  Never infer exclusive GUI ownership from an inactive legacy unit alone.

## Report

Distinguish command submission, an in-game message, saved output (SavedVariables), server ingestion and the loaded
acknowledgement, and report the stages actually verified. Check status and actual start time before forcing another
scan; the configured sweep cap is a maximum, not a wait. Before interpreting payload shapes or depth coverage read
[crowley-client-contracts](../crowley-client-contracts/SKILL.md).
Verify receipt paths on disk before linking deployment reports; preserve their exact directories when condensing
historical notes.

Keep this skill short: correct it when an observed mistake reveals a reusable lesson, and leave out incident logs,
transient PIDs and coordinates.
