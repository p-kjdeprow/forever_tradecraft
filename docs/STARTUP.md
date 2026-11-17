# Startup and recovery

The startup source implementation and independent reviews are complete; installed-runtime cutover
is pending. Installed legacy crowley-scan is still distinct from the source crowley-host/automation
packages; a source edit does not update running services.
See [STARTUP_PLAN](STARTUP_PLAN.md) for the frozen API, ownership and qualification gates.

The shared startup path reconciles the current desktop rather than assuming the launcher just
created the game. It reuses an exact existing game/Battle.net process, repairs supported window
geometry in place, and enters the configured character only from positively identified selection
UI. It validates native world readiness independently of scanner decoding before host scan policy
resumes. A healthy existing sweep requires no startup action or state reset.

## Holds and failures

| Observation | Intended behavior |
|---|---|
| Physical input within five minutes | Release startup ownership and wait; no attempts consumed |
| GUI lease or automation effect owned elsewhere | Hold without input or a relaunch penalty |
| Graphical-session environment absent/stale | Refresh from the user manager and wait visibly |
| Screenshot capture fails | Retire stale proof, release controls safely, log a bounded cause and reobserve within the original startup deadlines |
| Existing decorated/maximized game | Repair and verify the same owned window |
| Measured Battle.net Updates information window | Verify owned child, panel/heading/release/close control; dismiss once and observe disappearance |
| Login required or account conflict | Block for the user; never type credentials or reconnect |
| Updating/downloading/busy | Report and wait, without clicking an unrelated button |
| Expected full selected display name and enabled Enter World | Enter once and verify native character/realm identity |
| Missing HUD, loading, queue or unknown UI | Observe/hold; missing HUD never authorizes Return |
| Established prolonged scan failure | Guarded normal close of the exact client; verify exit before startup |
| Stop or controller death | Cancel recorded control helpers; preserve the real game and Battle.net |

Phase deadlines, the overall episode deadline, next retry and dispatched action are separate
status facts. A dispatch does not prove the expected UI/process/native postcondition.

## Configuration and qualification

Automation remains standalone: product facts, exact process identity, native realm/build pins,
window/canvas geometry and the measured full character-selection label are explicit configuration.
character_select_name is a literal observed UI value, independent of the native character/realm.
The current UI displays no separate realm label. The host renders the selected-name field from
its explicit measurement. A wrong or unqualified selected label blocks entry; after entry the
native character and realm must match before the controller declares ready or admits host input.

Tesseract with English data is the single supported OCR engine. Its exact text and bounding boxes
are corroborated by current layout/control geometry, ownership, focus and fresh capture evidence.
A blue button or four bright glyphs alone do not establish Play. Only the measured 900x600
Battle.net Updates release-information window is dismissible; unknown dialogs remain visible holds.
On the qualified GNOME Screenshot 41 / libhandy 1.8.3 platform, the screenshot child sets
`HDY_DISABLE_PORTAL=1`. This removes a measured blocking theme-settings lookup before capture;
the authenticated GNOME Shell screenshot path is unchanged. The switch is scoped to a copied
child environment and is an [internal library contract](https://github.com/GNOME/libhandy/blob/1.8.3/src/hdy-settings.c#L122)
that must be requalified when the desktop platform changes.
Source tests use adversarial cases and real sanitized capture evidence; qualification
requires the selected client build and measured UI. No trust/bypass toggle enables unknown layouts.

The controller owns the canonical GUI lease for one bounded, noninteractive startup episode.
Input subprocesses carry parent-death protection and the inherited lease descriptor. Real Wine/
Battle.net applications are deliberately outside that control-child lifetime. Graceful cancellation
can release only keys/buttons this run may have left down; SIGKILL cannot retract queued X events.
No helper waits for stdin or an internal review checkpoint while retaining desktop ownership.

## Rollout

Build and review coordinated component wheels/config/launcher/addons first. Migrate legacy
host.toml using [GAME_SETUP](GAME_SETUP.md), including the separately measured automation crop
and selected UI name. Review the exact installer dry-run and retain private rollback artifacts.
At a quiet saved/uploaded/ACKed scan boundary, retire the old owner and its control children,
install and verify the new desktop set, and enable only the new host. Preserve SavedVariables,
mutable Data.lua ACKs, original scan-plan identity and companion cadence. This startup work does
not require a market-server image or database change.

Final source verification, live UI qualification and installed cutover receipts will be recorded
in [HANDOFF](HANDOFF.md) when complete. Follow [local-game-control](../.agents/skills/local-game-control/SKILL.md)
for live operation; no machine reboot or account change is part of routine startup qualification.
