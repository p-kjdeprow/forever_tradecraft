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
client operation staging and bounded measured movement. Seller tests cover matched-price
cadence, conserved stock/spending, selective commodity returns, cancellation chunks, explicit
resume and interrupted-route/mail recovery. Native tests exercise query barriers, frozen
inspection generations, cache invalidation, attachment reindexing and actual-charge receipts.
Package tests build wheels and
sdists, check exact owned resources, and reject forbidden component imports.

Startup checks exercise session discovery, process/window identity, bounded recovery transitions
and control-child lifetime without touching the desktop. Recorded UI crops live under
tests/fixtures/startup with source digests and crop provenance; account-containing originals
remain private. Their OCR checks use Tesseract 5 with English data, and skip when the executable
is absent. Recorded-image tests use a fixed injected clock; separate tests verify capture expiry
before and during OCR. Production freshness limits remain enforced. Recorded screenshots are
offline regression evidence, never authority for live input.

The Lua harness models native behavior; it does not prove actual protected-call semantics.
Live qualification needs the selected client build and exact authorized batch. A dispatched
input is distinct from a callback, and a callback from a verified native postcondition.
