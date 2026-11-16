# Concrete operation runtime

Implementation complete. Temporary worker scopes below are historical; see
RESTRUCTURE_EXECUTION.md for final review and verification, and AUTOMATION.md for operation.


This is the next bounded Luna loop after native and desktop contracts settle. Parent owns
git/docs and host integration. Worker owns NEW automation backend.py/runtime.py and updates
automation cli.py/runner.py/ledger.py plus tests/automation/test_backend.py,
test_runtime_cli.py and existing foundation tests for changed public behavior only. Do not
edit desktop primitives/config, motion module, Lua/common frames, host, root metadata or
other tests. No live input/deployment/service/secret/production DB. Do not commit/reset/stash.

Read API/native/movement plans, final schema and concrete desktop config before editing.
One concrete desktop adapter plus small fake IO in tests, no new framework layers. Use
the same private GUI lease and pinned input tool. No alternate pointer planner or emitters.
Runtime factories may inject test dependencies without abstracting every pure function.

## Runtime boundary

Backend observes one owned game process and exact focused full-screen window, captures and
decodes a fresh current-call automation schema2 frame, and validates independently configured
region/realm/build/interface/faction/character. Never use catalog/plan/host/market imports.
Timestamp before capture and recheck capture age plus focus/presence/process afterward. Do
not retimestamp reused frames. GUI lease held across whole batch; conflicting owner rejects.

Stage uses only exact /cauto grammar. Each bounded <=128byte command: fresh current gates,
open chat if verified inactive, type text with per-command/per-character guard, wait for
fresh exact active+focused chat readback, press Return, then await command-specific native
publication. Never press Return if text differs. Timeout/boot changes stop, no blind replay.
All preparatory input requires batch/session authorization and no human presence.
Send action fields, expiry and seal; wait for native scanner ownership quiescence. Read
operation.action and canonicalize/hash actual args before returning Admission. Never accept
an echoed supplied digest. Each runner action stageIndex maps one bounded primitive.

Market and mail dispatch require ready exact identity/readback and fresh target button
rect. Plan seeded curved Fitts pointer path from actual pointer inside canonical click
permit; emit path only, recapture/revalidate target/focus/process/presence/expiry before
actual press, verify pointer endpoint still in target and correct window, precommit click
rate permit, send real mousedown+dwell+mouseup, record press. Always release on emitter
failure. Save private compact trajectory seed/press/receipt evidence. One protected call.
Await a causal receipt with changed sequence and exact identity; timeout returns uncertain,
not API success. Unknown retains native AH ownership and interlocks host maintenance.

Interaction targets are explicit immutable configured profiles, not arbitrary coordinates:
target ID hash binds house/build/instance/exact NPC name/GUID or exact measured mailbox
rect, expected AH/mail UI and distance. NPC uses exact target command/readback then the
verified INTERACTTARGET binding. Mailbox profile pins a reviewed measured raw player-position
anchor and tight maxDistance in the exact native instance/build/house/character, plus the
measured screen rect and exact world-object tooltip name. Fresh motion x/y must remain inside
that anchor radius; this measured range evidence is not a claim of native object coordinates.
After pointer motion require fresh hover.kind='object', exact name and physical cursor
coordinates matching endpoint, then recheck anchor/focus/presence/identity before press.
The contract does not expose a mailbox object GUID. Missing measured anchor/hover evidence
refuses input. Open UI is postcondition. Do not
pretend InteractTarget can interact with a mailbox game object if native behavior differs.

Routes use movement.py immutable measured profiles, exact arrow binding readback and
one persisted waypoint per primitive. Keys use <=0.25s holds with fresh gate and guaranteed
release. Arrival/readback establishes completion; errors/timeouts uncertain. No shipped
guessed routes, no replay after crash. Runtime RunnerConfig derives actual configured IDs
and waypoint counts; profile hash mismatch rejects.

## Public CLI

Keep prepare/inspect/status. Add observe (native frame and gold/inventory info), inventory/
owned/mail paged read-only inspections, dry-run (native admission and exact cost/identity
preflight but NEVER click/hold), run requiring --confirm fullBatchID and --arm exactBoot
plus separate short --arm-expires; stop; reconcile. All output bounded JSON and errors JSON.
Defaults use XDG_STATE_HOME via common paths, not hardcoded home directory.
No live arming by config/default. Interrupted dispatched state becomes uncertain globally.
Stop is durable, checked before staging/dispatch and in every movement/input wait; it never
claims an already-dispatched effect failed. Reconcile succeeds only from exact validated
native causal receipt or explicit independently reviewed evidence; no --success assertion
that bypasses unresolved effects. No automatic operation replay or startup auto-arm.
Incomplete native qualification must be accurately reported; code dry-run cannot claim live
sale verification. Staged action expires/clears safely if cancelled before dispatch.

## Verification

PYTHONPATH=packages/automation/src:packages/common/src uv run pytest tests/automation -n0 -q;
scoped Ruff check/format and Pyright. Real contract tests must cover staged argument drift,
stale/cached captures, command text/readback loss, human/focus/PID/boot drift immediately
before press, rate permit, cursor/window mismatch, native callback/result separation,
mail primitive cursor, movement release/stop, inspect pagination, default XDG, duplicate
and restart, uncertain global interlock and no false reconciliation. Do not mirror tests
against only one fake success path. Report exact files/checks and DONE only when complete.
Discuss runtime growth >1000 lines before adding scope; avoid generic buses/adapters.
