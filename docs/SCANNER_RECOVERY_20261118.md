# Scanner observation failure recovery

The scanner host stayed active while its startup recovery was permanently failed.
At 20:42 CST on Nov 17 it recorded `desktop_observation_failed`. The last accepted
sweep at diagnosis was 937, finished at 21:06 CST. The companion continued HTTP 200
acknowledgement refreshes and the website stayed available, but neither established
new collection. An earlier service-only check missed the collection failure.

Read-only desktop checks established one owned client, a valid focused fullscreen
window, authenticated session, no human-presence hold and no automation interlock.
The client showed WOW51900319. Its ordinary sparse OCR assigned low confidence to
the disconnect message, so startup called the screen unrecognized. A bounded local
OCR check confirmed the error code; no screenshot was sent to a model.

Recovery normally closed only the proven disconnected client under the GUI lease,
with fresh session/process/window/presence/build checks. Exact exit was verified;
no kill or duplicate Battle.net launch was used. The host reused Battle.net and
launched a new client. A pointer target revalidation rejected one character-entry
attempt before press. A fresh stable attempt entered the character, then a single
ObserveError during binding again caused permanent failure. A later independent
native read was healthy, with the pending command still visible.

The original exception detail was discarded by the old startup handler, so its
specific trigger cannot be established from the historical generic fault. The
recovery behavior is conclusive: observation errors were treated as terminal and
repeated forever in status while the service remained alive. The network cause of
the disconnect is not established by the error code alone.

## Narrow fix and deployment

ObserveError now shares the existing capture-error recovery path: a bounded local
diagnostic, removal of stale native proof, cleanup/release, and a fresh observation
attempt. Existing retry timing, original episode/phase deadlines and action-attempt
counters remain. The selected deadline is enforced when the observation returns;
persistent failures expire. Failed control cleanup still retains ownership. No
freshness limit, chat-readback requirement, focus/presence guard or trading gate is
relaxed. Commands are not blindly replayed.

Three regression groups now exercise both capture and observation errors: transient
recovery with unchanged counters/deadlines, original episode expiry, and unreaped
control ownership. A further regression enforces the earlier world-phase deadline.
Parent verification: 148 startup/host/control tests passed, scoped Ruff check/format
and Pyright passed. Sol's final source review is READY after one phase-expiry fix.

The runtime wheel is the exact installed 8b755b9 automation package plus this
startup module change. All 47 packaged resource files were compared; only
`crowley_automation/startup.py` changed. Its wheel SHA256 is
`9207ee17448beb5913756cad3cec26d7878acd4d0e0290e4585c167011a09201`.
Installation was performed with the host stopped, offline and without dependency
updates. The original wheel is retained for rollback; post-install bytes match and
`crowley-ops doctor` passes. The host resumed automatically. No addon, configuration,
server/database, scanner cadence, listing default or input backend was deployed.

Fresh post-install status proved native readiness, the open AH, closed chat and
`sweep_running` without a fault. Recovery sweep 939 finished Nov 18 at 00:36:53 CST;
the companion uploaded it at 00:37:27 with HTTP 200 and 14,038 listings accepted.
All 471 planned items were queried successfully. The website's latest-sweep API,
trading summary and recipe watch agree on sweep 939 and build 1.60.1.70959. The
summary returns 123 candidates and ten opportunities; both data views and the
dashboard return HTTP 200. These checks establish new collection and ingestion,
rather than substituting process activity for evidence. Remaining broader
disconnect-screen recognition/reconnection behavior is not claimed fixed by this
observation hotfix.

Private operational receipts and rollback artifact:
`~/.local/state/crowley/scanner-recovery-20261118/`. Build staging is disposable;
retain the deployed wheel and its rollback provenance. No trading occurred.
