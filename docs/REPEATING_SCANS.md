# Repeating scans: operator guide

`crowley-scan` (unit `crowley-scan.service`) keeps WoW Forever in the world with the auction house open, so the
addon's own 900 s timer sweeps the scan plan (481 hot items every sweep plus one of six cold slices of 51 or 52 items, 788 in the plan, catalog revision 5), and gets each sweep to the server. It recovers from crashes,
disconnects, maintenance and reboots without an operator. Design: [ARCHITECTURE](ARCHITECTURE.md). Host setup:
[GAME_SETUP](GAME_SETUP.md). Current state: [HANDOFF](HANDOFF.md).

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

Before stopping for a long time, let one save-reload and ack-reload finish so SavedVariables are clean. To go back
to the old session stack, reinstall the tag `pre-simplify` (`git show pre-simplify`); it is not supported otherwise.

## Changing the scan plan

The plan lives in three places that must agree: the installed package (`first_run.json`, target JSON and the
addon's `GeneratedTarget.lua`), the server image (it serves the canonical plan that the companion writes into
`Crowley_Data/Data.lua`), and the client's loaded addon data. A plan with cold items needs `scan.coldSlices` set in the target JSON (positive exactly when cold items
exist; the generator and `scan_plan` reject a mismatch), which changes `planHash`. Deploy in this order, right
after an upload:

1. `uv tool install --force --reinstall --link-mode copy .` and `crowley-ops install --game-root ...`.
2. `crowley-ops image build --source "$PWD"`, then the compose `up -d` from the README (recreates the server).
3. `systemctl --user restart crowley-companion` and wait until `Data.lua` carries the new `planHash`.
4. `systemctl --user restart crowley-scan`, then make the client reload: the loop's next save flush does it, or
   end the client and let the loop relaunch it.

If the client loads before step 3, the addon refuses the old plan and frames stop decoding; one more `/reload`
(or relaunch) after step 3 fixes it.
