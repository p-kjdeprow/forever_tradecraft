# Repeating scans

The standalone Crowley addon owns collection scheduling. The optional crowley-host application
owns unattended game launch, AH opening and scan-aware reload/ACK timing. The companion only
transports saved scans/probes and writes acknowledgements. These are independent packages.

A source checkout refactor does not upgrade a running installation. Older deployed versions use
the crowley-scan command/unit; perform the reviewed package/config/addon cutover before switching
to the new crowley-host commands below. Never install one half of a changed frame contract.

The host ticks every two seconds. It observes exact owned game processes, window/focus, independent
scanner and automation canvases, SavedVariables, uploaded/rejected companion IDs, Data.lua ACKs,
and physical input presence. Pure policy selects one bounded action. Unknown/malformed facts
prevent input. Commands are allowlisted and typed text must read back exactly before Return.

Sweeps start 900 seconds apart under the selected target, with target-defined query/sweep budgets.
Hot items run every sweep and cold items rotate through six recorded slices. Revision 9 remains
the selected catalog. Runtime counts come from its canonical plan, not historical documentation.
Query failures and unqueried items do not count as empty markets. Two successful sweeps do not
establish a runtime p95 or full cold-rotation reliability.

A sweep finishes in memory. At a safe scheduled boundary with at least 90 seconds before the next
start, host reloads to flush it. Companion uploads the saved bytes and writes ACKs. A subsequent
reload loads those ACKs; a later flush establishes disk pruning. These stages must be verified
separately. At six pending sweeps the addon pauses without evicting evidence. One or two uploaded
and acknowledged pending sweeps are normal.

```sh
systemctl --user status crowley-host crowley-companion
crowley-host status
journalctl --user -u crowley-host -f
crowley-ops health
crowley-desktop presence-ok
crowley-desktop probe-frame
```

Status is a heartbeat under the runtime directory, not authority for input. It reports action,
fault, observation summary and original frame age without chat text. State keeps relaunch and
command timing, not a hidden action replay log. Presence in the last five minutes holds all input;
unavailable presence also blocks. XTEST synthetic input does not create a human hold.

Usual faults identify missing/ambiguous window, native identity/plan mismatch, stale/missing frame,
blocking UI, foreign/unfocused chat, missing binding/range, persistence/upload blockage, or no
scanner progress. Diagnose the actual code/detail. Do not bypass guards or replay a command just
because its first publication lags. A disconnected client still has a valid Wine process identity.

Startup uses automation's shared in-process recovery controller. It refreshes the authenticated
desktop session, adopts exact existing clients and repairs supported window geometry. Positive
selected-name/control evidence authorizes entry; a fresh independent automation publication
establishes world readiness. A missing scanner canvas is a scanner fault. See [STARTUP](STARTUP.md).

After an established prolonged scan failure, the host can request a guarded normal close of the
same verified client with bounded backoff. Unknown UI, missing pre-world frames and failed focus
do not authorize closing a client. It must never reload, relaunch or send scan-maintenance keys
during an active or unresolved market effect. Client-side
AH ownership is separate from the desktop lease: Crowley_Host suspends the scanner through its
public contract and waits for active queries to finish before granting automation. Missing expected
coordination is an error; uncertain effects retain the grant.

```sh
systemctl --user stop crowley-host   # game remains running
systemctl --user start crowley-host
```

The new host cancels and reaps its recorded input/control helpers before releasing the GUI lease;
real game and Battle.net processes survive. Older installed crowley-scan launchers lack that
guarantee: an inactive legacy unit does not prove its launcher child ended. Read local-game-control
before manual contact and verify ownership under the shared lease. Stop forbids new presses,
motion and window changes; only bounded releases of possibly held controls may precede unlock.

A plan or package-contract cutover needs a quiet old-client boundary. Prebuild the full reviewed
wheel/image set and rollback artifacts. Verify old sweep persistence, server acceptance and ACK
write; the latest written ACK may load on the next boot. Require fresh old-plan frames, correct
native identity, focused window, no human/blocking UI/chat/command transaction, and at least
90 seconds of slack. A completed non-sweeping AH-closed state is also quiet. Stop host, prove its launcher child
ended, then hold the GUI lease across fresh post-capture ownership/presence checks and one guarded
normal window-manager close. Verify exact process exit before installing; a failed close aborts.
Never kill as a cutover fallback or modify SavedVariables to make a check pass.

Install every coordinated component/config/addon before restart. Observe actual loaded addon/boot,
new plan identity, new saved scan, accepted original payload, retained prices, ACK write and loaded
ACK. Rollback preserves newest operation receipts and never rearms a possible effect. Take a
pre-cutover backup with the installed legacy tool before changing the old server; the new tool
explicitly rejects snapshots of an incompatible old image. Test restoration on disposable data.

Source plan generation (`tools/generate_launch_target.py --check`/`--write`) never deploys or
rewrites live configuration. See [LAUNCH_TARGET](LAUNCH_TARGET.md), [GAME_SETUP](GAME_SETUP.md),
[MARKET_RECOVERY](MARKET_RECOVERY.md) and [HANDOFF](HANDOFF.md).
