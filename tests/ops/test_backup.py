from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import runpy
import subprocess
import tarfile
import uuid
import zipfile
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from crowley.ops import backup
from crowley.server.db import migrate
from crowley.server.ingest import _logical_digest

raw_scan = runpy.run_path(Path(__file__).parents[1] / "test_core.py")["raw_scan"]


def _manifest(raw_bytes: bytes, dump_bytes: bytes = b"custom dump") -> dict:
    return {
        "schema": backup.SCHEMA,
        "schema_version": 1,
        "backup_id": "beta-20261017T061327Z-012345abcdef",
        "created_at_utc": "2026-10-17T06:13:27Z",
        "source": {
            "compose_file_sha256": "1" * 64,
            "compose_project": "crowley",
            "services": {"database": "db", "server": "server"},
            "containers": {
                "database": {
                    "id": "a" * 64,
                    "image_id": "sha256:" + "b" * 64,
                    "repo_digests": ["timescale/db@sha256:" + "c" * 64],
                },
                "server": {
                    "id": "d" * 64,
                    "image_id": "sha256:" + "e" * 64,
                    "repo_digests": ["crowley/server@sha256:" + "f" * 64],
                },
            },
            "volumes": {
                "database": {"name": "crowley_db", "mountpoint": "/var/lib/docker/volumes/crowley_db/_data"},
                "raw_archive": {
                    "name": "crowley_raw",
                    "mountpoint": "/var/lib/docker/volumes/crowley_raw/_data",
                },
            },
            "networks": [{"name": "crowley_default", "id": "9" * 64}],
            "source_commit": "0" * 40,
            "dirty_tree_sha256": "2" * 64,
            "migrations": [
                {"path": "src/crowley/server/migrations/001_init.sql", "sha256": "3" * 64},
                {"path": "src/crowley/server/migrations/002_market_observations.sql", "sha256": "4" * 64},
            ],
            "postgres_server_version": "16.6",
            "timescaledb_extension_version": "2.17.2",
        },
        "database_dump": {
            "path": "database.dump",
            "format": "postgres-custom",
            "size_bytes": len(dump_bytes),
            "sha256": backup.sha256_bytes(dump_bytes),
        },
        "raw_archive": {
            "path": "raw.tar",
            "format": "tar",
            "size_bytes": len(raw_bytes),
            "sha256": backup.sha256_bytes(raw_bytes),
            "members": backup._hash_tar_members_from_bytes(raw_bytes),
        },
    }


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            info.mtime = 0
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _write_snapshot(tmp_path: Path, raw_members: dict[str, bytes] | None = None) -> Path:
    raw = _tar_bytes(raw_members or {})
    dump = b"custom dump"
    path = tmp_path / "beta-20261017T061327Z-012345abcdef"
    path.mkdir(mode=0o700, parents=True)
    (path / "database.dump").write_bytes(dump)
    (path / "raw.tar").write_bytes(raw)
    os.chmod(path / "database.dump", 0o600)
    os.chmod(path / "raw.tar", 0o600)
    manifest = _manifest(raw, dump)
    encoded = backup.canonical_json(manifest)
    (path / "manifest.json").write_bytes(encoded)
    os.chmod(path / "manifest.json", 0o600)
    (path / "COMPLETE").write_bytes(f"{backup.MARKER_PREFIX}{backup.sha256_bytes(encoded)}\n".encode())
    os.chmod(path / "COMPLETE", 0o600)
    os.chmod(path, 0o700)
    return path


def _valid_compose() -> dict:
    project = "crowley-drill-0123456789abcdef"
    return {
        "name": project,
        "services": {
            "db": {
                "image": "sha256:db",
                "restart": "no",
                "ports": [],
                "volumes": [{"type": "volume", "source": "crowley-db", "target": "/var/lib/postgresql/data"}],
            },
            "server": {
                "image": "sha256:server",
                "restart": "no",
                "ports": [],
                "volumes": [{"type": "volume", "source": "crowley-raw", "target": "/data"}],
            },
        },
        "networks": {"default": {"internal": True}},
        "volumes": {
            "crowley-db": {"name": project + "_crowley-db"},
            "crowley-raw": {"name": project + "_crowley-raw"},
        },
    }


def test_snapshot_manifest_and_complete_marker_validate(tmp_path: Path):
    snapshot = _write_snapshot(tmp_path, {"raw/format2/aa/example": b"abc"})
    manifest, digest = backup.validate_snapshot(snapshot)
    assert digest == backup.sha256_bytes((snapshot / "manifest.json").read_bytes())
    assert manifest["raw_archive"]["members"] == [
        {"path": "raw/format2/aa/example", "size_bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
    ]
    assert {p.name for p in snapshot.iterdir()} == {"database.dump", "raw.tar", "manifest.json", "COMPLETE"}


@pytest.mark.parametrize(
    "value",
    [
        "crowley-20261107T061327Z-012345abcdef",
        "beta-20261017T061327Z-012345abcdef",
    ],
)
def test_backup_id_formats_accept_new_and_named_historical_prefix(value: str) -> None:
    assert backup.BACKUP_ID_RE.fullmatch(value)


@pytest.mark.parametrize(
    "value",
    [
        "alpha-20261107T061327Z-012345abcdef",
        "crowley-20261107T061327Z-012345ABCDEf",
        "crowley-20261107T061327Z-0123456789ab0",
        "beta-20261107T061327Z-0123456789ab-extra",
    ],
)
def test_backup_id_formats_reject_unknown_prefix_or_shape(value: str) -> None:
    assert backup.BACKUP_ID_RE.fullmatch(value) is None


def _inventory_fixture() -> backup.SchemaInventory:
    return backup.SchemaInventory(
        tables=(backup.TableInventory("public", "orders"),),
        columns=(
            backup.ColumnInventory(
                "public", "orders", "id", "integer", False, "nextval('orders_id_seq'::regclass)"
            ),
        ),
        constraints=(backup.ConstraintInventory("public", "orders", "orders_pkey", "p", "PRIMARY KEY (id)"),),
        indexes=(
            backup.IndexInventory(
                "public",
                "orders",
                "orders_pkey",
                "CREATE UNIQUE INDEX orders_pkey ON public.orders USING btree (id)",
            ),
        ),
        views=(backup.ViewInventory("public", "order_view", " SELECT id FROM public.orders;"),),
    )


def test_schema_inventory_comparison_accepts_identical_inventories() -> None:
    inventory = _inventory_fixture()
    backup.compare_schema_inventories(inventory, inventory)


@pytest.mark.parametrize(
    ("attribute", "message"),
    [
        ("tables", "missing table public.orders"),
        ("columns", "missing column public.orders.id"),
        ("indexes", "missing index public.orders.orders_pkey"),
        ("constraints", "missing constraint public.orders.orders_pkey"),
    ],
)
def test_schema_inventory_comparison_reports_missing_objects(attribute: str, message: str) -> None:
    reference = _inventory_fixture()
    restored = replace(_inventory_fixture(), **{attribute: tuple()})
    with pytest.raises(backup.BackupError, match=message):
        backup.compare_schema_inventories(reference, restored)


def test_schema_inventory_comparison_reports_extra_object() -> None:
    reference = _inventory_fixture()
    restored = backup.SchemaInventory(
        tables=reference.tables + (backup.TableInventory("public", "extra"),),
        columns=reference.columns,
        constraints=reference.constraints,
        indexes=reference.indexes,
        views=reference.views,
    )
    with pytest.raises(backup.BackupError, match="extra table public.extra"):
        backup.compare_schema_inventories(reference, restored)


def test_schema_inventory_comparison_reports_column_type_change() -> None:
    reference = _inventory_fixture()
    restored = backup.SchemaInventory(
        tables=reference.tables,
        columns=(
            backup.ColumnInventory(
                "public", "orders", "id", "bigint", False, "nextval('orders_id_seq'::regclass)"
            ),
        ),
        constraints=reference.constraints,
        indexes=reference.indexes,
        views=reference.views,
    )
    with pytest.raises(backup.BackupError, match="changed column public.orders.id"):
        backup.compare_schema_inventories(reference, restored)


def test_incomplete_published_states_and_changed_bytes_fail_closed(tmp_path: Path):
    snapshot = _write_snapshot(tmp_path)
    (snapshot / "INCOMPLETE").write_bytes(b"")
    os.chmod(snapshot / "INCOMPLETE", 0o600)
    with pytest.raises(backup.BackupError, match="incomplete"):
        backup.validate_snapshot(snapshot)
    (snapshot / "INCOMPLETE").unlink()
    (snapshot / "database.dump").write_bytes(b"corrupt")
    with pytest.raises(backup.BackupError, match="dump hash"):
        backup.validate_snapshot(snapshot)


def test_marker_and_manifest_canonical_bytes_are_bound(tmp_path: Path):
    snapshot = _write_snapshot(tmp_path)
    (snapshot / "COMPLETE").write_bytes(b"crowley.market-backup-complete-v1 " + b"0" * 64 + b"\n")
    with pytest.raises(backup.BackupError, match="marker"):
        backup.validate_snapshot(snapshot)
    snapshot = _write_snapshot(tmp_path / "other")
    (snapshot / "manifest.json").write_bytes((snapshot / "manifest.json").read_bytes() + b"\n")
    with pytest.raises(backup.BackupError, match="canonical"):
        backup.validate_snapshot(snapshot)


def test_duplicate_unknown_and_missing_manifest_fields_are_rejected():
    value = _manifest(_tar_bytes({}))
    value["not_allowed"] = 1
    with pytest.raises(backup.BackupError, match="fields"):
        backup.validate_manifest(value)
    value = _manifest(_tar_bytes({}))
    value["database_dump"]["size_bytes"] = True
    with pytest.raises(backup.BackupError, match="dump size"):
        backup.validate_manifest(value)
    with pytest.raises(backup.BackupError, match="duplicate"):
        backup.strict_json(b'{"a":1,"a":2}')


@pytest.mark.parametrize("member", ["../escape", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", ""])
def test_archive_inventory_rejects_unsafe_paths(tmp_path: Path, member: str):
    archive_path = tmp_path / "bad.tar"
    with tarfile.open(archive_path, "w:") as archive:
        info = tarfile.TarInfo(member)
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(backup.BackupError):
        backup._hash_tar_members(archive_path)


def test_archive_inventory_rejects_links_directories_duplicates_and_extra_members(tmp_path: Path):
    link_path = tmp_path / "link.tar"
    with tarfile.open(link_path, "w:") as archive:
        link = tarfile.TarInfo("safe/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../escape"
        archive.addfile(link)
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup._hash_tar_members(link_path)

    directory_path = tmp_path / "directory.tar"
    with tarfile.open(directory_path, "w:") as archive:
        directory = tarfile.TarInfo("safe/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup._hash_tar_members(directory_path)

    duplicate_path = tmp_path / "duplicate.tar"
    with tarfile.open(duplicate_path, "w:") as archive:
        for value in (b"first", b"second"):
            info = tarfile.TarInfo("same")
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
    with pytest.raises(backup.BackupError, match="unsafe"):
        backup._hash_tar_members(duplicate_path)

    snapshot = _write_snapshot(tmp_path / "extra", {"safe": b"one"})
    with tarfile.open(snapshot / "raw.tar", "a:") as archive:
        info = tarfile.TarInfo("orphan")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(backup.BackupError, match="raw archive hash"):
        backup.validate_snapshot(snapshot)


def test_snapshot_rejects_extra_root_entries_and_symlinked_files(tmp_path: Path):
    snapshot = _write_snapshot(tmp_path)
    (snapshot / "extra").write_bytes(b"x")
    os.chmod(snapshot / "extra", 0o600)
    with pytest.raises(backup.BackupError, match="extra entries"):
        backup.validate_snapshot(snapshot)


def test_raw_paths_require_exact_data_prefix_and_inventory_membership():
    inventory = {"raw/format2/ab/object.json.gz"}
    assert (
        backup.raw_reference_path("/data/raw/format2/ab/object.json.gz", inventory)
        == "raw/format2/ab/object.json.gz"
    )
    for value in ("/datax/raw/format2/ab/object.json.gz", "/tmp/object", "/data/../escape", "/data/missing"):
        with pytest.raises(backup.BackupError):
            backup.raw_reference_path(value, inventory)


def test_restored_raw_reference_verifier_fails_closed_for_legacy_and_missing_members(
    tmp_path: Path, monkeypatch
):
    archive_path = tmp_path / "raw.tar"
    archive_path.write_bytes(_tar_bytes({"present": b"evidence"}))
    manifest = _manifest(archive_path.read_bytes())

    with monkeypatch.context() as patcher:
        patcher.setattr(
            backup,
            "_json_sql",
            lambda *_args, **_kwargs: [{"id": 1, "scan_uid": "legacy", "raw_path": "legacy.gz"}],
        )
        with pytest.raises(backup.BackupError, match="unsupported_legacy_reference"):
            backup._raw_references("db-id", archive_path, manifest)

    outputs = iter(
        [
            [],
            [
                {
                    "id": 1,
                    "sweep_uid": "sweep",
                    "house_key": "us-normal-alliance",
                    "plan_hash": "a" * 64,
                    "client_build": "70338",
                    "interface": 16001,
                    "content_digest": "b" * 64,
                    "raw_path": "/data/missing",
                    "plan_json": "{}",
                }
            ],
            [],
        ]
    )
    with monkeypatch.context() as patcher:
        patcher.setattr(backup, "_json_sql", lambda *_args, **_kwargs: next(outputs))
        with pytest.raises(backup.BackupError, match="absent from archive inventory"):
            backup._raw_references("db-id", archive_path, manifest)


def test_format2_reference_matches_restored_rows_and_rejects_identity_drift():
    envelope = raw_scan()
    from crowley.core.scanformat import validate_v2_envelope

    decoded = validate_v2_envelope(envelope)["decoded"]
    digest = _logical_digest(envelope, decoded)
    plan = decoded["planJSON"]
    path = f"raw/format2/{digest[:2]}/{digest}.json.gz"
    row = {
        "content_digest": digest,
        "inventory_path": path,
        "sweep_uid": envelope["id"],
        "house_key": envelope["house"]["key"],
        "client_build": envelope["context"]["clientBuild"],
        "interface": envelope["context"]["interface"],
        "plan_hash": decoded["planHash"],
    }
    member = gzip.compress(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())
    proof = backup.validate_format2_member(member, row, {decoded["planHash"]: plan})
    assert proof["logical_digest"] == digest
    row["sweep_uid"] = "different"
    with pytest.raises(backup.BackupError, match="sweep identity"):
        backup.validate_format2_member(member, row, {decoded["planHash"]: plan})


def test_orphans_are_classified_only_after_reference_set_is_known():
    inventory = [
        {"path": "raw/format2/aa/a.json.gz", "sha256": "a" * 64},
        {"path": "legacy/old.gz", "sha256": "b" * 64},
    ]
    result = backup.classify_orphans(inventory, {"raw/format2/aa/a.json.gz"})
    assert result == [
        {
            "path": "legacy/old.gz",
            "sha256": "b" * 64,
            "validation": "unvalidated",
            "warning": "not independently validated",
        }
    ]


def test_acceptance_counts_must_be_derived_from_restored_dump():
    counts = {"houses": 0, "market_sweeps": 0, "schema_migrations": 0}
    reference_table_keys = tuple(sorted(counts))
    proof = backup.db_count_provenance(counts, "restored_dump", reference_table_keys)
    assert proof["source"] == "restored_dump"
    with pytest.raises(backup.BackupError, match="restored dump"):
        backup.db_count_provenance(counts, "live_database", reference_table_keys)


def test_migration_inventory_uses_packaged_relative_paths():
    result = backup._packaged_migrations()
    assert [item["path"] for item in result] == [
        f"src/crowley/server/migrations/{name}"
        for name in sorted(
            (path.name for path in Path("src/crowley/server/migrations").glob("*.sql")),
            key=lambda value: value.encode("utf-8"),
        )
    ]


def test_reference_inventory_lifecycle_drops_database_after_inventory() -> None:
    events: list[str] = []
    expected = _inventory_fixture()

    def create(database_name: str) -> None:
        events.append(f"create:{database_name}")

    def migrate_database(database_name: str) -> None:
        events.append(f"migrate:{database_name}")

    def inventory(database_name: str) -> backup.SchemaInventory:
        events.append(f"inventory:{database_name}")
        return expected

    def drop(database_name: str) -> None:
        events.append(f"drop:{database_name}")

    def prepare(database_name: str) -> None:
        events.append(f"prepare:{database_name}")

    assert (
        backup.build_reference_inventory("reference", create, prepare, migrate_database, inventory, drop)
        == expected
    )
    assert events == [
        "create:reference",
        "prepare:reference",
        "migrate:reference",
        "inventory:reference",
        "drop:reference",
    ]

    events.clear()

    def failing_migrate(database_name: str) -> None:
        events.append(f"migrate:{database_name}")
        raise backup.BackupError("migration failed")

    with pytest.raises(backup.BackupError, match="migration failed"):
        backup.build_reference_inventory("reference", create, prepare, failing_migrate, inventory, drop)
    assert events == ["create:reference", "prepare:reference", "migrate:reference", "drop:reference"]


@pytest.mark.skipif(
    not os.environ.get("CROWLEY_TEST_DATABASE_URL"), reason="CROWLEY_TEST_DATABASE_URL not set"
)
def test_catalog_inventory_builds_from_real_packaged_migrations() -> None:
    database_url = os.environ["CROWLEY_TEST_DATABASE_URL"]
    expected_manifest = backup._packaged_migrations()
    maintenance_options = conninfo_to_dict(database_url)
    maintenance_options["dbname"] = "postgres"
    maintenance_url = make_conninfo(**maintenance_options)

    def create_database(database_name: str) -> None:
        with psycopg.connect(maintenance_url, autocommit=True) as connection:
            connection.execute(f"CREATE DATABASE {backup._sql_identifier(database_name)}")

    def reference_url(database_name: str) -> str:
        target_options = conninfo_to_dict(database_url)
        target_options["dbname"] = database_name
        return make_conninfo(**target_options)

    def migrate_database(database_name: str) -> None:
        with psycopg.connect(reference_url(database_name)) as connection:
            assert migrate(connection) == [Path(item["path"]).name for item in expected_manifest]

    def prepare(database_name: str) -> None:
        del database_name

    def inventory_database(database_name: str) -> backup.SchemaInventory:
        with psycopg.connect(reference_url(database_name)) as connection:

            def query(sql: str) -> object:
                value = connection.execute(sql).fetchone()[0]
                return json.loads(value) if isinstance(value, str) else value

            return backup.catalog_inventory(query)

    def drop_database(database_name: str) -> None:
        with psycopg.connect(maintenance_url, autocommit=True) as connection:
            connection.execute(f"DROP DATABASE {backup._sql_identifier(database_name)}")

    inventory = backup.build_reference_inventory(
        f"crowley_reference_test_{uuid.uuid4().hex[:16]}",
        create_database,
        prepare,
        migrate_database,
        inventory_database,
        drop_database,
    )
    assert any(table.name == "schema_migrations" for table in inventory.tables)
    assert all(table.schema not in backup.TIMESCALE_INTERNAL_SCHEMAS for table in inventory.tables)


def _compatibility_facts(source: dict) -> dict:
    return {
        "compose_file_sha256": source["compose_file_sha256"],
        "migrations": deepcopy(source["migrations"]),
        "postgres_server_version": source["postgres_server_version"],
        "timescaledb_extension_version": source["timescaledb_extension_version"],
        "containers": {
            key: {
                "image_id": value["image_id"],
                "repo_digests": deepcopy(value["repo_digests"]),
            }
            for key, value in source["containers"].items()
        },
        "server_image_migrations": deepcopy(source["migrations"]),
    }


def _new_manifest(raw_bytes: bytes) -> dict:
    value = _manifest(raw_bytes)
    source = value["source"]
    with backup._packaged_resources() as packaged:
        source["package_name"] = packaged.package_name
        source["package_version"] = packaged.package_version
        source["compose_file_sha256"] = packaged.compose_file_sha256
        source["migrations"] = list(packaged.migrations)
        source["deployment_resources_sha256"] = packaged.deployment_resources_sha256
    source.pop("source_commit")
    source.pop("dirty_tree_sha256")
    value["schema_version"] = backup.CURRENT_SCHEMA_VERSION
    value["backup_id"] = "crowley-20261107T061327Z-012345abcdef"
    return value


def test_new_schema_manifest_binds_packaged_resources_and_has_no_git_provenance() -> None:
    manifest = _new_manifest(_tar_bytes({}))
    validated = backup.validate_manifest(manifest)
    assert validated["schema_version"] == backup.CURRENT_SCHEMA_VERSION
    assert set(validated["source"]) >= {
        "package_name",
        "package_version",
        "deployment_resources_sha256",
    }
    assert "source_commit" not in validated["source"]
    assert "dirty_tree_sha256" not in validated["source"]


def test_snapshot_builds_new_manifest_from_packaged_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_home = tmp_path / "config" / "crowley"
    runtime = tmp_path / "runtime"
    config_home.mkdir(parents=True)
    runtime.mkdir(mode=0o700)
    secrets = config_home / "secrets.env"
    secrets.write_text("CROWLEY_DB_PASSWORD=fixture\nCROWLEY_API_TOKEN=fixture\n", encoding="utf-8")
    os.chmod(secrets, 0o600)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home.parent))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    source_containers = {
        "db": {"id": "a" * 64, "image_id": "sha256:" + "b" * 64},
        "server": {"id": "c" * 64, "image_id": "sha256:" + "d" * 64},
    }
    monkeypatch.setattr(
        backup,
        "_source_resources",
        lambda _compose, _env: (
            {"name": "crowley"},
            source_containers,
            {
                "volumes": {
                    "crowley-db": {"name": "db-volume", "mountpoint": "/db"},
                    "crowley-raw": {"name": "raw-volume", "mountpoint": "/raw"},
                },
                "networks": [],
            },
        ),
    )
    monkeypatch.setattr(
        backup,
        "_image_identity",
        lambda image_id: {"id": image_id, "repo_digests": []},
    )
    monkeypatch.setattr(backup, "_local_version", lambda _container_id: ("16.6", "2.17.2"))
    monkeypatch.setattr(
        backup,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=b"fixture dump"),
    )

    def capture_raw(_volume: str, _image: str, output: Path) -> None:
        output.write_bytes(_tar_bytes({"raw/object": b"fixture raw"}))
        os.chmod(output, 0o600)

    monkeypatch.setattr(backup, "_capture_raw_volume", capture_raw)

    snapshot = backup.snapshot(tmp_path / "backups")
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == backup.CURRENT_SCHEMA_VERSION
    assert manifest["source"]["package_name"] == "crowley"
    assert "source_commit" not in manifest["source"]
    assert "dirty_tree_sha256" not in manifest["source"]


def test_new_schema_resource_digest_mismatch_refuses_drill_compatibility() -> None:
    manifest = _new_manifest(_tar_bytes({}))
    recorded = manifest["source"]
    current = deepcopy(recorded)
    current["deployment_resources_sha256"] = "f" * 64
    with pytest.raises(backup.BackupError, match="packaged deployment resources"):
        backup.validate_source_compatibility(recorded, current, schema_version=backup.CURRENT_SCHEMA_VERSION)


def test_new_schema_package_version_is_provenance_only() -> None:
    manifest = _new_manifest(_tar_bytes({}))
    recorded = manifest["source"]
    current = _compatibility_facts(recorded)
    current.update(
        {
            "package_name": recorded["package_name"],
            "package_version": "0.1.1",
            "deployment_resources_sha256": recorded["deployment_resources_sha256"],
        }
    )
    backup.validate_source_compatibility(recorded, current, schema_version=backup.CURRENT_SCHEMA_VERSION)


def test_drill_rejects_resource_digest_before_secret_or_docker_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _new_manifest(_tar_bytes({}))
    manifest["source"]["deployment_resources_sha256"] = "f" * 64
    monkeypatch.setattr(backup, "validate_snapshot", lambda _path: (manifest, "a" * 64))

    def forbidden_secrets() -> Path:
        pytest.fail("secrets lookup must not occur before resource compatibility")

    def forbidden_source(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Docker source inspection must not occur before resource compatibility")

    def forbidden_docker(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Docker operation must not occur before resource compatibility")

    monkeypatch.setattr(backup, "secrets_path", forbidden_secrets)
    monkeypatch.setattr(backup, "_source_resources", forbidden_source)
    monkeypatch.setattr(backup, "_docker", forbidden_docker)

    with pytest.raises(backup.BackupError, match="packaged deployment resources"):
        backup.drill(tmp_path / "snapshot", tmp_path / "backups")


def test_wheel_contains_namespaced_compose_only_and_preserves_source_parity(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).parents[2]
    packaged = backup._compose_resource()
    assert packaged.read_bytes() == (repository / "src/crowley/ops/resources/docker-compose.yml").read_bytes()

    wheel_dir = tmp_path / "wheel"
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        files = {name for name in wheel.namelist() if not name.endswith("/")}
    compose_files = sorted(name for name in files if name.endswith("docker-compose.yml"))
    assert compose_files == ["crowley/ops/resources/docker-compose.yml"]
    assert not any(name.startswith("deploy/") for name in files)
    assert not any(".data/" in name for name in files)


def test_historical_resource_id_drift_does_not_block_restore_compatibility():
    recorded = deepcopy(_manifest(_tar_bytes({}))["source"])
    current = _compatibility_facts(recorded)
    recorded["source_commit"] = "f" * 40
    recorded["dirty_tree_sha256"] = "e" * 64
    recorded["containers"]["database"]["id"] = "f" * 64
    recorded["containers"]["server"]["id"] = "e" * 64
    recorded["volumes"]["database"]["name"] = "historical_db"
    recorded["volumes"]["raw_archive"]["name"] = "historical_raw"
    recorded["networks"] = [{"name": "historical_default", "id": "a" * 64}]
    backup.validate_source_compatibility(recorded, current)


@pytest.mark.parametrize("drift", ["migration", "image", "pinned_image_migrations"])
def test_restore_compatibility_rejects_migration_and_image_drift(drift: str):
    recorded = _manifest(_tar_bytes({}))["source"]
    current = _compatibility_facts(recorded)
    if drift == "migration":
        current["migrations"][0]["sha256"] = "f" * 64
    elif drift == "image":
        current["containers"]["server"]["image_id"] = "sha256:" + "f" * 64
    else:
        current["server_image_migrations"][0]["sha256"] = "f" * 64
    with pytest.raises(backup.BackupError, match="migration|image"):
        backup.validate_source_compatibility(recorded, current)


def test_compose_isolation_rejects_fixed_ports_and_wrong_live_project():
    config = _valid_compose()
    backup.validate_compose_isolation(
        config, config["name"], expected_images={"db": "sha256:db", "server": "sha256:server"}
    )
    with pytest.raises(backup.BackupError, match="host ports"):
        bad = _valid_compose()
        bad["services"]["db"]["ports"] = [{"target": 5432, "published": "5432"}]
        backup.validate_compose_isolation(bad, bad["name"])
    with pytest.raises(backup.BackupError, match="mounts"):
        bad = _valid_compose()
        bad["services"]["server"]["volumes"].append({"type": "bind", "source": "/tmp", "target": "/extra"})
        backup.validate_compose_isolation(bad, bad["name"])
    with pytest.raises(backup.BackupError, match="isolated"):
        backup.validate_compose_isolation(config, "crowley")


def _target_guard_fixture():
    project = "crowley-drill-0123456789abcdef"
    network_id = "target-network-id"
    database_volume = {
        "name": project + "_crowley-db",
        "mountpoint": "/volumes/db/_data",
        "labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.volume": "crowley-db",
        },
    }
    raw_volume = {
        "name": project + "_crowley-raw",
        "mountpoint": "/volumes/raw/_data",
        "labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.volume": "crowley-raw",
        },
    }
    target = {
        "containers": {
            "db": {
                "id": "target-db-id",
                "image_id": "sha256:db-image",
                "labels": {"com.docker.compose.project": project, "com.docker.compose.service": "db"},
                "ports": {},
                "state": "created",
                "mounts": [
                    {
                        "Type": "volume",
                        "Name": database_volume["name"],
                        "Source": database_volume["mountpoint"],
                        "Destination": "/var/lib/postgresql/data",
                        "RW": True,
                    }
                ],
                "networks": {project + "_default": {"NetworkID": network_id}},
            },
            "server": {
                "id": "target-server-id",
                "image_id": "sha256:server-image",
                "labels": {
                    "com.docker.compose.project": project,
                    "com.docker.compose.service": "server",
                },
                "ports": {},
                "state": "created",
                "mounts": [
                    {
                        "Type": "volume",
                        "Name": raw_volume["name"],
                        "Source": raw_volume["mountpoint"],
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "networks": {project + "_default": {"NetworkID": network_id}},
            },
        },
        "volumes": [deepcopy(database_volume), deepcopy(raw_volume)],
        "networks": [
            {
                "id": network_id,
                "name": project + "_default",
                "labels": {"com.docker.compose.project": project},
            }
        ],
    }
    current_db = {
        "id": "current-db-id",
        "labels": {"com.docker.compose.project": "crowley", "com.docker.compose.service": "db"},
    }
    current_server = {
        "id": "current-server-id",
        "labels": {"com.docker.compose.project": "crowley", "com.docker.compose.service": "server"},
    }
    source = {
        "containers": {"db": current_db, "server": current_server},
        "volumes": {
            "database": {"name": "current-db-volume"},
            "raw_archive": {"name": "current-raw-volume"},
        },
        "networks": [{"id": "current-network-id", "name": "current-network"}],
        "recorded": {
            "container_ids": ["historical-db-id", "historical-server-id"],
            "volume_names": ["historical-db-volume", "historical-raw-volume"],
            "networks": [{"id": "historical-network-id", "name": "historical-network"}],
        },
        "recorded_images": {"db": "sha256:db-image", "server": "sha256:server-image"},
        "target_volume_keys": {"db": "crowley-db", "server": "crowley-raw"},
    }
    live = {
        "containers": {current_db["id"]: current_db, current_server["id"]: current_server},
        "volumes": {"current-db-volume": {}, "current-raw-volume": {}},
        "networks": {
            "current-network-id": {"name": "current-network"},
            "historical-network-id": {"name": "historical-network"},
        },
    }
    return project, target, source, live


def test_target_guard_accepts_changed_current_ids_with_exact_compatibility_facts():
    project, target, source, live = _target_guard_fixture()
    backup._target_guard(target, project, source, live, service_state="created")


@pytest.mark.parametrize(
    ("resource", "identity"),
    [
        ("container", "current-db-id"),
        ("container", "historical-db-id"),
        ("volume", "current-db-volume"),
        ("volume", "historical-db-volume"),
        ("network_id", "current-network-id"),
        ("network_id", "historical-network-id"),
        ("network_name", "historical-network"),
    ],
)
def test_target_guard_rejects_collision_with_current_or_recorded_ids(resource: str, identity: str):
    project, target, source, live = _target_guard_fixture()
    if resource == "container":
        target["containers"]["db"]["id"] = identity
    elif resource == "volume":
        target["containers"]["db"]["mounts"][0]["Name"] = identity
        target["containers"]["db"]["mounts"][0]["Source"] = "/somewhere"
        target["volumes"][0]["name"] = identity
    elif resource == "network_id":
        target["networks"][0]["id"] = identity
        target["containers"]["db"]["networks"][project + "_default"]["NetworkID"] = identity
        target["containers"]["server"]["networks"][project + "_default"]["NetworkID"] = identity
    else:
        target["networks"][0]["name"] = identity
        target["containers"]["db"]["networks"] = {identity: {"NetworkID": "target-network-id"}}
        target["containers"]["server"]["networks"] = {identity: {"NetworkID": "target-network-id"}}
    with pytest.raises(backup.BackupError):
        backup._target_guard(target, project, source, live)


@pytest.mark.parametrize(
    "bad_mount", ["shared", "extra_volume", "extra_bind", "wrong_image", "swapped_roles"]
)
def test_target_guard_requires_recorded_images_and_exact_distinct_mounts(bad_mount: str):
    project, target, source, live = _target_guard_fixture()
    if bad_mount == "shared":
        target["containers"]["server"]["mounts"][0]["Name"] = target["volumes"][0]["name"]
        target["containers"]["server"]["mounts"][0]["Source"] = target["volumes"][0]["mountpoint"]
    elif bad_mount == "extra_volume":
        target["volumes"].append(
            {
                "name": project + "_unexpected",
                "mountpoint": "/volumes/unexpected/_data",
                "labels": {"com.docker.compose.project": project},
            }
        )
    elif bad_mount == "extra_bind":
        target["containers"]["server"]["mounts"].append(
            {"Type": "bind", "Name": "", "Source": "/tmp", "Destination": "/extra", "RW": True}
        )
    elif bad_mount == "swapped_roles":
        target["volumes"][0]["labels"]["com.docker.compose.volume"] = "crowley-raw"
        target["volumes"][1]["labels"]["com.docker.compose.volume"] = "crowley-db"
    else:
        target["containers"]["server"]["image_id"] = "sha256:unrecorded-image"
    with pytest.raises(backup.BackupError):
        backup._target_guard(target, project, source, live)


@pytest.mark.parametrize(
    ("fail_volume_removal", "stuck_container_removal"), [(False, False), (True, False), (False, True)]
)
def test_cleanup_verifies_absence_and_retains_only_confirmed_removals(
    monkeypatch, fail_volume_removal, stuck_container_removal
):
    project = "crowley-drill-0123456789abcdef"
    network_id = "target-network-id"
    network_name = project + "_default"
    db_volume = project + "_db"
    raw_volume = project + "_raw"
    volumes = {
        db_volume: {
            "name": db_volume,
            "mountpoint": "/volumes/db/_data",
            "labels": {"com.docker.compose.project": project},
        },
        raw_volume: {
            "name": raw_volume,
            "mountpoint": "/volumes/raw/_data",
            "labels": {"com.docker.compose.project": project},
        },
    }
    network = {"id": network_id, "name": network_name, "labels": {"com.docker.compose.project": project}}
    containers = {}
    for service, container_id, volume, destination in (
        ("db", "db-container-id", db_volume, "/var/lib/postgresql/data"),
        ("server", "server-container-id", raw_volume, "/data"),
    ):
        containers[container_id] = {
            "id": container_id,
            "image_id": f"sha256:{service}",
            "labels": {"com.docker.compose.project": project, "com.docker.compose.service": service},
            "mounts": [
                {
                    "Type": "volume",
                    "Name": volume,
                    "Source": volumes[volume]["mountpoint"],
                    "Destination": destination,
                    "RW": True,
                }
            ],
            "networks": {network_name: {"NetworkID": network_id}},
            "ports": {},
            "state": "created",
        }
    state = {
        "containers": deepcopy(containers),
        "volumes": deepcopy(volumes),
        "networks": {network_id: network},
    }
    target = {
        "containers": {"db": containers["db-container-id"], "server": containers["server-container-id"]},
        "volumes": list(volumes.values()),
        "networks": [network],
    }
    source = {"containers": {}, "volumes": {}, "networks": [], "none_network_id": "none-network-id"}
    monkeypatch.setattr(backup, "_inspect_container", lambda key: deepcopy(state["containers"][key]))
    monkeypatch.setattr(backup, "_inspect_volume", lambda key: deepcopy(state["volumes"][key]))
    monkeypatch.setattr(backup, "_inspect_network", lambda key: deepcopy(state["networks"][key]))
    monkeypatch.setattr(backup, "_live_inventory", lambda: deepcopy(state))

    def docker(args):
        if args[:2] == ["rm", "-f"]:
            if not stuck_container_removal:
                del state["containers"][args[2]]
        elif args[:2] == ["volume", "rm"]:
            if fail_volume_removal:
                raise backup.BackupError("bounded backup operation failed")
            del state["volumes"][args[2]]
        elif args[:2] == ["network", "rm"]:
            del state["networks"][args[2]]
        else:
            pytest.fail(f"unexpected cleanup command category: {args[:2]}")

    monkeypatch.setattr(backup, "_docker", docker)
    result = backup._remove_owned(
        target, [], project, source, {"containers": {}, "volumes": {}, "networks": {}}
    )
    if fail_volume_removal:
        assert result == {
            "verified": False,
            "removed": ["container:db-container-id", "container:server-container-id"],
        }
        assert not state["containers"]
        assert set(state["volumes"]) == {db_volume, raw_volume}
    elif stuck_container_removal:
        assert result == {"verified": False, "removed": []}
        assert set(state["containers"]) == {"db-container-id", "server-container-id"}
    else:
        assert result["verified"] is True
        assert set(result["removed"]) == {
            "container:db-container-id",
            "container:server-container-id",
            f"volume:{db_volume}",
            f"volume:{raw_volume}",
            f"network:{network_id}",
        }
        assert state == {"containers": {}, "volumes": {}, "networks": {}}


def test_target_guard_refuses_source_container_and_nonproject_volume():
    project = "crowley-drill-0123456789abcdef"
    source_container = {
        "id": "source",
        "labels": {"com.docker.compose.project": "crowley", "com.docker.compose.service": "db"},
        "image_id": "img",
        "ports": {},
        "mounts": [
            {"Destination": "/var/lib/postgresql/data", "Type": "volume", "Name": "srcdb", "RW": True}
        ],
        "networks": {},
    }
    source = {
        "containers": {"db": source_container, "server": {"id": "src2"}},
        "volumes": {"db": {"name": "srcdb"}},
        "networks": [],
    }
    live = {"containers": {"source": source_container}, "volumes": {"srcdb": {}}, "networks": {}}
    target = {
        "containers": {
            "db": source_container,
            "server": {
                "id": "new",
                "labels": {"com.docker.compose.project": project, "com.docker.compose.service": "server"},
                "ports": {},
                "mounts": [{"Destination": "/data", "Type": "volume", "Name": "rawnew", "RW": True}],
                "networks": {},
            },
        },
        "volumes": [{"name": "rawnew", "labels": {"com.docker.compose.project": project}}],
        "networks": [{"id": "newnet", "labels": {"com.docker.compose.project": project}}],
    }
    with pytest.raises(backup.BackupError):
        backup._target_guard(target, project, source, live)


def test_partial_compose_create_failure_leaves_cleanup_blocked(monkeypatch):
    project, _target, source, live = _target_guard_fixture()
    creation = {"attempted": False, "target": {"containers": {}, "volumes": [], "networks": []}}
    created_subset = ["one project-labelled db container"]

    def create_then_fail(*_args, **_kwargs):
        assert created_subset
        raise backup.BackupError("bounded backup operation failed")

    monkeypatch.setattr(backup, "_target_compose", create_then_fail)
    monkeypatch.setattr(
        backup,
        "_remove_owned",
        lambda *_args, **_kwargs: pytest.fail("uncaptured partial resources must not be removed"),
    )
    with pytest.raises(backup.BackupError):
        backup._create_and_capture_target(Path("."), project, Path("override"), Path("credentials"), creation)
    result = backup._cleanup_after_creation(creation, [], project, source, live)
    assert creation["attempted"] is True
    assert result == {"verified": False, "removed": []}


def test_successful_create_with_discovery_failure_leaves_cleanup_blocked(monkeypatch):
    project, _target, source, live = _target_guard_fixture()
    creation = {"attempted": False, "target": {"containers": {}, "volumes": [], "networks": []}}
    monkeypatch.setattr(backup, "_target_compose", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        backup,
        "_target_ids",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(backup.BackupError("discovery failed")),
    )
    monkeypatch.setattr(
        backup,
        "_remove_owned",
        lambda *_args, **_kwargs: pytest.fail("undiscovered resources must not be removed"),
    )
    with pytest.raises(backup.BackupError, match="discovery"):
        backup._create_and_capture_target(Path("."), project, Path("override"), Path("credentials"), creation)
    result = backup._cleanup_after_creation(creation, [], project, source, live)
    assert creation["attempted"] is True
    assert result == {"verified": False, "removed": []}


def test_project_absence_check_tolerates_unlabeled_external_docker_resources():
    backup._assert_project_absent(
        {
            "containers": {"container": {"labels": None}},
            "volumes": {"volume": {"labels": None}},
            "networks": {"network": {"labels": None}},
        },
        "crowley-drill-0123456789abcdef",
    )


def test_live_source_guard_compares_full_captured_resource_identities():
    container = {
        "id": "source-container",
        "image_id": "source-image",
        "labels": {"com.docker.compose.project": "crowley"},
        "mounts": [],
        "networks": {},
        "ports": {},
    }
    volume = {"name": "source-volume", "mountpoint": "/source", "labels": {}}
    network = {"id": "source-network", "name": "crowley_default", "labels": {}}
    live = {
        "containers": {container["id"]: deepcopy(container)},
        "volumes": {volume["name"]: deepcopy(volume)},
        "networks": {network["id"]: deepcopy(network)},
    }
    source = {
        "containers": {"db": deepcopy(container)},
        "volumes": {"database": deepcopy(volume)},
        "networks": [deepcopy(network)],
    }
    backup._assert_source_current(live, source)
    live["volumes"][volume["name"]]["mountpoint"] = "/changed"
    with pytest.raises(backup.BackupError, match="source volume identity changed"):
        backup._assert_source_current(live, source)


def test_container_identity_allows_endpoint_changes_but_rejects_network_id_drift():
    created = {
        "id": "disposable-container",
        "image_id": "sha256:disposable-image",
        "labels": {"com.docker.compose.project": "crowley-drill-test"},
        "mounts": [],
        "ports": {},
        "state": "created",
        "networks": {
            "crowley-drill-test_default": {
                "NetworkID": "",
                "EndpointID": "",
                "IPAddress": "",
                "Gateway": "",
            }
        },
    }
    running = deepcopy(created)
    running["state"] = "running"
    running["networks"]["crowley-drill-test_default"].update(
        NetworkID="network-id", EndpointID="endpoint-id", IPAddress="172.30.0.2", Gateway="172.30.0.1"
    )
    assert backup._same_container_identity(created, running)
    expected_network = {"crowley-drill-test_default": "network-id"}
    assert backup._target_network_attachment_matches(created, expected_network)
    assert backup._target_network_attachment_matches(running, expected_network)
    wrong_network = deepcopy(running)
    wrong_network["networks"]["crowley-drill-test_default"]["NetworkID"] = "other-network-id"
    assert not backup._target_network_attachment_matches(wrong_network, expected_network)


def test_helper_cleanup_requires_exact_builtin_none_network_and_no_endpoint():
    created = {
        "state": "created",
        "networks": {"none": {"NetworkID": "", "EndpointID": "", "IPAddress": ""}},
    }
    assert backup._helper_uses_only_none_network(created, "none-network-id")
    exited = {
        "state": "exited",
        "networks": {"none": {"NetworkID": "none-network-id", "EndpointID": "", "IPAddress": ""}},
    }
    assert backup._helper_uses_only_none_network(exited, "none-network-id")
    wrong_id = deepcopy(exited)
    wrong_id["networks"]["none"]["NetworkID"] = "other-network-id"
    assert not backup._helper_uses_only_none_network(wrong_id, "none-network-id")
    connected = deepcopy(exited)
    connected["networks"]["none"]["IPAddress"] = "172.20.0.2"
    assert not backup._helper_uses_only_none_network(connected, "none-network-id")
    extra_network = deepcopy(exited)
    extra_network["networks"]["crowley"] = {"NetworkID": "project-network-id"}
    assert not backup._helper_uses_only_none_network(extra_network, "none-network-id")


@pytest.mark.parametrize(
    "stage",
    [
        "identity_before_database",
        "database_readiness",
        "start_disposable_database",
        "create_database",
        "identity_after_database",
        "create_extension",
    ],
)
def test_safe_failure_stage_only_keeps_fixed_private_labels(stage: str):
    assert backup._safe_failure_stage(backup.BackupError(stage)) == stage
    assert backup._safe_failure_stage(backup.BackupError("SQL ERROR with details")) is None
    assert backup._safe_failure_stage(RuntimeError(stage)) is None
    assert backup._safe_failure_reason(
        backup.BackupError("disposable container network attachment changed")
    ) == ("network_attachment_changed")
    assert (
        backup._safe_failure_reason(backup.BackupError("SQL ERROR with details")) == "safe_operation_failed"
    )


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, "accepting_connections"),
        (1, "rejecting_connections"),
        (2, "no_response"),
        (3, "invalid_probe_parameters"),
        (255, "probe_failed"),
    ],
)
def test_readiness_status_is_categorical(returncode: int, expected: str):
    assert backup._readiness_status(returncode) == expected


def test_disposable_setup_metadata_records_only_fixed_read_only_flags(monkeypatch):
    calls = []
    values = iter(
        [
            {
                "crowley_database_exists": True,
                "timescaledb_available": True,
                "role_superuser": True,
                "role_createdb": True,
                "timescaledb_preloaded": True,
                "server_version": "16.6",
            },
            {"timescaledb_enabled": False},
        ]
    )

    def fake_json_sql(container_id, sql, db):
        calls.append((container_id, sql, db))
        return next(values)

    monkeypatch.setattr(backup, "_json_sql", fake_json_sql)
    result = backup._disposable_setup_metadata("disposable-db-id")
    assert result == {
        "postgres": {
            "crowley_database_exists": True,
            "timescaledb_available": True,
            "role_superuser": True,
            "role_createdb": True,
            "timescaledb_preloaded": True,
            "server_version": "16.6",
        },
        "crowley_extension_enabled": False,
    }
    assert len(calls) == 2
    assert calls[0][0] == calls[1][0] == "disposable-db-id"
    assert calls[0][2] == "postgres" and calls[1][2] == "crowley"
    assert all("pg_database" in call[1] or "pg_extension" in call[1] for call in calls)


def test_disposable_setup_metadata_rejects_unexpected_fields(monkeypatch):
    monkeypatch.setattr(backup, "_json_sql", lambda *_args: {"secret": "do not retain"})
    with pytest.raises(backup.BackupError, match="metadata is incomplete"):
        backup._disposable_setup_metadata("disposable-db-id")


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        (b"ERROR: permission denied for schema public", "insufficient_privilege"),
        (b"ERROR: relation already exists", "object_conflict"),
        (b"ERROR: relation does not exist", "referenced_object_missing"),
        (b"pg_restore: error: unsupported version (1.16)", "unsupported_dump_feature"),
        (b"pg_restore: error: could not execute query: ERROR: other", "restore_sql_error"),
        (b"pg_restore: error: input file is too short", "restore_client_error"),
        (b"", "unclassified_restore_error"),
    ],
)
def test_pg_restore_error_category_never_returns_command_text(stderr: bytes, expected: str):
    result = backup._pg_restore_error_category(stderr)
    assert result == expected
    assert result not in stderr.decode("utf-8", errors="ignore")


def test_pg_restore_stream_classifies_broken_pipe_without_retaining_stderr(monkeypatch):
    class ClosedInput:
        def write(self, _block):
            raise BrokenPipeError

        def close(self):
            raise BrokenPipeError

    class FailedProcess:
        stdin = ClosedInput()

        def __init__(self, stderr):
            stderr.write(b"pg_restore: error: input file is too short")

        def wait(self):
            return 1

    monkeypatch.setattr(backup.subprocess, "Popen", lambda *_args, **kwargs: FailedProcess(kwargs["stderr"]))
    code, category = backup._stream_pg_restore(["pg_restore"], io.BytesIO(b"archive bytes"))
    assert (code, category) == (1, "restore_client_error")


def test_pg_restore_uses_stdin_without_a_filename_sentinel_or_parallel_jobs():
    args = backup._pg_restore_args("disposable-db-id")
    assert args[:4] == ["docker", "exec", "-i", "disposable-db-id"]
    assert args[4:] == [
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "-Fc",
        "-U",
        "crowley",
        "-d",
        "crowley",
    ]


def test_disposable_restore_metadata_is_limited_to_target_catalog_flags(monkeypatch):
    expected = {
        "crowley_database_exists": True,
        "timescaledb_enabled": True,
        "public_schema_exists": True,
        "schema_migrations_exists": False,
        "public_relation_count": 4,
    }
    monkeypatch.setattr(backup, "_json_sql", lambda *_args: expected)
    assert backup._disposable_restore_metadata("disposable-db-id") == expected

    monkeypatch.setattr(backup, "_json_sql", lambda *_args: {**expected, "statement": "secret"})
    with pytest.raises(backup.BackupError, match="metadata is incomplete"):
        backup._disposable_restore_metadata("disposable-db-id")


def _verified_receipt():
    started = datetime.now(UTC).replace(microsecond=0)
    manifest = _manifest(_tar_bytes({}))
    receipt = backup._receipt_template(manifest, "a" * 64, started)
    project = "crowley-drill-0123456789abcdef"
    net_id = "target-network-id"
    net_name = project + "_default"
    db_volume = project + "_crowley-db"
    raw_volume = project + "_crowley-raw"
    db_mountpoint = "/var/lib/docker/volumes/db/_data"
    raw_mountpoint = "/var/lib/docker/volumes/raw/_data"
    database_image = "sha256:database-image"
    server_image = "sha256:server-image"
    target = receipt["target"]
    target["project"] = project
    target["services"] = {
        "db": {
            "id": "database-container-id",
            "labels": {"com.docker.compose.project": project, "com.docker.compose.service": "db"},
            "image_id": database_image,
            "mounts": [
                {
                    "Type": "volume",
                    "Name": db_volume,
                    "Source": db_mountpoint,
                    "Destination": "/var/lib/postgresql/data",
                    "RW": True,
                }
            ],
            "networks": {net_name: {"NetworkID": net_id}},
        },
        "server": {
            "id": "server-container-id",
            "labels": {"com.docker.compose.project": project, "com.docker.compose.service": "server"},
            "image_id": server_image,
            "mounts": [
                {
                    "Type": "volume",
                    "Name": raw_volume,
                    "Source": raw_mountpoint,
                    "Destination": "/data",
                    "RW": True,
                }
            ],
            "networks": {net_name: {"NetworkID": net_id}},
        },
    }
    target["volumes"] = [
        {
            "name": db_volume,
            "mountpoint": db_mountpoint,
            "labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.volume": "crowley-db",
            },
        },
        {
            "name": raw_volume,
            "mountpoint": raw_mountpoint,
            "labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.volume": "crowley-raw",
            },
        },
    ]
    target["networks"] = [{"id": net_id, "name": net_name, "labels": {"com.docker.compose.project": project}}]
    target["helpers"] = [
        {
            "id": "raw-restore-helper-id",
            "service": "raw-restore-helper",
            "labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "raw-restore-helper",
            },
            "image_id": server_image,
            "mounts": [
                {
                    "Type": "volume",
                    "Name": raw_volume,
                    "Source": raw_mountpoint,
                    "Destination": "/data",
                    "RW": True,
                }
            ],
            "networks": {"none": {"NetworkID": "builtin-none-network-id"}},
        },
        {
            "id": "raw-verify-helper-id",
            "service": "raw-verify-helper",
            "labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "raw-verify-helper",
            },
            "image_id": server_image,
            "mounts": [
                {
                    "Type": "volume",
                    "Name": raw_volume,
                    "Source": raw_mountpoint,
                    "Destination": "/data",
                    "RW": False,
                }
            ],
            "networks": {"none": {"NetworkID": "builtin-none-network-id"}},
        },
        {
            "id": "reference-migration-helper-id",
            "service": "reference-migration-helper",
            "labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "reference-migration-helper",
            },
            "image_id": server_image,
            "mounts": [],
            "networks": {net_name: {"NetworkID": net_id}},
        },
    ]
    receipt["result"] = "restore_verified"
    receipt["target_versions"] = {
        "postgres_server_version": "16.6",
        "timescaledb_extension_version": "2.17.2",
        "match": True,
    }
    receipt["counts"] = {
        "provenance": {
            "source": "restored_dump",
            "reference_table_keys": [
                "houses",
                "items",
                "market_plans",
                "market_sweeps",
                "scans",
                "schema_migrations",
            ],
            "tables": {
                "scans": 0,
                "houses": 1,
                "items": 0,
                "market_sweeps": 1,
                "market_plans": 1,
                "schema_migrations": 2,
            },
        },
        "groups": {
            "scans": [],
            "market_sweeps": [
                {
                    "client_build": "19000",
                    "interface": 120000,
                    "house_key": "house-a",
                    "plan_hash": "b" * 64,
                    "rows": 1,
                }
            ],
        },
        "houses": {"count": 1, "keys": ["house-a"]},
        "market_plans": {"count": 1, "hashes": ["b" * 64]},
        "migrations": {
            "expected": ["001_init.sql", "002_market_observations.sql"],
            "applied": ["001_init.sql", "002_market_observations.sql"],
            "match": True,
        },
    }
    receipt["migrations"] = deepcopy(receipt["counts"]["migrations"])
    digest = "c" * 64
    receipt["references"] = [
        {
            "source_table": "market_sweeps",
            "row_id": 1,
            "inventory_path": f"raw/format2/{digest[:2]}/{digest}.json.gz",
            "member_sha256": "d" * 64,
            "validation": "valid_format2",
            "logical": {
                "logical_digest": digest,
                "sweep_uid": "sweep-1",
                "house_key": "house-a",
                "plan_hash": "b" * 64,
            },
        }
    ]
    receipt["api"] = {"health_probe": True, "authenticated_read": True}
    for operation in receipt["operations"]:
        operation["state"] = "passed"
    receipt["cleanup"] = {
        "verified": True,
        "removed": sorted(
            [
                "container:database-container-id",
                "container:server-container-id",
                "container:raw-restore-helper-id",
                "container:raw-verify-helper-id",
                "container:reference-migration-helper-id",
                f"volume:{db_volume}",
                f"volume:{raw_volume}",
                f"network:{net_id}",
            ]
        ),
    }
    return receipt


def test_complete_restore_verified_receipt_is_accepted():
    receipt = _verified_receipt()
    assert backup.validate_receipt(receipt)["result"] == "restore_verified"


def test_verified_receipt_rejects_missing_non_core_reference_table_count() -> None:
    receipt = _verified_receipt()
    del receipt["counts"]["provenance"]["tables"]["items"]
    with pytest.raises(backup.BackupError, match="reference table-key list"):
        backup.validate_receipt(receipt)


def test_partial_blocked_receipt_is_accepted_but_must_remain_partial():
    receipt = backup._receipt_template(_manifest(_tar_bytes({})), "a" * 64, datetime.now(UTC))
    assert backup.validate_receipt(receipt)["result"] == "blocked"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda receipt: receipt["operations"][3].update(state="not_run"),
        lambda receipt: receipt["target_versions"].update(match=False),
        lambda receipt: receipt["migrations"].update(match=False),
        lambda receipt: receipt["migrations"]["applied"].append("003_unexpected.sql"),
        lambda receipt: receipt.update(counts={}),
        lambda receipt: receipt["counts"]["provenance"]["tables"].update(scans="1"),
        lambda receipt: receipt["api"].update(authenticated_read=False),
        lambda receipt: receipt["cleanup"].update(verified=False),
        lambda receipt: receipt["target"].update(services={}),
        lambda receipt: receipt["references"][0]["logical"].update(plan_hash=42),
        lambda receipt: receipt["target"]["services"]["db"].update(labels=[]),
        lambda receipt: receipt["target"]["services"]["server"]["mounts"][0].update(Destination=42),
        lambda receipt: receipt["target"]["helpers"][0]["networks"]["none"].update(NetworkID=42),
        lambda receipt: receipt["counts"]["groups"]["market_sweeps"][0].update(interface=True),
        lambda receipt: receipt["counts"]["migrations"]["expected"].__setitem__(0, {"name": "bad"}),
    ],
)
def test_restore_verified_receipt_rejects_failed_invariants_and_nested_types(mutation):
    receipt = _verified_receipt()
    mutation(receipt)
    with pytest.raises(backup.BackupError):
        backup.validate_receipt(receipt)


def test_receipt_rejects_duplicate_json_keys_and_unknown_fields():
    receipt = _verified_receipt()
    receipt["mystery"] = True
    with pytest.raises(backup.BackupError, match="fields"):
        backup.validate_receipt(receipt)
    duplicate = b'{"schema":"x","schema":"y"}'
    with pytest.raises(backup.BackupError, match="duplicate"):
        backup.validate_receipt(duplicate)
