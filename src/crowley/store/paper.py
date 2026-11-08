"""Read models for paper-trading routes."""

from __future__ import annotations

from typing import LiteralString, cast

from psycopg.rows import dict_row

from ._types import Connection, JsonObject


def _paper_run_query() -> LiteralString:
    return """
        SELECT pr.id, pr.name, h.key AS house, pr.client_build, pr.start_t,
               pr.capital_copper, pr.strategies, pr.weights, pr.demand_share,
               pr.created_at, latest.step_t AS latest_step_t,
               latest.equity AS latest_equity, latest.realized AS latest_realized,
               pending.step_t AS pending_step_t
        FROM paper_runs AS pr
        JOIN houses AS h ON h.id = pr.house_id
        LEFT JOIN LATERAL (
            SELECT step_t, equity, realized
            FROM paper_steps
            WHERE run_id = pr.id
            ORDER BY step_t DESC
            LIMIT 1
        ) AS latest ON TRUE
        LEFT JOIN paper_pending AS pending ON pending.run_id = pr.id
    """


def list_paper_runs(conn: Connection) -> list[JsonObject]:
    """Read the summary row for every paper run."""

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(_paper_run_query() + " ORDER BY pr.name").fetchall()
    return [cast(JsonObject, dict(row)) for row in rows]


def paper_run_detail(
    conn: Connection, name: str
) -> tuple[JsonObject | None, list[JsonObject], list[JsonObject], JsonObject | None]:
    """Read a repeatable paper-run detail snapshot."""

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SET TRANSACTION READ ONLY")
        run = cur.execute(_paper_run_query() + " WHERE pr.name = %s", (name,)).fetchone()
        if run is None:
            return None, [], [], None
        curve_rows = cur.execute(
            """
            SELECT step_t, equity, realized, cash
            FROM paper_steps
            WHERE run_id = %s
            ORDER BY step_t
            """,
            (run["id"],),
        ).fetchall()
        intent_rows = cur.execute(
            """
            SELECT step_t AS t, strategy, ordinal, intent, outcome, detail
            FROM paper_intents
            WHERE run_id = %s
            ORDER BY step_t DESC, strategy, ordinal DESC
            LIMIT 200
            """,
            (run["id"],),
        ).fetchall()
        pending = cur.execute(
            """
            SELECT step_t AS t, sweep_key, intents
            FROM paper_pending
            WHERE run_id = %s
            """,
            (run["id"],),
        ).fetchone()
    return (
        cast(JsonObject, dict(run)),
        [cast(JsonObject, dict(row)) for row in curve_rows],
        [cast(JsonObject, dict(row)) for row in intent_rows],
        None if pending is None else cast(JsonObject, dict(pending)),
    )
