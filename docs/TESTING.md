# Testing

```sh
uv run pytest                      # default: 16 xdist workers, --dist worksteal (pyproject.toml)
uv run ruff check . && uv run ruff format --check .
```

The default suite needs no database and no game. Tests that need PostgreSQL/TimescaleDB are skipped unless
`CROWLEY_TEST_DATABASE_URL` is set. The server tests reset the shared schema for each test, so run them serially
(`-n0`) against a freshly created disposable database, never the production one, and never with xdist workers
(`tests/conftest.py` refuses that combination before collection):

```sh
CROWLEY_TEST_DATABASE_URL='<fresh disposable database URL>' \
  uv run pytest -n0 tests/test_server.py tests/test_client_data.py tests/test_trading_paper.py
```

Use `-n0` too for focused debugging. `tmp_path_retention_policy = "failed"` keeps only failed tests' directories.

## What is covered

- Scan loop (`tests/test_scan_*.py`): the policy table including each fault row and the 90 s reload margin, the
  typed-command transaction, relaunch backoff, presence on IdleMonitor, observation and the loop/CLI wiring.
- GUI layer (`tests/test_gui_*.py`): pointer planning and clicks, the one-click-per-second gate, the pinned
  XTEST tool and capture.
- `tests/test_import_smoke.py`: imports every console script and runs `--help` for each CLI.
- `tests/test_repo_hardcoded_paths.py`: no absolute paths or links in tracked files.
- Addon and data path: Lua harness (`tests/wow_harness.py`), scan format, companion, server, migrations.
- Trading and analysis: item/data identity, financial queue and paper-trading safety, golden statistics.
- Ops and platform: install, doctor, health, backup, launch target (`tests/ops`, `tests/platform`).

Live checks (a real game, desktop or database) are not part of the suite; see
[REPEATING_SCANS](REPEATING_SCANS.md) for the loop's live acceptance.
