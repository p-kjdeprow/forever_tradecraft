# Use the packaged server interpreter for migration inspection

The 70959 restart needs a verified legacy backup before the market plan publisher
is replaced. A completed original legacy snapshot exists. Its old verifier scans
an absent `/app/src` directory. The new verifier uses package resources correctly,
but invokes `/usr/local/bin/python`, which cannot import either packaged server.
Actual old and candidate images both run `/app/.venv/bin/crowley-server`; direct
read-only inspection with `/app/.venv/bin/python` successfully reads all twelve
original legacy migration files. Their hashes match the snapshot and live ledger.

Luna may change only `packages/market/src/crowley_market/backup.py` and
`tests/packages/test_backup_image_contract.py`. Change only migration metadata
inspection to the single packaged `/app/.venv/bin/python` interpreter. Preserve
explicit manifest namespace selection, every compatibility/hash check, isolation,
image identity, snapshot bytes and cleanup. No alternate interpreter/import path,
retry, schema change or live service/database action. Do not change stdlib-only
raw archive helpers or the existing reference-migration helper.

Extend existing interpreter/namespace contract coverage without a new harness.
Run `uv run pytest -n0 tests/packages/test_backup_image_contract.py
tests/packages/test_backup_snapshot_image_contract.py tests/ops/test_backup.py -q`,
scoped Ruff/format, and diff checks. Report changes and actual results; DONE only
when complete. Sol reviews read-only. Parent independently exercises the corrected
source helper against both exact image IDs, checks hashes against packaged SQL,
then runs the supported disposable restore of the original legacy snapshot.
Rebuild the affected reviewed market wheel/image before deploying; retain original
snapshot/receipts and old image for rollback. No force or bypass is required.
