# Restore collection after the 70959 client update

The user authorized taking down the stranded client and restarting the full scanner.
The exact old client exited on 2026-11-17; no replacement was launched. Battle.net's
selected wowf product and active `.build.info` now independently report 1.60.1.70959.
Private observations are in the startup rollout qualification receipts.

This is an operational build promotion of the existing revision-9 collection plan.
It does not requalify historical item/export facts, seller operations or movement.
Both catalog and canonical plan hashes change despite retaining revision and item pools.

## Source scope and order

Luna owns only:

- `packages/common/src/crowley_common/data/targets/forever-us-camwow7.json`
- `packages/common/src/crowley_common/data/first_run.json`
- Generated artifacts written by `tools/generate_launch_target.py --write`
- Existing selected-target assertions in `tests/test_launch_target.py`, if required

Promote the full build to 1.60.1.70959 and mark routes explicitly unmeasured on that
build. Keep interface 16001 as a strict qualification expectation; a fresh native
publication must confirm it before collection is declared restored. Keep revision,
item IDs, pools, all historical item reasons, cadence, caps and wire schemas unchanged.
Do not change the seller's separately qualified build or dashboard/export evidence.
Do not modify runtime configuration, services, credentials, SQL or saved state.

## Verification and review

Run the generator with `--write`, then `--check`. Run `uv run pytest
tests/test_launch_target.py tests/test_watchlist.py tests/test_addon.py
tests/test_companion.py` using existing actual test paths, and report any missing
path instead of creating a substitute. Run scoped Ruff checks and `git diff --check`.
Sol independently reviews correctness, changed catalog fields and hashes, generated
resources, preserved historical provenance and runtime deployment requirements.
Parent runs relevant verification, builds coordinated reviewed wheels and the market
image, and checks that all twelve applied migrations retain their original bytes.

## Runtime order and acceptance

Keep host stopped and companion transport available. Preserve original Battle.net,
SavedVariables, ACKs, durable scan policy and disabled timers. Before replacing the
legacy market image, create and validate a snapshot with its matching preserved
legacy tool, verify its disposable restore, and retain the exact previous image/config for rollback. Verify the
actual database migration ledger read-only. Replace only the server service; retain
the database/raw volumes. No schema change or manual database write is required.

Install the reviewed desktop wheels and generated resources through the supported
installer, including re-rendering standalone automation's build pin. Let companion
publish the server's new canonical plan; do not edit Data.lua by hand. Verify server,
installed target and companion-written Data.lua plan agree, then
start normal host. Confirm exactly one new game, fresh build/interface/house/plan,
AH collection, a new original saved payload accepted by the server, retained prices
and ACK publication. Report loaded ACK separately. Stop on a contradictory native
contract instead of loosening pins. Land source and final handoff on main/origin/main;
remove only task-owned disposable artifacts.

## Current rollout deviation

The original legacy dump/raw snapshot completed and passed byte validation. The
disposable restore failed at `create_database` and verified cleanup of its own
resources. The user then explicitly prioritized immediate scanner recovery, so
the remaining restore investigation was deferred. The reviewed server-only cutover
preserved the original database container, DB/raw mounts and twelve migration
checksums; health and the authenticated new plan passed. The snapshot remains
unqualified for restore. See HANDOFF for current collection evidence.
