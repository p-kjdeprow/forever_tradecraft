---
name: local-game-control
description: Operate and diagnose the running WoW Forever client on this Linux desktop (Wine, Battle.net, the crowley-scan loop). Use for live addon checks, game-window troubleshooting and any manual contact with the game; excludes addon implementation and trading automation.
---

# Local game control

`crowley-scan` (unit `crowley-scan.service`) already owns the game: it relaunches, enters the world, opens the AH
and reloads. Read [REPEATING_SCANS](../../../docs/REPEATING_SCANS.md) first. A diagnostic task does not authorize
trading, account changes or unrelated display changes.

## Before touching the game

- Look first, do not act: `systemctl --user status crowley-scan`, `crowley-scan status` (action, fault code,
  observation summary), `journalctl --user -u crowley-scan -n 50`, `crowley-scan probe-frame`
  (exit 0 frame decoded, 1 verified none, 3 error).
- Any physical input holds the loop for 5 minutes (Mutter IdleMonitor), so a human using the desktop is safe.
  For longer manual work stop the loop: `systemctl --user stop crowley-scan` (the game keeps running) and start it
  again when done.
- Your own synthetic input (XTEST) does not register as a human, so it will not hold the loop. Stop the unit
  before sending any, and obey the click rules in `CLAUDE.md` (at most one automated click per second, curved
  human pointer motion through `crowley.gui`, never warp the pointer).
- Check processes and map windows to PIDs before launching anything. Match the owned game by uid, Windows argv0
  `Wow.exe` and `WINEPREFIX` (`crowley.gui.process`), not by broad command text. Battle.net visible does not mean
  the game stopped. Never start a second client or a second Battle.net (it deletes the saved login token).

## Environment

- A worker shell may lack the graphical environment. Take `DISPLAY` and `XAUTHORITY` from the graphical session or
  `host.toml`; do not dump the whole environment. `CROWLEY_WINE` is in `secrets.env`.
- Use current screenshot dimensions and visible controls; coordinates from another resolution or window position
  are stale. The display is 1920x1080 and the game window is fullscreen at the origin.
- Verify tool flags with a harmless invocation before relying on them (`xwininfo -stats` prints geometry;
  `-geometry` is unsupported). `wmctrl -lp` rows are XID, desktop (can be -1), PID, machine, title.
- Wine identity channels differ: `/proc/<pid>/exe` is the Linux loader, `comm` is the Windows program. Windows
  argv0 uses Windows path semantics.

## Plan and build cutovers

- Before staging a client update, compare the active `.build.info` product version, the running HUD build and the
  packaged target. A warm client can stay on its old build after the files on disk change; a clean relaunch activates
  the update.
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

- Exit code zero from an input tool proves dispatch, not effect. Read the result from a fresh HUD frame
  (`crowley-scan probe-frame`, `crowley-scan status`) before the next dependent step; XTEST keys can land up to about
  1 s late and go to the compositor-focused window, which is not always the X "active window".
- Do not replay an action because its first frame lags.
- A GNOME system modal can dim the HUD and ignore Xwayland input. A disconnected login screen keeps the same Wine
  identity; read the screen, not just the process.

## Recover without widening the task

- Account-conflict dialog: check for duplicate local clients first and close only the accidental duplicate normally.
  Prefer the application's Quit or `wmctrl -ic <XID>` (window-manager close); `xdotool windowclose` does not end the
  client.
- A disconnect or maintenance needs nothing from you: the loop relaunches (15 min apart, then hourly). Stop it only
  if a human needs the desktop.
- Honor the latest user instruction. A stop ends input immediately, including cleanup.

## Report

Distinguish command submission, an in-game message, saved output (SavedVariables), server ingestion and the loaded
acknowledgement, and report the stages actually verified. Check status and actual start time before forcing another
scan; the configured sweep cap is a maximum, not a wait. Before interpreting payload shapes or depth coverage read
[crowley-client-contracts](../crowley-client-contracts/SKILL.md).

Keep this skill short: correct it when an observed mistake reveals a reusable lesson, and leave out incident logs,
transient PIDs and coordinates.
