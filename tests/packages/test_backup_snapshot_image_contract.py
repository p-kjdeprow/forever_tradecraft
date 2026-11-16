from __future__ import annotations

import os
import tarfile
from pathlib import Path
from typing import Any

import pytest
from crowley_market import backup


def _configure_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
    image_migrations: list[dict[str, str]] | None,
) -> tuple[list[str], list[str]]:
    dump_calls: list[str] = []
    inspected: list[str] = []
    monkeypatch.setattr(backup, "_package_identity", lambda: (package_name, "0.2.0"))
    monkeypatch.setattr(backup, "secrets_path", lambda: Path("secrets.env"))
    monkeypatch.setattr(
        backup,
        "_source_resources",
        lambda _compose, _secrets: (
            {"name": "crowley"},
            {
                "db": {"id": "db-container", "image_id": "sha256:" + "a" * 64},
                "server": {"id": "server-container", "image_id": "sha256:" + "b" * 64},
            },
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

    def inspect_image(image_id: str, *, package_namespace: str) -> list[dict[str, str]]:
        inspected.extend((image_id, package_namespace))
        if image_migrations is None:
            raise backup.BackupError("bounded backup operation failed")
        return image_migrations

    def run_dump(args: list[str], **kwargs: Any) -> Any:
        dump_calls.append(" ".join(args))
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write(b"fixture database dump")
        return type("Completed", (), {"returncode": 0})()

    def capture_raw(_volume: str, _image: str, output: Path) -> None:
        with output.open("xb") as stream:
            with tarfile.open(fileobj=stream, mode="w:"):
                pass
        os.chmod(output, 0o600)

    monkeypatch.setattr(backup, "_target_image_migrations", inspect_image)
    monkeypatch.setattr(backup, "_run", run_dump)
    monkeypatch.setattr(backup, "_capture_raw_volume", capture_raw)
    return dump_calls, inspected


@pytest.mark.parametrize(
    ("package_name", "image_migrations"),
    (
        ("crowley-market", []),
        ("crowley-market", None),
    ),
)
def test_snapshot_rejects_an_image_without_matching_package_migrations_before_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
    image_migrations: list[dict[str, str]] | None,
) -> None:
    dump_calls, inspected = _configure_snapshot(monkeypatch, package_name, image_migrations)
    backup_root = tmp_path / "backups"

    with pytest.raises(backup.BackupError):
        backup.snapshot(backup_root)

    assert inspected == ["sha256:" + "b" * 64, "crowley_market"]
    assert dump_calls == []
    assert list(backup_root.iterdir()) == []
    assert not list(backup_root.rglob("COMPLETE"))


@pytest.mark.parametrize(
    ("package_name", "expected_namespace"),
    (("crowley", "crowley"), ("crowley-market", "crowley_market")),
)
def test_snapshot_accepts_an_image_matching_its_local_package_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    package_name: str,
    expected_namespace: str,
) -> None:
    migrations = backup._packaged_migrations()
    dump_calls, inspected = _configure_snapshot(monkeypatch, package_name, migrations)

    snapshot = backup.snapshot(tmp_path / "backups")
    manifest, _ = backup.validate_snapshot(snapshot)

    assert inspected == ["sha256:" + "b" * 64, expected_namespace]
    assert len(dump_calls) == 1 and "pg_dump" in dump_calls[0]
    assert (snapshot / "COMPLETE").is_file()
    assert manifest["source"]["package_name"] == package_name
