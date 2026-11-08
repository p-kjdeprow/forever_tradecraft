"""Database-gated query-plan coverage for the bounded volume read."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from crowley.server.db import migrate
from crowley.store.sweeps import VOLUME_QUERY

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")]


@pytest.fixture
def db_conn() -> psycopg.Connection:
    assert DB_URL is not None
    with psycopg.connect(DB_URL) as conn:
        migrate(conn)
        yield conn


def test_volume_query_uses_house_time_index_for_bounded_window(db_conn: psycopg.Connection) -> None:
    """The time predicate must remain an index range, even on an empty test DB."""

    assert DB_URL is not None
    since = datetime.now(UTC) - timedelta(hours=24, minutes=30)
    with db_conn.transaction():
        db_conn.execute("SET LOCAL enable_seqscan = off")
        plan = db_conn.execute(
            "EXPLAIN (COSTS OFF) " + VOLUME_QUERY,
            (1, "2589", since),
        ).fetchall()
    text = "\n".join(str(row[0]) for row in plan)
    assert re.search(r"(?:Index Scan|Index Only Scan|Bitmap Heap Scan)(?: using \S+)? on market_sweeps", text)
    assert re.search(r"(?:Index Cond|Recheck Cond):.*finished_at\s*>=", text)
    assert "Seq Scan on market_observations" not in text
