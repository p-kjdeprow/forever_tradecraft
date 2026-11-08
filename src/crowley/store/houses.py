"""Persistence and read models for auction houses."""

from __future__ import annotations

from typing import cast

from psycopg.rows import dict_row

from crowley.core.house import House

from ._types import Connection, JsonObject


class HouseNotFoundError(LookupError):
    """The requested house has no persisted data."""


def ensure_house(conn: Connection, house: House) -> int:
    """Return the stable database id for ``house``, inserting it if needed."""

    with conn.cursor() as cur:
        row = cur.execute(
            "INSERT INTO houses (key, region, ruleset, faction) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key RETURNING id",
            (house.key, house.region, house.ruleset, house.faction),
        ).fetchone()
    if row is None:
        raise RuntimeError("house upsert did not return an id")
    return cast(int, row[0])


def house_id(conn: Connection, house: House) -> int:
    """Return the id for an existing house or raise a domain error."""

    with conn.cursor() as cur:
        row = cur.execute("SELECT id FROM houses WHERE key = %s", (house.key,)).fetchone()
    if row is None:
        raise HouseNotFoundError(f"no data for house {house.key}")
    return cast(int, row[0])


def list_houses(conn: Connection) -> list[JsonObject]:
    """List houses without multiplying legacy scans by modern sweeps."""

    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            """
            SELECT h.key, h.region, h.ruleset, h.faction,
                   (SELECT count(*) FROM scans AS s WHERE s.house_id = h.id) AS legacy_scans,
                   (SELECT count(*) FROM market_sweeps AS ms WHERE ms.house_id = h.id) AS sweeps,
                   (SELECT count(*) FROM scans AS s WHERE s.house_id = h.id)
                     + (SELECT count(*) FROM market_sweeps AS ms WHERE ms.house_id = h.id) AS scans,
                   GREATEST(
                       (SELECT max(s.scanned_at) FROM scans AS s WHERE s.house_id = h.id),
                       (SELECT max(ms.finished_at) FROM market_sweeps AS ms WHERE ms.house_id = h.id)
                   ) AS last_scan_at,
                   (SELECT s.num_listings
                    FROM scans AS s
                    WHERE s.house_id = h.id
                    ORDER BY s.scanned_at DESC
                    LIMIT 1) AS last_scan_listings
            FROM houses AS h
            ORDER BY h.key
            """
        ).fetchall()
    return [cast(JsonObject, dict(row)) for row in rows]
