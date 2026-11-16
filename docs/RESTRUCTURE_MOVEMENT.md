# Bounded route controller

Luna owns ONLY new packages/automation/src/crowley_automation/movement.py and
tests/automation/test_movement.py. Read RESTRUCTURE_API, CLAUDE and HANDOFF. No other
edits, commits/reset/stash, game input, services, deployment or installed/private files.
Parent will integrate concrete desktop IO and CLI; this task implements one small measured
route profile and feedback waypoint controller, not navigation, pathfinding or a supervisor.

Route profile JSON schema1 exact keys: schema, house, character, clientBuild, instanceId,
leftRadiansPerSecond, rightRadiansPerSecond, forwardUnitsPerSecond, tolerance, waypoints.
waypoints is 1..64 pairs of finite native UnitPosition x,y, no bool-as-number. Units raw
native; no coordinate conversion. Turn rates measured signed nonzero opposite signs and
bounded, forward positive measured, tolerance finite0.1..5. ClientBuild nonempty numeric
string, exact house validator, character nonempty. Canonical compact sorted JSON yields
route ID 'route-' + SHA256[:58] (64 ASCII chars) valid model target shape; file lookup
requires recomputed route ID, immutable exact profile. Max16KiB, reject duplicate keys,
nonfinite, unknown fields, wrong scalar/collection types. Nothing shipped as a real route.

Motion dataclass: boot:str, observed_at:float monotonic, x:float,y:float,facing:float,
instance_id:int, client_build:str, house:str, character:str, moving:bool, dead:bool,
combat:bool,mounted:bool. Require finite, exact booleans and known scalar bounds.
walk_waypoint(profile, index, *, sample, hold, monotonic, stop) returns result including
reached, action_complete, reason, bounded samples/holds evidence. sample callable obtains
fresh Motion; hold(keys:tuple[str,...], seconds:float) emits bounded actual key press and
release with adapter's fresh focus/presence gate. No direct native IO in this module.

Follow raw atan2(dy,dx), forward=(cos facing,sin facing), wrapped shortest angle. Choose
the signed calibrated turn key and <=0.25s hold, then forward <=0.25s only within a small
heading threshold. Re-sample between all holds. No forward+turn diagonal assumptions.
Stop immediately on stale (>2s) or future observation, boot/identity/build/instance drift,
combat/death/mount, cancellation, overshoot/divergence/no progress, or bounded15s waypoint
deadline. Release is adapter hold contract; exceptions never imply waypoint success.
No route retry/reverse/recovery/fallback. Enforce64 total configured waypoints; one runner
primitive maps one waypoint so persisted cursor prevents replay after uncertain effects.
Check that measured displacement and facing are consistent before admitting forward
progress (reject implausible jumps); no guessed key turn signs or rate defaults.

Keep runtime under~350lines. Verify PYTHONPATH=packages/automation/src:packages/common/src
uv run pytest tests/automation/test_movement.py -n0 -q, scoped ruff check/format and scoped
Pyright. Meaningful tests for wraparound/turn sign, waypoint success, monotonic deadlines,
wrong identity/stale/boot, no progress, stop, key bounds, exact profile/hash and no replay
assumptions. No implementation-mirroring tests. Report files, actual results, and DONE
only when finished. Parent owns wire/backend integration and review dispatch.
