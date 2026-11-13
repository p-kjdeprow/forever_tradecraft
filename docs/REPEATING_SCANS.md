# Repeating scans: operator guide

`crowley-scan` (unit `crowley-scan.service`) keeps WoW Forever in the world with the auction house open, so the
addon's own 900 s timer sweeps its installed scan plan and gets each sweep to the server. The reviewed repository
source is catalog revision 6, now pinned to measured native client build 1.60.1.70938: 421 core and 1 discovery item
every sweep, plus one of six cold slices containing 61 or 62 items (790 active IDs total; 483 or 484 planned item
queries per sweep). The deployed revision 6 plan has catalog hash
`082e567cf1c41e53306118cb0ad8cfbcd61b829e1861f9f076feafe5d5292147` and plan hash
`d6465ae2b812f307ca9fe0c7cf2838325fe5454d0f86346691f148e856ecf5a9`; package, generated addon target, healthy
server, and companion `Data.lua` agree on build 70938/interface 16001 and this plan. The first accepted full sweep,
`us-normal-alliance-1794550375-Dandune-3791` (database sweep 538), used cold slice 3 and ran 2026-11-13 06:12:55–
06:23:30 UTC (635 seconds). Its exact 483-ID set matched the plan; all 483 had `query.kind=queried` and
`reason_code=ok`, with 418 priced items. It was saved and uploaded, and its acknowledgement was written to
`Data.lua`. Client-loaded ACK was not independently verified; no reload was forced. No 70938 recipe export
has been loaded. Private receipt: `.archive/scan-rebalance-20261112/run/parent-first70938-sweep.json`.

The 635-second result is one measured sweep, not a p95. Separately, scaling the historical 24-sweep observed p95 of
652 seconds by planned query count projects 593.3 seconds p95, 58.7 seconds lower. This is an estimate, not measured
runtime distribution. The repository plan keeps the 900-second cadence, configured sweep cap, and six-slice policy.
Cold items recur periodically; this sweep's slice 3 did not query 279943, 279972 or 279973, so their live Auction
House eligibility remains unverified. The loop recovers from crashes, disconnects, maintenance and reboots without
an operator. Design: [ARCHITECTURE](ARCHITECTURE.md). Host setup: [GAME_SETUP](GAME_SETUP.md). Current state:
[HANDOFF](HANDOFF.md).

## What it does each cycle

Every 2 s it observes, decides and acts once.

1. **Observe:** the owned `Wow.exe`, the single game window and whether it is focused, the addon's HUD frame
   (screenshot, cropped to `canvas_crop`, decoded), the scan ids in SavedVariables, the companion's uploaded ids and
   the acked ids in `Data.lua`, and presence (physical input in the last 5 minutes).
2. **Decide:** the first matching row of the table in `src/crowley/scan/policy.py`.
3. **Act:** at most one thing: wait, hold, report a fault, relaunch the game, or one key action (open chat, type an
   allowlisted command, Return, Escape, Ctrl-F12).

The steady cycle: a sweep finishes and is saved in memory; when the next timer sweep is at least 90 s away the loop types
`/reload` (save flush); the companion uploads the sweep and writes the ack; the next reload loads the ack; the loop
reopens the AH (`/targetexact Auctioneer Jaxon`, Ctrl-F12) and the addon starts the next sweep once its cooldown
allows. Steady state holds 1-2 pending sweeps (cap 6).

## Looking at it

```sh
systemctl --user status crowley-scan
journalctl --user -u crowley-scan -f      # one JSON line per decision change, action and fault
crowley-scan status                       # scan-status.json
crowley-ops health                        # freshness and fault summary
```

`crowley-scan status` prints `action`, `fault_code`, `detail`, an observation summary (process, window, frame age,
snapshot phase, pending count, human_active), `updated_at` (rewritten at least every 60 s) and `pid`. Status and logs never
contain chat text or frame contents. The status file is in the runtime directory (`$XDG_RUNTIME_DIR/crowley`), the
rate-limit file `scan-loop.json` in the state directory; losing either is harmless.

## Human hold

If anyone has used the keyboard or mouse in the last 5 minutes (Mutter IdleMonitor), the action is `hold_human`
(detail `human input within 5 min`). Nothing is sent, including from the launcher. After 5 minutes of idleness it
continues by itself; there is nothing to resume. If the idle time cannot be read it counts as a human being active.
`crowley-scan presence-ok` gives the same answer (exit 0 means idle).

## Decisions and faults

Rows are checked in this order; the first match wins. "Fault" means no input is sent and the code is reported.

| Action / code | Meaning | What to do |
|---|---|---|
| `hold_human` | Human input within 5 min | Nothing; it resumes by itself |
| `relaunch` (`no_process`) | No owned client | Automatic, see Relaunch |
| `relaunch` (`no_frame_after_running`) | A running client produced no frame for 10 min (disconnect, idle logout, character select) | Automatic |
| `no_frames_after_launch` | A fresh client has no frame within 10 min (maintenance, login failure, crop or schema problem) | Automatic relaunch with backoff; if it persists check the screen and `crowley-scan probe-frame` |
| `window_invalid` | Not exactly one visible game window at display size and origin | Relaunches after 30 min; check the window and the launcher's fullscreen step |
| `window_activation_failed` | Three activations in 5 min did not give focus | Relaunches after 30 min; check the desktop |
| `pin_mismatch` | Character, realm, faction, house, plan hash, build, interface or addon version differs from the target; detail names the fields | Fix the config or addon install; it never scans under the wrong identity |
| `plan_invalid` | The addon refuses its scan plan | Regenerate the plan/addon files and reload |
| `construction_marker` | HUD boot or render state is not ordinary | Reload the addon; report if it recurs |
| `blocking_ui` | A popup or the Game Menu is up | Game Menu: one Escape per 30 s. Anything else: dismiss it by hand. Relaunches after 30 min |
| `chat_foreign_text` / `chat_unfocused` | Chat box holds text that is not our command, or is open without focus | Clear it by hand; the loop never erases or submits unknown text |
| `chat_open_failed` / `chat_closed_unexpectedly` / `submit_unconfirmed` | A typed-command step did not read back as expected | Retried after a 30 s cooldown; check focus and keybindings if it repeats |
| `flush_not_persisted` | A `/reload` ran but the sweep is still not in SavedVariables after 2 min | Check the game folder permissions and disk |
| `queue_full` | 6 sweeps pending and none loadable (upload stuck); detail shows rejected count and whether the companion has an error | Check `crowley-companion` and the server; payloads stay in SavedVariables |
| `not_in_stormwind` | The character is elsewhere. A relaunch logs in at the same place, so the loop does not fix it | Move the character to the auction house |
| `auctioneer_out_of_range` | The targeted auctioneer is too far | Move closer |
| `interaction_mode_unavailable` / `binding_unavailable` / `binding_conflict` | `SoftTargetInteract` is not 2 or 3, binding API missing, or Ctrl-F12 is bound to something else | Set `/console SoftTargetInteract 2`; free Ctrl-F12 |
| `ah_open_failed` | Three open attempts in 10 min failed | Relaunches after 30 min; check range and target |
| `no_progress` | AH open but no new saved sweep for 30 min | Closes and reopens the AH once; relaunches after another 30 min |
| `observe_failed` / `action_failed` | An operational error in a tick (capture, X11, input tool); the exception type is the detail | Read the journal; the tick sends no input |

Anything that is not an expected operational error ends the process and systemd restarts it (5 s).

## Relaunch

The loop ends the owned client (SIGTERM, then SIGKILL) and runs `crowley-world-of-warcraft` as a child it keeps
polling, so presence and status continue while it runs. The launcher waits for Battle.net login, clicks Play like a
human, makes the window fullscreen and presses Return at character select, repeating until a frame decodes; a launcher
run longer than 10 minutes is killed and reported. Spacing between attempts: 15 minutes for the first 3, then 60 minutes with no cap, so
maintenance and login failures keep retrying hourly. The counter resets when a new sweep is saved.

## Maintenance windows

Server maintenance shows up as a disconnect or `no_frames_after_launch`. Do nothing: it keeps relaunching hourly and
resumes when the realm is back. The server health check alerts on data age, not on the loop's relaunches.

## Stop, start, roll back

```sh
systemctl --user stop crowley-scan        # leaves the game running (KillMode=process)
systemctl --user start crowley-scan       # takes seconds; nothing to reconcile
systemctl --user disable --now crowley-scan
```

For an ordinary long stop, let the normal save/upload/ack cycle finish first. A scan-plan change has a stricter
procedure below because the running loop's protocol contract is pinned to its startup plan. To go back to the old
session stack, reinstall the tag `pre-simplify` (`git show pre-simplify`); it is not supported otherwise.

## Changing the scan plan

A plan change updates several identities together: the installed package and generated addon target, the server's
canonical plan, the companion's `Crowley_Data/Data.lua`, and the plan loaded by the client. The scan loop pins its
protocol contract at startup, including expected plan counts. If an old loop sees a new plan frame, it may reject the
frame before it can report a useful hash mismatch. Do not use an in-place reload or wait for an old-loop
`pin_mismatch` receipt to bridge a plan change.

Use the approved landed commit, full plan hash, and Sol READY review. Prebuild while normal scanning continues:
build the base rollback wheel, the approved new wheel, and the new server image into temporary locations; retain the
old server image ID and rollback artifacts. Do not mutate installed files or restart services during prebuild.

At an ordinary old-plan boundary, use `systemctl --user is-active crowley-scan crowley-companion`,
`crowley-scan status`, `crowley-scan presence-ok`, and `crowley-ops doctor` to verify a fresh healthy status with
`action=wait`, empty detail, `chat_active=false`, the auction house open, `phase=scheduled`, at least 90 seconds
before `due_at`, no blocking UI or chat, no human hold, and no command transaction in progress or open/type/submit
journal step. Confirm that `last_saved_uid` is persisted and accepted by the server as a complete old-plan sweep with
its expected item set and successful observations. Confirm every pending UID is uploaded and appears in `Data.lua`'s
`ackedScans`.
One or two already uploaded and acknowledged pending sweeps are normal; do not require `pending_count=0`.

Stop only the scan loop and confirm it is inactive and `crowley-scan presence-ok` succeeds. Then acquire and hold
the GUI lease nonblocking across the remaining old-client checks, close and exit:

```sh
exec {gui_fd}>>"$XDG_RUNTIME_DIR/crowley/gui.lock"
flock -n "$gui_fd" || { exec {gui_fd}>&-; exit 1; }
trap 'flock -u "$gui_fd"; exec {gui_fd}>&-' EXIT
```

With the old package still installed, take a fresh read-only observation using its old protocol contract. Require a
newly decoded frame from this observation call within the installed `STALE_FRAME_S` policy, using the original frame
timestamp; the old plan hash, build and interface; the expected character, realm,
faction and house; `phase=scheduled` with at least 90 seconds before `due_at`; the auction house open; no blocking UI
or chat; a valid focused window at the expected size and origin; human idle; and exactly one owned game process
and window. Verify ownership using the configured matcher (uid, Windows argv0 `Wow.exe`, and configured
`WINEPREFIX`). Under that same held lease, refresh window identity/focus and human presence after capture, then
recheck frame freshness and the safety margin immediately before sending one normal window-manager close to the
exact verified window (`wmctrl -ic <verified-XID>`). Wait a bounded interval for that exact process and window to
exit. Do not use a kill fallback, broad process matching, or another GUI owner. If any check or clean close fails,
abort without changing the installed package, addon, server or companion state.

After the exact process and window exit is confirmed, remove the exit trap, unlock and close the lease descriptor
before starting installation: `trap - EXIT; flock -u "$gui_fd"; exec {gui_fd}>&-`.

Only after the old client has exited may the approved installation begin:

1. Install the approved package and addon while `crowley-scan` remains stopped. Check the installed target revision,
   full plan hash, client build and interface against the approved values.
2. Deploy the approved server image/package and wait for a healthy server. Check the in-container canonical plan's
   full hash and revision. Restart only `crowley-companion`; wait for a fresh successful addon-data write and check
   that `Data.lua`'s plan hash matches the server.
3. Confirm package, installed `GeneratedTarget.lua`, server and companion `Data.lua` all agree on the approved full
   hash. Confirm the old client remains exited, then start `crowley-scan`. Let its normal launcher relaunch the
   existing client; do not start a second client or manually reload.
4. Require fresh status without faults and verify one newly accepted full sweep under the approved hash and build.
   Its exact item-ID set must equal the approved hot IDs plus that sweep's cold slice, and every observation must be
   successful. Verify the companion uploaded it and `Data.lua` acknowledges its UID. Record the sweep ID, slice,
   elapsed time, item/ok counts and full plan hash. Treat client ACK load/prune as optional evidence only if a later
   natural reload exposes it; do not force another reload or wait an extra cadence for that receipt.

Before rollback, verify the retained package build and interface match the measured running client; never mix plan
files. If they do not match, keep the scanner stopped and use a separately reviewed target-pin update. The detailed
preflight and command-level receipts are in the local
`.archive/scan-rebalance-20261112/OPS_PLAN.md`.
