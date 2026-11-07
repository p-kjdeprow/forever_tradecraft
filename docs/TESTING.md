# Testing

The default suite is the launch-readiness gate. It runs the retained unit,
contract, process, and native-boundary tests in parallel with sixteen bounded
workers on the 16-CPU host. Native tests are not hidden behind a default
marker exclusion; the suite still executes their independent safety oracles.

Run the default suite with the pinned Rust toolchain and the machine-wide test
lock:

```sh
export CROWLEY_RUST_TOOLCHAIN=/path/to/rust-toolchain-1.85.1/toolchain/bin
export CROWLEY_TEST_LOCK=/path/to/r-pg.lock
flock "$CROWLEY_TEST_LOCK" uv run pytest
```

Native builds require that same `CROWLEY_RUST_TOOLCHAIN` directory; they fail
if it is unset or any pinned binary version differs. The X11 observer also
requires `pkg-config --static --libs x11` to resolve its static library inputs.
Use `uv run crowley-native verify` before a native build.

Independent final verification on 2026-10-24: bare `uv run pytest` passed with
**2,455 passed, 25 database-gated skips and 33 warnings in 153.78 seconds** of
external wall time, excluding lock wait. All 83 frozen source hashes were
unchanged before/after the run. The parallel database guard was exercised
through the CLI and refused the shared-URL invocation before collection.
The separate serial disposable-database matrix passed **178 tests with zero
skips in 34.08 seconds**, including every default DB-gated case. Results and
source manifests are in `.archive/launch-readiness-20261023/`:
`parent-verification-final-r4.json`, `final-r4-source-sha256.json` and
`parent-database-final.json`.

The server tests reset the shared disposable database schema for each test, so
run them serially. Use a freshly created test database and never combine a shared
URL with `-n 16`. Do not use the production database.

The shared `tests/conftest.py` guard rejects any `CROWLEY_TEST_DATABASE_URL`
run with xdist workers before collection, so this cannot silently race or skip
the database coverage.

```sh
CROWLEY_TEST_DATABASE_URL='postgresql://crowley:testonly@127.0.0.1:55471/<fresh_test_database>' \
  flock "$CROWLEY_TEST_LOCK" uv run pytest -n0 tests/test_server.py tests/test_client_data.py tests/test_trading_paper.py
```

Replace `<fresh_test_database>` with your disposable database name. The launch
preparation verification uses a guarded task-owned database and runs every
default DB-gated case without skips.

The default worker count is configured in `pyproject.toml` as `-n 16` with
`--dist worksteal`. Pytest's `tmp_path_retention_policy = "failed"` removes
successful tests' newly created disposable directories and retains failure
evidence. Private Xvfb allocation is synchronized only through owned-display
readiness; test bodies continue in parallel. Use `-n0` for the documented DB
run or focused debugging, and still acquire the same lock. A complete
run should report zero failures and zero errors; skipped tests must be called
out in the launch handoff (database tests skip when no disposable database URL
is supplied).

The suite retains independent checks for item/data identity, financial queue
and paper-trading safety, the one-click-per-second cap, takeover and revoke
behaviour, process/watchdog cleanup, and native input fencing. Five duplicate
test functions and one forwarding helper were removed to keep the run bounded;
the replacement coverage is recorded in
`.archive/test-suite-refactor-20261023/removal-rationale.md`.
The fragile local ten-second statistics microbenchmark was also removed while
retaining its full 330-item × 240-sweep computation and result assertions; see
`.archive/test-suite-refactor-20261024/statistics-timing-rationale.md`.
The independently measured default-suite wall time is the performance gate.

For scoped verification:

```sh
uv run ruff check tests pyproject.toml
uv run ruff format --check tests
```
