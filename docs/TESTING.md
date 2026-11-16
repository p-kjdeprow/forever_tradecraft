# Testing

Run from the workspace root:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

The default suite uses 16 xdist workers and needs neither a game nor a database. Database
checks skip unless CROWLEY_TEST_DATABASE_URL is set. Their fixtures reset the schema, so use
a fresh disposable TimescaleDB and run serially. Never use the deployed database:

```sh
CROWLEY_TEST_DATABASE_URL='<disposable test database URL>' uv run pytest -n0
```

The fixture rejects a shared database URL with xdist. Tests retain only failed temporary
directories. Type checking requires zero errors across the five workspace source roots.

Tests cover retained dashboard economics/activity/caching, canonical scan formats and
migration ledger, native scanner lifecycle and transport/ACK behavior, host policy/input,
private filesystem and backup contracts, strict operation batches, crash/receipt ambiguity,
client operation staging and bounded measured movement. Package tests build wheels and
sdists, check exact owned resources, and reject forbidden component imports.

The Lua harness models native behavior; it does not prove actual protected-call semantics.
Live qualification needs the selected client build and exact authorized batch. A dispatched
input is distinct from a callback, and a callback from a verified native postcondition.
