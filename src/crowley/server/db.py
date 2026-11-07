"""Database connection and explicit migration management.

The migration ledger upgrade has a deliberately staged sequence.  The runner
first applies ``011_migration_ledger.sql`` in its own transaction; that SQL
adds a nullable ``checksum`` column so a production ledger containing only
the legacy ``name`` and ``applied_at`` columns remains readable.  In the next
runner-owned transaction, all NULL checksums are filled from the packaged file
bytes and the column is changed to NOT NULL.  This sequence is only reachable
through :func:`migrate` / :func:`apply_pending`; startup and ``--status`` only
read the ledger.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources

import psycopg
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

_MIGRATION_PACKAGE = "crowley.server.migrations"
_MIGRATION_NAME = re.compile(r"(?P<number>[0-9]{3})_[^/]+\.sql\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# This is a session-level advisory lock.  It is intentionally stable across
# processes and documented here so every migration caller uses the same lock.
MIGRATION_ADVISORY_LOCK_KEY = 0x43524F574C4559


class MigrationError(RuntimeError):
    """Base class for migration manifest, state, and verification errors."""


class MigrationManifestError(MigrationError):
    """Raised when packaged migration names or numbering are invalid."""


class MigrationStateError(MigrationError):
    """Raised when the database ledger cannot safely be upgraded."""


class MigrationVerificationError(MigrationError):
    """Raised when the database is not at the packaged migration revision."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One packaged migration and the SHA-256 of its exact file bytes."""

    name: str
    sha256: str


@dataclass(frozen=True, slots=True)
class MigrationManifest:
    """The validated, ordered migration manifest shipped with the server."""

    migrations: tuple[Migration, ...]

    def __post_init__(self) -> None:
        _validate_manifest_entries(self.migrations)

    @classmethod
    def from_package(cls) -> MigrationManifest:
        """Build a manifest from the packaged migration resources."""

        package = resources.files(_MIGRATION_PACKAGE)
        files = tuple(
            (entry.name, entry.read_bytes()) for entry in package.iterdir() if entry.name.endswith(".sql")
        )
        return cls.from_files(files)

    @classmethod
    def from_files(cls, files: Iterable[tuple[str, bytes]]) -> MigrationManifest:
        """Build and validate a manifest from ``(name, exact_bytes)`` pairs."""

        files = tuple(files)
        if not files:
            raise MigrationManifestError("migration manifest is empty")

        migrations: list[tuple[int, Migration]] = []
        seen_names: set[str] = set()
        seen_numbers: set[int] = set()
        for name, contents in files:
            if not isinstance(name, str):
                raise MigrationManifestError("migration filename must be text")
            match = _MIGRATION_NAME.fullmatch(name)
            if match is None:
                raise MigrationManifestError(f"invalid migration filename {name!r}; expected NNN_*.sql")
            if name in seen_names:
                raise MigrationManifestError(f"duplicate migration filename {name!r}")
            number = int(match.group("number"))
            if number in seen_numbers:
                raise MigrationManifestError(f"duplicate migration number {number:03d}")
            if not isinstance(contents, bytes):
                raise MigrationManifestError(f"migration {name!r} contents must be bytes")
            seen_names.add(name)
            seen_numbers.add(number)
            migrations.append((number, Migration(name=name, sha256=hashlib.sha256(contents).hexdigest())))

        migrations.sort(key=lambda entry: entry[0])
        expected_numbers = tuple(range(1, len(migrations) + 1))
        actual_numbers = tuple(number for number, _ in migrations)
        if actual_numbers != expected_numbers:
            expected = ", ".join(f"{number:03d}" for number in expected_numbers)
            actual = ", ".join(f"{number:03d}" for number in actual_numbers)
            raise MigrationManifestError(
                f"migration numbering is not contiguous: expected [{expected}], found [{actual}]"
            )
        return cls(migrations=tuple(migration for _, migration in migrations))

    @property
    def names(self) -> tuple[str, ...]:
        """Return migration names in application order."""

        return tuple(migration.name for migration in self.migrations)


def _validate_manifest_entries(migrations: tuple[Migration, ...]) -> None:
    if not migrations:
        raise MigrationManifestError("migration manifest is empty")
    seen_names: set[str] = set()
    seen_numbers: set[int] = set()
    numbers: list[int] = []
    for migration in migrations:
        match = _MIGRATION_NAME.fullmatch(migration.name)
        if match is None:
            raise MigrationManifestError(f"invalid migration filename {migration.name!r}; expected NNN_*.sql")
        if migration.name in seen_names:
            raise MigrationManifestError(f"duplicate migration filename {migration.name!r}")
        number = int(match.group("number"))
        if number in seen_numbers:
            raise MigrationManifestError(f"duplicate migration number {number:03d}")
        if _SHA256.fullmatch(migration.sha256) is None:
            raise MigrationManifestError(f"migration {migration.name!r} has an invalid SHA-256")
        seen_names.add(migration.name)
        seen_numbers.add(number)
        numbers.append(number)
    expected_numbers = tuple(range(1, len(numbers) + 1))
    if tuple(numbers) != expected_numbers:
        expected = ", ".join(f"{number:03d}" for number in expected_numbers)
        actual = ", ".join(f"{number:03d}" for number in numbers)
        raise MigrationManifestError(
            f"migration numbering is not contiguous: expected [{expected}], found [{actual}]"
        )


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """Read-only comparison between the database ledger and the package."""

    applied: tuple[str, ...]
    pending: tuple[str, ...]
    unknown_applied: tuple[str, ...]
    checksum_mismatch: tuple[str, ...]
    checksum_missing: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        """Whether the ledger is complete, known, and checksum verified."""

        return not (self.pending or self.unknown_applied or self.checksum_mismatch or self.checksum_missing)


def create_pool(database_url: str) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=1, max_size=8, open=False)
    pool.open(wait=True, timeout=30)
    return pool


def migration_status(conn: psycopg.Connection[tuple[object, ...]]) -> MigrationStatus:
    """Return migration state without creating or changing any database object."""

    manifest = MigrationManifest.from_package()
    if not _ledger_exists(conn):
        return MigrationStatus(
            applied=(),
            pending=manifest.names,
            unknown_applied=(),
            checksum_mismatch=(),
            checksum_missing=(),
        )
    return _read_status(conn, manifest)


def verify_current(conn: psycopg.Connection[tuple[object, ...]]) -> MigrationStatus:
    """Verify startup may continue without mutating the schema."""

    status = migration_status(conn)
    if not status.is_current:
        details: list[str] = []
        if status.pending:
            details.append(f"pending: {', '.join(status.pending)}")
        if status.unknown_applied:
            details.append(f"unknown applied: {', '.join(status.unknown_applied)}")
        if status.checksum_mismatch:
            details.append(f"checksum mismatch: {', '.join(status.checksum_mismatch)}")
        if status.checksum_missing:
            details.append(f"checksum missing: {', '.join(status.checksum_missing)}")
        detail = "; ".join(details)
        raise MigrationVerificationError(
            f"database migrations are not current ({detail}); run `crowley-server migrate`"
        )
    return status


def apply_pending(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    """Apply pending migrations under the process-wide session advisory lock."""

    manifest = MigrationManifest.from_package()
    with _migration_lock(conn):
        _ensure_ledger(conn)
        status = _read_status(conn, manifest)
        _finish_read_transaction(conn)
        _refuse_unsafe_state(status, manifest)

        applied = set(status.applied)
        newly_applied: list[str] = []
        for migration in manifest.migrations:
            if migration.name in applied:
                continue
            contents = _packaged_bytes(migration)
            with conn.transaction():
                conn.execute(contents.decode("utf-8"))
                if _ledger_has_checksum(conn):
                    conn.execute(
                        "INSERT INTO schema_migrations (name, checksum) VALUES (%s, %s)",
                        (migration.name, migration.sha256),
                    )
                else:
                    # The legacy ledger has no checksum column until 011.
                    # 011's follow-up transaction backfills these rows.
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (migration.name,))
            log.info("applied migration %s", migration.name)
            newly_applied.append(migration.name)
            applied.add(migration.name)

        if "011_migration_ledger.sql" in applied:
            _backfill_and_require_checksums(conn, manifest)
        return newly_applied


def migrate(conn: psycopg.Connection[tuple[object, ...]]) -> list[str]:
    """Explicitly apply pending migrations and return names in order."""

    return apply_pending(conn)


def _read_status(
    conn: psycopg.Connection[tuple[object, ...]], manifest: MigrationManifest
) -> MigrationStatus:
    if _ledger_has_checksum(conn):
        rows = conn.execute("SELECT name, checksum FROM schema_migrations ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            "SELECT name, NULL::text AS checksum FROM schema_migrations ORDER BY name"
        ).fetchall()

    records = {str(row[0]): row[1] if row[1] is None else str(row[1]) for row in rows}
    packaged = {migration.name: migration for migration in manifest.migrations}
    unknown_applied = tuple(sorted(name for name in records if name not in packaged))
    applied = tuple(migration.name for migration in manifest.migrations if migration.name in records)
    pending = tuple(migration.name for migration in manifest.migrations if migration.name not in records)
    checksum_mismatch = tuple(
        migration.name
        for migration in manifest.migrations
        if migration.name in records
        and records[migration.name] is not None
        and records[migration.name] != migration.sha256
    )
    checksum_missing = tuple(
        migration.name
        for migration in manifest.migrations
        if migration.name in records and records[migration.name] is None
    )
    return MigrationStatus(
        applied=applied,
        pending=pending,
        unknown_applied=unknown_applied,
        checksum_mismatch=checksum_mismatch,
        checksum_missing=checksum_missing,
    )


def _refuse_unsafe_state(status: MigrationStatus, manifest: MigrationManifest) -> None:
    if status.unknown_applied:
        raise MigrationStateError(
            "database has migrations unknown to this package: " + ", ".join(status.unknown_applied)
        )
    if status.checksum_mismatch:
        raise MigrationStateError("migration checksum mismatch: " + ", ".join(status.checksum_mismatch))
    expected_applied = manifest.names[: len(status.applied)]
    if status.applied != expected_applied:
        raise MigrationStateError(
            "database migration ledger is not a contiguous prefix of the packaged manifest"
        )


def _packaged_bytes(migration: Migration) -> bytes:
    contents = resources.files(_MIGRATION_PACKAGE).joinpath(migration.name).read_bytes()
    actual_sha256 = hashlib.sha256(contents).hexdigest()
    if actual_sha256 != migration.sha256:
        raise MigrationStateError(
            f"packaged migration {migration.name} changed while applying; restart the migrate command"
        )
    return contents


def _ensure_ledger(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )


def _ledger_exists(conn: psycopg.Connection[tuple[object, ...]]) -> bool:
    return conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0] is not None


def _ledger_has_checksum(conn: psycopg.Connection[tuple[object, ...]]) -> bool:
    return bool(
        conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_attribute
                WHERE attrelid = to_regclass('schema_migrations')
                  AND attname = 'checksum'
                  AND NOT attisdropped
            )
            """
        ).fetchone()[0]
    )


def _backfill_and_require_checksums(
    conn: psycopg.Connection[tuple[object, ...]], manifest: MigrationManifest
) -> None:
    if not _ledger_has_checksum(conn):
        raise MigrationStateError("migration 011 is recorded but schema_migrations.checksum is absent")

    nullable = conn.execute(
        """
        SELECT is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'schema_migrations'
          AND column_name = 'checksum'
        """
    ).fetchone()
    if nullable is None:
        raise MigrationStateError("schema_migrations.checksum is not visible in the current schema")
    if nullable[0] == "NO":
        _finish_read_transaction(conn)
        return

    checksums = {migration.name: migration.sha256 for migration in manifest.migrations}
    _finish_read_transaction(conn)
    with conn.transaction():
        for name, checksum in checksums.items():
            conn.execute(
                "UPDATE schema_migrations SET checksum = %s WHERE name = %s AND checksum IS NULL",
                (checksum, name),
            )
        null_count = conn.execute("SELECT count(*) FROM schema_migrations WHERE checksum IS NULL").fetchone()[
            0
        ]
        if null_count:
            raise MigrationStateError(
                "cannot make schema_migrations.checksum NOT NULL; an applied migration is unknown"
            )
        conn.execute("ALTER TABLE schema_migrations ALTER COLUMN checksum SET NOT NULL")


def _finish_read_transaction(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    if not conn.autocommit and conn.info.transaction_status != TransactionStatus.IDLE:
        conn.rollback()


@contextmanager
def _migration_lock(conn: psycopg.Connection[tuple[object, ...]]) -> Iterator[None]:
    _prepare_idle_connection(conn)

    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
    _finish_read_transaction(conn)
    try:
        yield
    finally:
        if not conn.autocommit and conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
        _finish_read_transaction(conn)


def _prepare_idle_connection(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    status = conn.info.transaction_status
    if status == TransactionStatus.INERROR:
        raise MigrationStateError("migration connection has a failed transaction; rollback before migrating")
    if status == TransactionStatus.IDLE:
        return
    if conn.execute("SELECT txid_current_if_assigned()").fetchone()[0] is not None:
        raise MigrationStateError(
            "migration connection has uncommitted changes; commit or rollback before migrating"
        )
    conn.rollback()
