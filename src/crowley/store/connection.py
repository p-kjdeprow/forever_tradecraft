"""Database connection policy shared by the server pool."""

from __future__ import annotations

# Every request uses the same bounded server-side statement timeout.  This is
# passed as a libpq connection option so it applies to every pooled session,
# including sessions created after the application starts.
STATEMENT_TIMEOUT_MS = 30_000
