# Natural input planning and OS delivery

User authorization: review this design with Sol, implement it, and land on main
and origin/main. This is source implementation and isolated verification. Keep
the deployed scanner/website running, do not send game input, enable OS device
permissions, install services, or arm/trade during development. Steam Deck work
is cancelled and remains out of scope.

## Objective and boundaries

Reuse the seller policy, ledger, receipts, exact native readback, GUI lease and
click spacing. Replace duplicated input execution with coherent timed gestures;
add an explicit Linux uinput backend and physical takeover handling; support
continuous forward movement inside one admitted waypoint on the selected uinput
transport, without releasing keys merely to check stop. XTEST retains bounded
movement chunks; keys always release before receipt or next chat staging.
Natural behavior must not depend on subprocess or screenshot overhead. Virtual
devices remain virtual; no hardware impersonation or indistinguishability claim.

The current installed runtime remains unchanged. A kernel backend must be an
explicit selection, with required permissions/calibration validated and no silent
fallback to XTEST. The existing XTEST deployment stays supported during migration.
Do not broaden native observation freshness, relax exact command readback, fake
physical idleness, raise the one-click-per-second cap, or change trading economics.

## Implementation order and acceptance criteria

1. Review the plan and the current input seams with Sol before implementation.
   Resolve concrete findings in this plan, including any scope/safety conflict.
2. Implement a small immutable event/timeline contract and one shared timed input
   executor. Keyboard plans validate the full text before input, balance key and
   modifier transitions, use correlated bounded cadence/dwell and word/boundary
   pauses, and never invent command typos. A command uses one bounded emission
   job; no per-event process launch or image capture. Scheduling is monotonic;
   deadlines account for declared duration without renewing authorizations.
3. Migrate host text and standalone automation text to that shared path. Preserve
   fresh admission and fresh exact full-text readback before the separate Return.
   Use cheap stop/focus/session/presence monitoring during emission and release
   possibly held controls before unlocking on cancellation or helper failure.
4. Preserve bounded curved pointer paths, distance/target-size timing and safe
   endpoint variation. Allow a genuine stationary click when already inside the
   verified safe target instead of forcing an artificial detour. Update the
   project rule to distinguish required movement from an already-positioned
   pointer. Every click still needs fresh target/focus checks and the shared cap.
5. Implement uinput as a separately selected, usable input transport: virtual
   keyboard plus relative mouse, no per-key device recreation, bounded native
   event parsing, monotonic timing, held-state tracking and teardown. Give any
   privileged portion the narrow device access it requires; never run trading
   logic as root. Permissions/mappings/calibration must fail explicitly when
   absent. Relative counts must not be assumed equal to screen pixels; require
   verified pointer behavior and closed-loop endpoint checking.
6. Pair uinput with physical activity detection that excludes only its exact
   owned virtual devices and treats actual keyboard/mouse/controller input as
   takeover. Missing device access, ambiguous identity or unhandled hotplug must
   block execution. Record activity, not private typed contents. Do not assume
   Mutter ignores this transport as it ignores XTEST.
7. Preserve the per-waypoint ledger/resume contract. Only selected uinput gains
   continuous forward holds inside one admitted waypoint, bounded independently
   by authorization, position-feedback age, estimated travel and watchdog expiry.
   Unchanged keys stay down across feedback checks while those budgets remain.
   Stationary turning stays separate: no moving-turn calibration is invented.
   Release before final verification, receipt, chat staging or control changes.
   Keep XTEST's short bounded holds and unqualified desktop routes unavailable.
   A helper owns device teardown and retains the inherited GUI lease until after
   release/destruction, including parent loss. Parent handles helper loss before
   releasing its lease. Blocking capture/guard calls cannot renew the helper's
   authorization/feedback deadline. Physical history uses a conservatively seeded
   empirically qualified, exact-topology keyboard/mouse Mutter coverage before
   any owned event, or a complete five-minute observation in a foreground session.
   Short manual arms without qualified history fail early. Controllers/unknown
   capabilities cannot use keyboard/mouse bootstrap. Lost coverage invalidates
   the session; an empty queue never proves prior idleness. Never use post-emission Mutter idleness for
   uinput. No across-waypoint continuity or moving-turn support is claimed.
8. Independently verify the final diff, obtain Sol implementation review, fix
   reachable findings, then commit/push. Document implemented versus live-qualified
   capabilities and leave new device delivery inactive on the live machine.

Sol design review: READY after the waypoint/cleanup refinements. A subsequent
bounded review identified the 300-second preparation versus 120-second manual-arm
conflict; the explicit qualified-coverage path above resolves it without extending
authorization. Final implementation review is READY after two source fix rounds; live qualification
remains separate.

## File scope

Implementation may change Python input/config/runtime code under
`packages/automation/src/crowley_automation/`, host composition under
`apps/host/src/crowley_host/scan/`, corresponding host config/installer rendering,
and directly affected tests. New modules should be limited to the actual timeline,
device/session and activity responsibilities; use existing interfaces and ownership.
The parent owns documentation, CLAUDE.md, integration and all git operations.

Forbidden: market/scanner business rules or SQL, game addon/native API contracts,
vendored input-tool archive changes, secrets, actual host config/services/device
permissions, production DB, game input/captures, Steam Deck access, alternate
execution ledgers or workflow queues. Do not commit/reset/stash/revert others' work.

Review may refine the exact implementation split before coding. Parallel workers
must have disjoint write sets and an explicit shared API contract. Do not build a
general input framework or new background service to cover hypothetical platforms.

## Verification

Use pure plans and fake descriptors/clocks/devices to test normal execution and
failure boundaries; do not exercise the real /dev/uinput or capture the desktop.
Cover balanced/invalid keys and modifiers, seeded cadence and realistic bounds,
timing/deadline expiry, physical takeover versus owned events, lost permissions,
device disappearance/hotplug, worker/parent death cleanup, click spacing, pointer
endpoint correctness, stationary clicks, and continuous-route progress/cancellation.
Retain scanner/automation package independence and command capture-count regression.

Run scoped Ruff check/format and Pyright, affected input/control/host tests,
`uv run pytest -n 0 tests/automation -q`, and package boundary checks. Parent repeats
the integration checks and checks final scope. Build matching distributions and
verify packaged resources without installing/restarting the running system.

Completion requires implemented transports/behavior (not stubs), meaningful tests,
Sol's final source review, clean tracked worktree and matching main/origin/main.
Live uinput, game movement and protected trading qualification remain separate and
must be reported honestly, not inferred from simulated tests.

## Completion evidence

Parent implementation and independent gpt-6.1-sol source review are complete. Sol
reported READY after correcting preparation expiry, parent-loss/release ownership,
absolute travel budgets, logical button mapping, private calibration validation,
scheduler latency and the stopped baseline before stationary turns. No source
findings remain. Parent final verification: 575 affected tests pass, scoped Ruff
check/format and Pyright pass (zero errors), and all five matching wheels/SDISTs
build with new input modules and required resources present. Build staging was
removed; no worktree was created. Scanner and companion remain active and the
dashboard responds HTTP 200. New input delivery remains inactive/unqualified on
the actual desktop; no game input, captures, service/config/permission changes or
trade occurred.
