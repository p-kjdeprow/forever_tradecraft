"""Non-database tests for migration manifest validation."""

from __future__ import annotations

import hashlib

import pytest

from crowley.server.db import MigrationManifest, MigrationManifestError


def test_packaged_manifest_contains_the_new_ledger_migration() -> None:
    manifest = MigrationManifest.from_package()

    assert manifest.names[-1] == "011_migration_ledger.sql"
    assert len(manifest.migrations[-1].sha256) == 64


def test_manifest_is_ordered_and_hashes_exact_bytes() -> None:
    manifest = MigrationManifest.from_files(
        (
            ("002_second.sql", b"second\n"),
            ("001_first.sql", b"first\r\n"),
        )
    )

    assert manifest.names == ("001_first.sql", "002_second.sql")
    assert manifest.migrations[0].sha256 == hashlib.sha256(b"first\r\n").hexdigest()


def test_manifest_rejects_a_numbering_gap() -> None:
    with pytest.raises(MigrationManifestError, match="not contiguous"):
        MigrationManifest.from_files((("001_first.sql", b"first"), ("003_third.sql", b"third")))


def test_manifest_rejects_duplicate_filename() -> None:
    with pytest.raises(MigrationManifestError, match="duplicate migration filename"):
        MigrationManifest.from_files((("001_first.sql", b"first"), ("001_first.sql", b"different")))


def test_manifest_rejects_duplicate_number() -> None:
    with pytest.raises(MigrationManifestError, match="duplicate migration number"):
        MigrationManifest.from_files((("001_first.sql", b"first"), ("001_second.sql", b"second")))


@pytest.mark.parametrize(
    "name",
    ("1_first.sql", "001.sql", "001_first.txt", "001_first.sql/extra"),
)
def test_manifest_rejects_bad_name(name: str) -> None:
    with pytest.raises(MigrationManifestError, match="invalid migration filename"):
        MigrationManifest.from_files(((name, b"contents"),))
