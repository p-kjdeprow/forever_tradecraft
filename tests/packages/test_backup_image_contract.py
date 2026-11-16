from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crowley_market import backup


@pytest.mark.parametrize(
    ("schema_version", "package_name", "expected"),
    (
        (backup.HISTORICAL_SCHEMA_VERSION, None, "crowley"),
        (backup.HISTORICAL_SCHEMA_VERSION, "crowley", "crowley"),
        (backup.CURRENT_SCHEMA_VERSION, "crowley", "crowley"),
        (backup.CURRENT_SCHEMA_VERSION, "crowley-market", "crowley_market"),
    ),
)
def test_manifest_selects_the_server_namespace_in_its_recorded_image(
    schema_version: int, package_name: str | None, expected: str
) -> None:
    assert backup._migration_namespace_for_manifest(schema_version, package_name) == expected


@pytest.mark.parametrize(
    ("schema_version", "package_name"),
    (
        (backup.HISTORICAL_SCHEMA_VERSION, "unknown"),
        (backup.CURRENT_SCHEMA_VERSION, None),
        (backup.CURRENT_SCHEMA_VERSION, "unknown"),
        (3, "crowley-market"),
        (True, "crowley"),
    ),
)
def test_manifest_rejects_unknown_migration_package_identity(
    schema_version: int, package_name: str | None
) -> None:
    with pytest.raises(backup.BackupError, match="snapshot package identity|manifest schema"):
        backup._migration_namespace_for_manifest(schema_version, package_name)


@pytest.mark.parametrize(
    ("schema_version", "package_name", "expected_namespace"),
    (
        (backup.HISTORICAL_SCHEMA_VERSION, None, "crowley"),
        (backup.CURRENT_SCHEMA_VERSION, "crowley", "crowley"),
        (backup.CURRENT_SCHEMA_VERSION, "crowley-market", "crowley_market"),
    ),
)
def test_image_and_reference_migration_scripts_use_the_selected_namespace(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int,
    package_name: str | None,
    expected_namespace: str,
) -> None:
    namespace = backup._migration_namespace_for_manifest(schema_version, package_name)
    reference_script = backup._reference_migration_script(namespace)
    ast.parse(reference_script)
    assert f"from {expected_namespace}.server.db import migrate" in reference_script

    calls: list[list[str]] = []

    def fake_docker(args: list[str], **_: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(stdout="[]\n")

    monkeypatch.setattr(backup, "_docker", fake_docker)
    assert backup._target_image_migrations("image-id", package_namespace=namespace) == []
    script = calls[0][calls[0].index("-c") + 1]
    ast.parse(script)
    assert f"resources.files('{expected_namespace}.server.migrations')" in script
    assert "src/crowley/server/migrations/" in script


@pytest.mark.parametrize("namespace", ("crowley", "crowley_market"))
def test_reference_inventory_passes_selected_namespace_to_helper(
    monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    received: list[str] = []
    registered: list[dict[str, Any]] = []

    def fake_make_helper(
        project: str,
        database_name: str,
        network_name: str,
        image_id: str,
        credentials: Path,
        *,
        package_namespace: str,
    ) -> tuple[str, dict[str, Any]]:
        assert (project, network_name, image_id, credentials) == (
            "project",
            "network",
            "image",
            Path("credentials.env"),
        )
        assert database_name.startswith("crowley_reference_")
        received.append(package_namespace)
        return "helper-id", {"id": "helper-id"}

    def fake_build_reference_inventory(
        database_name: str,
        create_database: Any,
        prepare_migration: Any,
        migrate_database: Any,
        inventory_database: Any,
        drop_database: Any,
    ) -> str:
        del create_database, migrate_database, inventory_database, drop_database
        prepare_migration(database_name)
        return "inventory"

    monkeypatch.setattr(backup, "_make_reference_migration_helper", fake_make_helper)
    monkeypatch.setattr(backup, "build_reference_inventory", fake_build_reference_inventory)
    result = backup._reference_database_inventory(
        "db",
        "image",
        "network",
        Path("credentials.env"),
        "project",
        registered.append,
        [],
        package_namespace=namespace,
    )

    assert result == "inventory"
    assert received == [namespace]
    assert registered == [{"id": "helper-id"}]


@pytest.mark.parametrize("namespace", ("crowley", "crowley_market"))
def test_reference_migration_helper_executes_script_for_recorded_image(
    monkeypatch: pytest.MonkeyPatch, namespace: str
) -> None:
    calls: list[list[str]] = []

    def fake_docker(args: list[str], **_: Any) -> SimpleNamespace:
        calls.append(args)
        return SimpleNamespace(stdout="helper-id\n")

    monkeypatch.setattr(backup, "_docker", fake_docker)
    monkeypatch.setattr(
        backup,
        "_inspect_container",
        lambda _: {
            "labels": {
                "com.docker.compose.project": "project",
                "com.docker.compose.service": "reference-migration-helper",
            },
            "state": "created",
            "mounts": [],
            "ports": {},
            "networks": {"network": {}},
        },
    )

    helper_id, _ = backup._make_reference_migration_helper(
        "project",
        "reference",
        "network",
        "image-id",
        Path("credentials.env"),
        package_namespace=namespace,
    )

    script = calls[0][calls[0].index("-c") + 1]
    ast.parse(script)
    assert helper_id == "helper-id"
    assert f"from {namespace}.server.db import migrate" in script
