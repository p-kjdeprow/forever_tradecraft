"""Shared types for the PostgreSQL adapters."""

from __future__ import annotations

import psycopg

type Connection = psycopg.Connection[tuple[object, ...]]
type JsonObject = dict[str, object]
