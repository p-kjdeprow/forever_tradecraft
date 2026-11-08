"""Health-check database adapter."""

from __future__ import annotations

from ._types import Connection


def check_connection(conn: Connection) -> None:
    """Raise if the connection cannot execute a trivial read."""

    with conn.cursor() as cur:
        cur.execute("SELECT 1")
