"""Private, offline snapshot and isolated restore checks for Crowley market data.

The CLI deliberately keeps Docker's resolved configuration, command errors,
database metadata and receipts out of stdout.  It is a one-shot recovery tool,
not a scheduler or a general Docker cleanup utility.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

SCHEMA = "crowley.market-backup-manifest"
RECEIPT_SCHEMA = "crowley.market-backup-drill-receipt"
SUMMARY_SCHEMA = "crowley.market-backup-drill-summary"
MARKER_PREFIX = "crowley.market-backup-complete-v1 "
DEFAULT_ROOT = Path.home() / ".local/state/crowley/market-backups"
COMPOSE = Path("deploy/docker-compose.yml")
ENV_FILE = Path("deploy/.env")
MIGRATIONS = Path("src/crowley/server/migrations")
NEW_BACKUP_ID_PREFIX = "crowley-"
HISTORICAL_BACKUP_ID_PREFIX = "beta-"
BACKUP_ID_RE = re.compile(
    rf"^(?:{re.escape(NEW_BACKUP_ID_PREFIX)}|{re.escape(HISTORICAL_BACKUP_ID_PREFIX)})"
    r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$"
)
TIMESCALE_INTERNAL_SCHEMAS = frozenset(
    {
        "_timescaledb_cache",
        "_timescaledb_catalog",
        "_timescaledb_config",
        "_timescaledb_functions",
        "_timescaledb_internal",
        "timescaledb_experimental",
        "timescaledb_information",
    }
)
REFERENCE_MIGRATION_SCRIPT = (
    "import os, psycopg\n"
    "from crowley.server.db import migrate\n"
    "database_url = ('postgresql://crowley:' + os.environ['CROWLEY_DB_PASSWORD']\n"
    "                + '@db:5432/' + os.environ['CROWLEY_REFERENCE_DATABASE'])\n"
    "with psycopg.connect(database_url, autocommit=True) as conn:\n"
    "    migrate(conn)\n"
)
OPERATIONS = (
    "validate_snapshot",
    "create_disposable_resources",
    "start_disposable_database",
    "create_database_extension",
    "timescaledb_pre_restore",
    "pg_restore_single_job_no_owner",
    "timescaledb_post_restore",
    "analyze",
    "count_restored_tables",
    "extract_raw_archive",
    "validate_raw_references",
    "start_server",
    "health_probe",
    "authenticated_read",
    "cleanup_owned_resources",
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True, order=True)
class TableInventory:
    schema: str
    name: str


@dataclass(frozen=True, slots=True, order=True)
class ColumnInventory:
    schema: str
    table: str
    name: str
    data_type: str
    nullable: bool
    default_expression: str | None


@dataclass(frozen=True, slots=True, order=True)
class ConstraintInventory:
    schema: str
    table: str
    name: str
    constraint_type: str
    definition: str


@dataclass(frozen=True, slots=True, order=True)
class IndexInventory:
    schema: str
    table: str
    name: str
    definition: str


@dataclass(frozen=True, slots=True, order=True)
class ViewInventory:
    schema: str
    name: str
    definition: str


@dataclass(frozen=True, slots=True)
class SchemaInventory:
    tables: tuple[TableInventory, ...]
    columns: tuple[ColumnInventory, ...]
    constraints: tuple[ConstraintInventory, ...]
    indexes: tuple[IndexInventory, ...]
    views: tuple[ViewInventory, ...]


class BackupError(RuntimeError):
    """Safe-to-report bounded operation error (does not contain subprocess text)."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("duplicate JSON key")
        result[key] = value
    return result


def strict_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("invalid UTF-8 JSON") from exc


def _exact_keys(value: Any, keys: set[str], where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BackupError(f"invalid {where} fields")
    return value


def _string(value: Any, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise BackupError(f"invalid {where}")
    return value


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise BackupError(f"invalid {where} SHA-256")
    return value


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BackupError(f"invalid {where}")
    return value


def _path(value: Any, where: str) -> str:
    text = _string(value, where)
    if "\x00" in text or "\\" in text:
        raise BackupError(f"unsafe {where}")
    pure = PurePosixPath(text)
    if pure.is_absolute() or text.startswith("/") or any(part in ("", ".", "..") for part in text.split("/")):
        raise BackupError(f"unsafe {where}")
    if pure.as_posix() != text:
        raise BackupError(f"noncanonical {where}")
    return text


def validate_manifest(value: Any) -> dict[str, Any]:
    top = _exact_keys(
        value,
        {"schema", "schema_version", "backup_id", "created_at_utc", "source", "database_dump", "raw_archive"},
        "manifest",
    )
    if top["schema"] != SCHEMA or type(top["schema_version"]) is not int or top["schema_version"] != 1:
        raise BackupError("unsupported manifest schema")
    if not isinstance(top["backup_id"], str) or not BACKUP_ID_RE.fullmatch(top["backup_id"]):
        raise BackupError("invalid backup id")
    created = _string(top["created_at_utc"], "created_at_utc")
    try:
        parsed = dt.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("invalid created_at_utc") from exc
    if not created.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
        raise BackupError("created_at_utc must be UTC Z")
    source = _exact_keys(
        top["source"],
        {
            "compose_file_sha256",
            "compose_project",
            "services",
            "containers",
            "volumes",
            "networks",
            "source_commit",
            "dirty_tree_sha256",
            "migrations",
            "postgres_server_version",
            "timescaledb_extension_version",
        },
        "source",
    )
    _sha(source["compose_file_sha256"], "compose file")
    _string(source["compose_project"], "compose project")
    services = _exact_keys(source["services"], {"database", "server"}, "services")
    _string(services["database"], "database service")
    _string(services["server"], "server service")
    containers = _exact_keys(source["containers"], {"database", "server"}, "containers")
    for name in ("database", "server"):
        item = _exact_keys(containers[name], {"id", "image_id", "repo_digests"}, f"{name} container")
        for key in ("id", "image_id"):
            if not ID_RE.fullmatch(_string(item[key], f"{name}.{key}")):
                raise BackupError(f"invalid {name} {key}")
        if not isinstance(item["repo_digests"], list) or any(
            not isinstance(d, str) or not d for d in item["repo_digests"]
        ):
            raise BackupError(f"invalid {name} repo digests")
        if item["repo_digests"] != sorted(set(item["repo_digests"])):
            raise BackupError(f"unsorted {name} repo digests")
    volumes = _exact_keys(source["volumes"], {"database", "raw_archive"}, "volumes")
    for key in ("database", "raw_archive"):
        volume = _exact_keys(volumes[key], {"name", "mountpoint"}, f"{key} volume")
        _string(volume["name"], f"{key} name")
        _string(volume["mountpoint"], f"{key} mountpoint")
    if not isinstance(source["networks"], list):
        raise BackupError("invalid networks")
    network_names: list[str] = []
    for network in source["networks"]:
        net = _exact_keys(network, {"name", "id"}, "network")
        network_names.append(_string(net["name"], "network name"))
        _string(net["id"], "network id")
    if network_names != sorted(set(network_names)):
        raise BackupError("networks must be unique and sorted")
    _string(source["source_commit"], "source commit")
    _sha(source["dirty_tree_sha256"], "dirty tree")
    if not isinstance(source["migrations"], list) or not source["migrations"]:
        raise BackupError("invalid migrations")
    migration_paths: list[str] = []
    for migration in source["migrations"]:
        item = _exact_keys(migration, {"path", "sha256"}, "migration")
        path = _path(item["path"], "migration path")
        if not path.startswith("src/crowley/server/migrations/") or not path.endswith(".sql"):
            raise BackupError("migration path outside repository migration tree")
        migration_paths.append(path)
        _sha(item["sha256"], "migration")
    if migration_paths != sorted(set(migration_paths), key=lambda p: p.encode("utf-8")):
        raise BackupError("migrations must be unique and sorted")
    _string(source["postgres_server_version"], "postgres version")
    _string(source["timescaledb_extension_version"], "timescaledb version")
    dump = _exact_keys(top["database_dump"], {"path", "format", "size_bytes", "sha256"}, "database_dump")
    if dump["path"] != "database.dump" or dump["format"] != "postgres-custom":
        raise BackupError("invalid database dump descriptor")
    _integer(dump["size_bytes"], "dump size")
    _sha(dump["sha256"], "dump")
    raw = _exact_keys(
        top["raw_archive"], {"path", "format", "size_bytes", "sha256", "members"}, "raw_archive"
    )
    if raw["path"] != "raw.tar" or raw["format"] != "tar":
        raise BackupError("invalid raw archive descriptor")
    _integer(raw["size_bytes"], "raw archive size")
    _sha(raw["sha256"], "raw archive")
    if not isinstance(raw["members"], list):
        raise BackupError("invalid raw members")
    member_paths: list[str] = []
    for member in raw["members"]:
        item = _exact_keys(member, {"path", "size_bytes", "sha256"}, "raw member")
        member_paths.append(_path(item["path"], "raw member path"))
        _integer(item["size_bytes"], "raw member size")
        _sha(item["sha256"], "raw member")
    if member_paths != sorted(set(member_paths), key=lambda p: p.encode("utf-8")):
        raise BackupError("raw members must be unique and byte-sorted")
    return top


def _hash_tar_stream(source: BinaryIO) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=source, mode="r:") as archive:
            for member in archive:
                path = _path(member.name, "tar member path")
                if path in seen or not member.isfile() or member.issym() or member.islnk():
                    raise BackupError("unsafe or duplicate raw archive member")
                seen.add(path)
                stream = archive.extractfile(member)
                if stream is None:
                    raise BackupError("raw archive member has no data")
                digest = hashlib.sha256()
                size = 0
                with stream:
                    for block in iter(lambda stream=stream: stream.read(1024 * 1024), b""):
                        size += len(block)
                        digest.update(block)
                if size != member.size:
                    raise BackupError("raw archive member size mismatch")
                result.append({"path": path, "size_bytes": size, "sha256": digest.hexdigest()})
    except (tarfile.TarError, OSError) as exc:
        raise BackupError("invalid raw archive") from exc
    return sorted(result, key=lambda item: item["path"].encode("utf-8"))


def _hash_tar_members(tar_path: Path) -> list[dict[str, Any]]:
    try:
        with tar_path.open("rb") as handle:
            return _hash_tar_stream(handle)
    except OSError as exc:
        raise BackupError("invalid raw archive") from exc


def _hash_tar_members_from_bytes(data: bytes) -> list[dict[str, Any]]:
    return _hash_tar_stream(io.BytesIO(data))


def validate_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    absolute = path.absolute()
    probe = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        probe = probe / part
        if probe.is_symlink():
            raise BackupError("snapshot path contains a symlink")
    if path.is_symlink() or not path.is_dir():
        raise BackupError("snapshot path is not a real directory")
    if stat.S_IMODE(path.stat().st_mode) != 0o700 or path.stat().st_uid != os.getuid():
        raise BackupError("snapshot directory is not owner-only")
    if (path / "INCOMPLETE").exists() or (path / "INCOMPLETE").is_symlink():
        raise BackupError("snapshot is incomplete")
    if {child.name for child in path.iterdir()} != {"database.dump", "raw.tar", "manifest.json", "COMPLETE"}:
        raise BackupError("snapshot has missing or extra entries")
    for name in ("database.dump", "raw.tar", "manifest.json", "COMPLETE"):
        entry = path / name
        if (
            entry.is_symlink()
            or not entry.is_file()
            or stat.S_IMODE(entry.stat().st_mode) != 0o600
            or entry.stat().st_uid != os.getuid()
        ):
            raise BackupError("snapshot contains unsafe file")
    manifest_bytes = (path / "manifest.json").read_bytes()
    manifest = validate_manifest(strict_json(manifest_bytes))
    if canonical_json(manifest) != manifest_bytes:
        raise BackupError("manifest bytes are not canonical")
    digest = sha256_bytes(manifest_bytes)
    if (path / "COMPLETE").read_bytes() != f"{MARKER_PREFIX}{digest}\n".encode("ascii"):
        raise BackupError("snapshot completion marker mismatch")
    dump = manifest["database_dump"]
    if (path / "database.dump").stat().st_size != dump["size_bytes"] or sha256_file(
        path / "database.dump"
    ) != dump["sha256"]:
        raise BackupError("database dump hash mismatch")
    raw = manifest["raw_archive"]
    if (path / "raw.tar").stat().st_size != raw["size_bytes"] or sha256_file(path / "raw.tar") != raw[
        "sha256"
    ]:
        raise BackupError("raw archive hash mismatch")
    if _hash_tar_members(path / "raw.tar") != raw["members"]:
        raise BackupError("raw archive inventory mismatch")
    return manifest, digest


def dirty_tree_sha256(repo: Path) -> str:
    result = _run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo, binary=True)
    records = result.stdout.split(b"\0")
    changed: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(records):
        item = records[index]
        index += 1
        if not item:
            continue
        if len(item) < 4:
            raise BackupError("git status returned malformed paths")
        status = item[:2].decode("ascii", "replace")
        raw_name = item[3:]
        # Rename/copy records have a second NUL-separated path. Hash both names.
        names = [raw_name]
        if "R" in status or "C" in status:
            if index >= len(records):
                raise BackupError("git status rename record was incomplete")
            names.append(records[index])
            index += 1
        for raw in names:
            name = raw.decode("utf-8", "strict")
            if name in {"deploy/.env", "deploy/.env.local"} or Path(name).name.startswith(".env."):
                continue
            candidate = repo / name
            if candidate.is_file() and not candidate.is_symlink():
                changed[name] = {"path": name, "state": "present", "sha256": sha256_file(candidate)}
            else:
                changed[name] = {"path": name, "state": "deleted", "sha256": None}
    rows = sorted(changed.values(), key=lambda item: item["path"].encode("utf-8"))
    return sha256_bytes(canonical_json(rows))


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    input_data: bytes | None = None,
    binary: bool = False,
    check: bool = True,
    stdout: BinaryIO | None = None,
) -> subprocess.CompletedProcess[Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            input=input_data,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupError("required local command is unavailable") from exc
    if check and proc.returncode:
        raise BackupError("bounded backup operation failed")
    if binary:
        return proc
    try:
        proc.stdout = proc.stdout.decode("utf-8") if isinstance(proc.stdout, bytes) else proc.stdout
    except UnicodeDecodeError as exc:
        raise BackupError("command returned non-UTF-8 metadata") from exc
    return proc


def _stream_to_process(args: list[str], source: BinaryIO) -> int:
    """Stream a large private archive to a child without buffering it in RAM."""
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        raise BackupError("required local command is unavailable") from exc
    assert proc.stdin is not None
    try:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            proc.stdin.write(block)
        proc.stdin.close()
        return proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise


def _pg_restore_error_category(stderr: bytes) -> str:
    message = stderr.lower()
    if b"permission denied" in message or b"must be owner of" in message:
        return "insufficient_privilege"
    if b"already exists" in message or b"duplicate key" in message:
        return "object_conflict"
    if b"does not exist" in message or b"undefined table" in message or b"undefined object" in message:
        return "referenced_object_missing"
    if b"unsupported version" in message or b"unsupported feature" in message:
        return "unsupported_dump_feature"
    if b"could not execute query" in message:
        return "restore_sql_error"
    if b"pg_restore:" in message:
        return "restore_client_error"
    if b"error:" in message:
        return "restore_sql_error"
    return "unclassified_restore_error"


def _stream_pg_restore(args: list[str], source: BinaryIO) -> tuple[int, str]:
    """Stream a private dump and retain only a fixed stderr category in memory."""
    try:
        with tempfile.TemporaryFile(mode="w+b") as error_buffer:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=error_buffer
            )
            assert proc.stdin is not None
            try:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    try:
                        proc.stdin.write(block)
                    except BrokenPipeError:
                        break
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                code = proc.wait()
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            error_buffer.seek(0, os.SEEK_END)
            size = error_buffer.tell()
            error_buffer.seek(max(0, size - 1024 * 1024))
            category = _pg_restore_error_category(error_buffer.read()) if code else "none"
            return code, category
    except OSError as exc:
        raise BackupError("required local command is unavailable") from exc


def _pg_restore_args(container_id: str) -> list[str]:
    return [
        "docker",
        "exec",
        "-i",
        container_id,
        "pg_restore",
        "--exit-on-error",
        "--no-owner",
        "-Fc",
        "-U",
        "crowley",
        "-d",
        "crowley",
    ]


def _docker(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    return _run(["docker", *args], **kwargs)


def _json_output(raw: str, what: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError(f"invalid Docker {what} metadata") from exc


def _labels(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise BackupError("invalid Docker resource labels")
    return value


def _network_membership(value: Any) -> dict[str, str | None]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BackupError("invalid Docker network attachment metadata")
    result: dict[str, str | None] = {}
    for name, attachment in value.items():
        if not isinstance(name, str) or not isinstance(attachment, dict):
            raise BackupError("invalid Docker network attachment metadata")
        network_id = attachment.get("NetworkID")
        if network_id in (None, ""):
            result[name] = None
        elif isinstance(network_id, str):
            result[name] = network_id
        else:
            raise BackupError("invalid Docker network ID")
    return result


def _helper_uses_only_none_network(container: dict[str, Any], none_network_id: str) -> bool:
    networks = container.get("networks")
    if not isinstance(networks, dict) or set(networks) != {"none"}:
        return False
    attachment = networks["none"]
    if not isinstance(attachment, dict):
        return False
    network_id = attachment.get("NetworkID")
    if container.get("state") == "created":
        if network_id not in (None, "", none_network_id):
            return False
    elif network_id != none_network_id:
        return False
    return all(
        attachment.get(field) in (None, "")
        for field in ("EndpointID", "IPAddress", "Gateway", "GlobalIPv6Address", "MacAddress")
    )


def _builtin_none_network_id(live: dict[str, Any]) -> str:
    matches = [network["id"] for network in live["networks"].values() if network["name"] == "none"]
    if len(matches) != 1:
        raise BackupError("Docker isolated none network identity is ambiguous")
    return matches[0]


def _same_container_identity(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    if not all(
        current[field] == expected[field] for field in ("id", "image_id", "labels", "mounts", "ports")
    ):
        return False
    expected_networks = _network_membership(expected["networks"])
    current_networks = _network_membership(current["networks"])
    if expected_networks.keys() != current_networks.keys():
        return False
    for name, expected_network_id in expected_networks.items():
        current_network_id = current_networks[name]
        if (
            expected_network_id is not None
            and current_network_id is not None
            and expected_network_id != current_network_id
        ):
            return False
        if current.get("state") == "running" and current_network_id is None:
            return False
    return True


def _target_network_attachment_matches(container: dict[str, Any], expected: dict[str, str]) -> bool:
    current = _network_membership(container["networks"])
    if current.keys() != expected.keys():
        return False
    if any(network_id not in (None, expected[name]) for name, network_id in current.items()):
        return False
    return container["state"] != "running" or current == expected


def _inspect_container(container_id: str) -> dict[str, Any]:
    fmt = (
        "{{json .Id}}|{{json .Image}}|{{json .Config.Image}}|{{json .Config.Labels}}|"
        "{{json .Mounts}}|{{json .NetworkSettings.Networks}}|{{json .HostConfig.PortBindings}}|"
        "{{json .State.Status}}"
    )
    fields = _docker(["inspect", "--format", fmt, container_id]).stdout.rstrip("\n").split("|", 7)
    if len(fields) != 8:
        raise BackupError("container identity inspection failed")
    result = dict(
        zip(
            ("id", "image_id", "config_image", "labels", "mounts", "networks", "ports", "state"),
            (_json_output(field, "container") for field in fields),
            strict=True,
        )
    )
    result["labels"] = _labels(result["labels"])
    return result


def _inspect_volume(name: str) -> dict[str, Any]:
    fmt = "{{json .Name}}|{{json .Mountpoint}}|{{json .Labels}}"
    fields = _docker(["volume", "inspect", "--format", fmt, name]).stdout.rstrip("\n").split("|", 2)
    if len(fields) != 3:
        raise BackupError("volume identity inspection failed")
    result = dict(
        zip(
            ("name", "mountpoint", "labels"), (_json_output(field, "volume") for field in fields), strict=True
        )
    )
    result["labels"] = _labels(result["labels"])
    return result


def _inspect_network(network_id: str) -> dict[str, Any]:
    fmt = "{{json .Id}}|{{json .Name}}|{{json .Labels}}"
    fields = _docker(["network", "inspect", "--format", fmt, network_id]).stdout.rstrip("\n").split("|", 2)
    if len(fields) != 3:
        raise BackupError("network identity inspection failed")
    result = dict(
        zip(("id", "name", "labels"), (_json_output(field, "network") for field in fields), strict=True)
    )
    result["labels"] = _labels(result["labels"])
    return result


def _image_identity(image_id: str) -> dict[str, Any]:
    # Request only the two image identity fields. Never inspect image or
    # container environment values, which may contain deployment secrets.
    fields = (
        _docker(["image", "inspect", image_id, "--format", "{{json .Id}}|{{json .RepoDigests}}"])
        .stdout.rstrip("\n")
        .split("|", 1)
    )
    if len(fields) != 2:
        raise BackupError("image identity inspection failed")
    image, digests = (_json_output(field, "image") for field in fields)
    return {"id": image, "repo_digests": sorted(set(digests or []))}


def _live_inventory() -> dict[str, Any]:
    containers: dict[str, dict[str, Any]] = {}
    for container_id in _docker(["ps", "-aq"]).stdout.splitlines():
        if container_id:
            info = _inspect_container(container_id)
            containers[info["id"]] = info
    volumes: dict[str, dict[str, Any]] = {}
    for name in _docker(["volume", "ls", "-q"]).stdout.splitlines():
        if name:
            info = _inspect_volume(name)
            volumes[info["name"]] = info
    networks: dict[str, dict[str, Any]] = {}
    for network_id in _docker(["network", "ls", "-q"]).stdout.splitlines():
        if network_id:
            info = _inspect_network(network_id)
            networks[info["id"]] = info
    return {"containers": containers, "volumes": volumes, "networks": networks}


def _compose_base(repo: Path, extra: list[str] | None = None) -> list[str]:
    args = [
        "compose",
        "--project-directory",
        str(repo / "deploy"),
        "-f",
        str(repo / COMPOSE),
        "--env-file",
        str(repo / ENV_FILE),
    ]
    if extra:
        args.extend(extra)
    return args


def _compose_config(repo: Path, extras: list[str] | None = None) -> dict[str, Any]:
    raw = _docker(_compose_base(repo, extras) + ["config", "--format", "json"]).stdout
    return _json_output(raw, "compose config")


def _source_compose(repo: Path) -> dict[str, Any]:
    config = _compose_config(repo)
    if config.get("name") != "crowley" or set(config.get("services", {})) != {"db", "server"}:
        raise BackupError("unexpected source Compose project or services")
    if set(config.get("volumes", {})) != {"crowley-db", "crowley-raw"}:
        raise BackupError("unexpected source Compose volumes")
    return config


def _repo_digest(path: Path) -> str:
    return sha256_file(path)


def _migration_inventory(repo: Path) -> list[dict[str, str]]:
    files = sorted((repo / MIGRATIONS).glob("*.sql"), key=lambda p: p.as_posix().encode("utf-8"))
    if not files:
        raise BackupError("no migration files found")
    return [{"path": p.relative_to(repo).as_posix(), "sha256": sha256_file(p)} for p in files]


def _local_version(container_id: str) -> tuple[str, str]:
    sql = (
        "SELECT current_setting('server_version'), extversion "
        "FROM pg_extension JOIN pg_namespace n ON n.oid=extnamespace "
        "WHERE extname='timescaledb'"
    )
    result = _docker(
        [
            "exec",
            container_id,
            "psql",
            "-X",
            "-w",
            "-A",
            "-t",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "crowley",
            "-d",
            "crowley",
            "-c",
            sql,
        ]
    ).stdout.strip()
    parts = result.split("|")
    if len(parts) != 2 or not all(parts):
        raise BackupError("could not read source PostgreSQL/TimescaleDB versions")
    return parts[0], parts[1]


def _safe_repo_commit(repo: Path) -> str:
    commit = _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise BackupError("invalid source commit metadata")
    return commit


def _set_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, mode)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_root(root: Path) -> None:
    if not root.is_absolute():
        raise BackupError("private backup root must be absolute")
    cur = Path(root.anchor)
    for index, part in enumerate(root.parts[1:], start=1):
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            if cur.is_symlink() or not cur.is_dir():
                raise BackupError("private backup path contains an unsafe component")
        else:
            cur.mkdir(mode=0o700)
            # Newly created ancestors belong to this process; existing parent
            # directories may legitimately be root-owned (for example /home).
            if index == len(root.parts[1:]) and cur.stat().st_uid != os.getuid():
                raise BackupError("private backup root is not owned by this user")
    if root.stat().st_uid != os.getuid():
        raise BackupError("private backup root is not owned by this user")
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise BackupError("private backup root must have mode 0700")


RAW_WALK_SCRIPT = r"""import hashlib,os,stat,sys,tarfile
import re
root='/data'; out=sys.stdout.buffer
with tarfile.open(fileobj=out,mode='w|',format=tarfile.PAX_FORMAT) as tar:
    stack=[(root,'')]
    while stack:
        absolute,relative=stack.pop()
        with os.scandir(absolute) as it:
            entries=sorted(list(it),key=lambda e:e.name.encode('utf-8'),reverse=True)
        for entry in entries:
            name=entry.name
            path=(relative+'/'+name).lstrip('/')
            info=entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode): raise SystemExit(41)
            if stat.S_ISDIR(info.st_mode):
                stack.append((entry.path,path)); continue
            if not stat.S_ISREG(info.st_mode): raise SystemExit(42)
            if re.fullmatch(r'\.[0-9a-f]{64}\..+\.tmp',name): continue
            ti=tarfile.TarInfo(path); ti.size=info.st_size; ti.mode=0o600; ti.mtime=0
            with open(entry.path,'rb') as f: tar.addfile(ti,f)
"""


def _capture_raw_volume(repo: Path, raw_volume: str, server_image_id: str, output: Path) -> None:
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        f"type=volume,src={raw_volume},dst=/data,readonly",
        "--entrypoint",
        "/usr/local/bin/python",
        server_image_id,
        "-c",
        RAW_WALK_SCRIPT,
    ]
    with output.open("xb") as handle:
        os.chmod(output, 0o600)
        proc = _run(cmd, cwd=repo, binary=True, stdout=handle, check=False)
        handle.flush()
        os.fsync(handle.fileno())
    if proc.returncode:
        raise BackupError("read-only raw-volume snapshot helper failed")


def _source_resources(repo: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = _source_compose(repo)
    project = config["name"]
    current = _live_inventory()
    source_containers: dict[str, dict[str, Any]] = {}
    for service in ("db", "server"):
        matches = [
            c
            for c in current["containers"].values()
            if c["labels"].get("com.docker.compose.project") == project
            and c["labels"].get("com.docker.compose.service") == service
        ]
        if len(matches) != 1 or matches[0]["state"] != "running":
            raise BackupError("source Compose service identity is ambiguous or stopped")
        source_containers[service] = matches[0]
    db, server = source_containers["db"], source_containers["server"]
    expected = {
        "db": {"/var/lib/postgresql/data": "crowley-db"},
        "server": {"/data": "crowley-raw"},
    }
    volumes: dict[str, dict[str, str]] = {}
    for service, container in source_containers.items():
        mounted = {m["Destination"]: m for m in container["mounts"] if m["Type"] == "volume"}
        for dest, key in expected[service].items():
            mount = mounted.get(dest)
            if mount is None or not mount["RW"]:
                raise BackupError("source Compose volume identity mismatch")
            volume_info = current["volumes"].get(mount["Name"])
            if volume_info is None:
                raise BackupError("source volume is missing")
            label_project = volume_info["labels"].get("com.docker.compose.project")
            if label_project != project:
                raise BackupError("source volume project label mismatch")
            volumes[key] = {"name": volume_info["name"], "mountpoint": volume_info["mountpoint"]}
    networks: list[dict[str, str]] = []
    network_ids: set[str] = set()
    for container in source_containers.values():
        for net in container["networks"].values():
            network_ids.add(net["NetworkID"])
    for network_id in network_ids:
        net = current["networks"].get(network_id)
        if not net or net["labels"].get("com.docker.compose.project") != project:
            raise BackupError("source network identity mismatch")
        networks.append({"name": net["name"], "id": net["id"]})
    networks.sort(key=lambda n: n["name"].encode("utf-8"))
    return (
        config,
        {"db": db, "server": server},
        {"volumes": volumes, "networks": networks, "live_inventory": current},
    )


def snapshot(repo: Path, root: Path) -> Path:
    repo = repo.resolve(strict=True)
    _ensure_root(root)
    config, containers, inventory = _source_resources(repo)
    database, server = containers["db"], containers["server"]
    database_image = _image_identity(database["image_id"])
    server_image = _image_identity(server["image_id"])
    postgres_version, timescale_version = _local_version(database["id"])
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    backup_id = f"{NEW_BACKUP_ID_PREFIX}{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    out = root / backup_id
    out.mkdir(mode=0o700)
    _fsync_dir(root)
    _set_private(out / "INCOMPLETE", b"")
    _fsync_dir(out)
    dump_path, raw_path = out / "database.dump", out / "raw.tar"
    try:
        with dump_path.open("xb") as handle:
            os.chmod(dump_path, 0o600)
            proc = _run(
                ["docker", "exec", database["id"], "pg_dump", "-w", "-U", "crowley", "-d", "crowley", "-Fc"],
                binary=True,
                stdout=handle,
                check=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        if proc.returncode:
            raise BackupError("database dump failed")
        _capture_raw_volume(repo, inventory["volumes"]["crowley-raw"]["name"], server["image_id"], raw_path)
        raw_members = _hash_tar_members(raw_path)
        status = _run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo, binary=True
        )
        del status
        dirty_hash = dirty_tree_sha256(repo)
        manifest = {
            "schema": SCHEMA,
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at_utc": now.isoformat().replace("+00:00", "Z"),
            "source": {
                "compose_file_sha256": _repo_digest(repo / COMPOSE),
                "compose_project": config["name"],
                "services": {"database": "db", "server": "server"},
                "containers": {
                    "database": {
                        "id": database["id"],
                        "image_id": database_image["id"],
                        "repo_digests": database_image["repo_digests"],
                    },
                    "server": {
                        "id": server["id"],
                        "image_id": server_image["id"],
                        "repo_digests": server_image["repo_digests"],
                    },
                },
                "volumes": {
                    "database": inventory["volumes"]["crowley-db"],
                    "raw_archive": inventory["volumes"]["crowley-raw"],
                },
                "networks": inventory["networks"],
                "source_commit": _safe_repo_commit(repo),
                "dirty_tree_sha256": dirty_hash,
                "migrations": _migration_inventory(repo),
                "postgres_server_version": postgres_version,
                "timescaledb_extension_version": timescale_version,
            },
            "database_dump": {
                "path": "database.dump",
                "format": "postgres-custom",
                "size_bytes": dump_path.stat().st_size,
                "sha256": sha256_file(dump_path),
            },
            "raw_archive": {
                "path": "raw.tar",
                "format": "tar",
                "size_bytes": raw_path.stat().st_size,
                "sha256": sha256_file(raw_path),
                "members": raw_members,
            },
        }
        validate_manifest(manifest)
        manifest_bytes = canonical_json(manifest)
        _set_private(out / "manifest.json", manifest_bytes)
        _fsync_dir(out)
        digest = sha256_bytes(manifest_bytes)
        temp_marker = out / f".COMPLETE.{uuid.uuid4().hex}.tmp"
        _set_private(temp_marker, f"{MARKER_PREFIX}{digest}\n".encode("ascii"))
        os.replace(temp_marker, out / "COMPLETE")
        _fsync_dir(out)
        (out / "INCOMPLETE").unlink()
        _fsync_dir(out)
        validate_snapshot(out)
        return out
    except BaseException:
        # Keep the INCOMPLETE evidence in place. Never erase a partial backup.
        raise


def validate_compose_isolation(
    config: dict[str, Any],
    project: str,
    source_project: str = "crowley",
    expected_images: dict[str, str] | None = None,
) -> None:
    if config.get("name") != project or project == source_project:
        raise BackupError("restore project is not isolated")
    services = config.get("services")
    if not isinstance(services, dict) or set(services) != {"db", "server"}:
        raise BackupError("restore Compose services are unexpected")
    for name, service in services.items():
        if service.get("ports") not in (None, []):
            raise BackupError("restore Compose configuration contains host ports")
        if service.get("restart") not in (None, "no"):
            raise BackupError("restore Compose restart policy is not disabled")
        if expected_images is not None and service.get("image") != expected_images.get(name):
            raise BackupError("restore service image is not the recorded local image")
    networks = config.get("networks", {})
    if len(networks) != 1 or any(not net.get("internal", False) for net in networks.values()):
        raise BackupError("restore network is not private internal")
    volumes = config.get("volumes", {})
    expected_volume_keys = {"crowley-db", "crowley-raw"}
    if set(volumes) != expected_volume_keys or any(
        not isinstance(volume, dict) or volume.get("external", False) for volume in volumes.values()
    ):
        raise BackupError("restore volumes are not disposable project volumes")
    for volume in volumes.values():
        if not isinstance(volume.get("name"), str) or not volume["name"].startswith(project + "_"):
            raise BackupError("restore volume is not project-scoped")
    expected_mounts = {
        "db": ("crowley-db", "/var/lib/postgresql/data"),
        "server": ("crowley-raw", "/data"),
    }
    for service_name, service in services.items():
        mounts = service.get("volumes")
        source, destination = expected_mounts[service_name]
        if (
            not isinstance(mounts, list)
            or len(mounts) != 1
            or not isinstance(mounts[0], dict)
            or mounts[0].get("type") != "volume"
            or mounts[0].get("source") != source
            or mounts[0].get("target") != destination
        ):
            raise BackupError("restore service mounts are not the expected isolated volumes")


def raw_reference_path(raw_path: str, inventory: set[str]) -> str:
    if not isinstance(raw_path, str) or not raw_path.startswith("/data/"):
        raise BackupError("raw path is outside the exact /data/ prefix")
    relative = _path(raw_path[len("/data/") :], "raw path")
    if relative not in inventory:
        raise BackupError("raw reference is absent from archive inventory")
    return relative


def validate_format2_member(
    member_bytes: bytes, row: dict[str, Any], plans: dict[str, str]
) -> dict[str, Any]:
    from crowley.core.scanformat import decode_strict_json, validate_v2_envelope
    from crowley.server.ingest import _logical_digest

    try:
        envelope = decode_strict_json(gzip.decompress(member_bytes))
        validated = validate_v2_envelope(envelope)
        digest = _logical_digest(envelope, validated["decoded"])
    except Exception as exc:
        raise BackupError("format-2 raw member failed strict validation") from exc
    expected = row["content_digest"]
    expected_path = f"raw/format2/{expected[:2]}/{expected}.json.gz"
    if digest != expected or row["inventory_path"] != expected_path:
        raise BackupError("format-2 content digest/path mismatch")
    if envelope.get("id") != row["sweep_uid"]:
        raise BackupError("format-2 sweep identity mismatch")
    house = envelope.get("house")
    context = envelope.get("context")
    decoded = validated["decoded"]
    plan_json = decoded["planJSON"]
    decoded_hash = decoded["planHash"]
    if not isinstance(house, dict) or house.get("key") != row["house_key"]:
        raise BackupError("format-2 house identity mismatch")
    if (
        not isinstance(context, dict)
        or context.get("clientBuild") != row["client_build"]
        or context.get("interface") != row["interface"]
    ):
        raise BackupError("format-2 client identity mismatch")
    if decoded_hash != row["plan_hash"]:
        raise BackupError("format-2 plan hash mismatch")
    restored_plan = plans.get(row["plan_hash"])
    if restored_plan is None or plan_json != restored_plan:
        raise BackupError("format-2 plan JSON differs from restored plan")
    return {
        "logical_digest": digest,
        "sweep_uid": envelope["id"],
        "house_key": house["key"],
        "plan_hash": decoded_hash,
    }


def classify_orphans(
    inventory: list[dict[str, Any]],
    referenced: set[str],
    validators: dict[str, tuple[bool, str | None]] | None = None,
) -> list[dict[str, Any]]:
    validators = validators or {}
    result = []
    for member in inventory:
        path = member["path"]
        if path not in referenced:
            valid, warning = validators.get(path, (False, "not independently validated"))
            result.append(
                {
                    "path": path,
                    "sha256": member["sha256"],
                    "validation": "valid_format2" if valid else "unvalidated",
                    "warning": warning,
                }
            )
    return result


def _compose_args(repo: Path, project: str, override: Path, credentials: Path) -> list[str]:
    return [
        "compose",
        "--project-directory",
        str(repo / "deploy"),
        "-f",
        str(repo / COMPOSE),
        "-f",
        str(override),
        "--env-file",
        str(repo / ENV_FILE),
        "--env-file",
        str(credentials),
        "--project-name",
        project,
    ]


def _target_compose(
    repo: Path,
    project: str,
    override: Path,
    credentials: Path,
    args: list[str],
    *,
    check: bool = True,
    input_data: bytes | None = None,
) -> subprocess.CompletedProcess[Any]:
    return _docker(
        _compose_args(repo, project, override, credentials) + args, check=check, input_data=input_data
    )


def _labels_ok(labels: Any, project: str, service: str) -> bool:
    return (
        isinstance(labels, dict)
        and labels.get("com.docker.compose.project") == project
        and labels.get("com.docker.compose.service") == service
    )


def _target_ids(repo: Path, project: str, override: Path, credentials: Path) -> dict[str, Any]:
    ids = [
        line
        for line in _target_compose(repo, project, override, credentials, ["ps", "-aq"]).stdout.splitlines()
        if line
    ]
    inspected = [_inspect_container(cid) for cid in ids]
    containers = {item["labels"].get("com.docker.compose.service"): item for item in inspected}
    if len(ids) != 2 or len(containers) != 2 or set(containers) != {"db", "server"}:
        raise BackupError("disposable container set is incomplete or ambiguous")
    volumes = [
        line
        for line in _docker(
            ["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]
        ).stdout.splitlines()
        if line
    ]
    inspected_volumes = [_inspect_volume(name) for name in volumes]
    networks = [
        line
        for line in _docker(
            ["network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"]
        ).stdout.splitlines()
        if line
    ]
    inspected_networks = [_inspect_network(nid) for nid in networks]
    return {"containers": containers, "volumes": inspected_volumes, "networks": inspected_networks}


def _assert_project_absent(live: dict[str, Any], project: str) -> None:
    if (
        any(
            _labels(c.get("labels")).get("com.docker.compose.project") == project
            for c in live["containers"].values()
        )
        or any(
            _labels(v.get("labels")).get("com.docker.compose.project") == project
            for v in live["volumes"].values()
        )
        or any(
            _labels(n.get("labels")).get("com.docker.compose.project") == project
            for n in live["networks"].values()
        )
    ):
        raise BackupError("generated restore project already has resources")


def _guard_current(
    repo: Path,
    project: str,
    override: Path,
    credentials: Path,
    source: dict[str, Any],
    expected: dict[str, Any],
    helpers: list[dict[str, Any]],
) -> dict[str, Any]:
    fresh = _live_inventory()
    _assert_source_current(fresh, source)
    expected_containers = {item["id"] for item in expected["containers"].values()} | {
        item["id"] for item in helpers
    }
    expected_volumes = {item["name"] for item in expected["volumes"]}
    expected_networks = {item["id"] for item in expected["networks"]}
    project_containers = {
        cid
        for cid, item in fresh["containers"].items()
        if item["labels"].get("com.docker.compose.project") == project
    }
    project_volumes = {
        name
        for name, item in fresh["volumes"].items()
        if item["labels"].get("com.docker.compose.project") == project
    }
    project_networks = {
        nid
        for nid, item in fresh["networks"].items()
        if item["labels"].get("com.docker.compose.project") == project
    }
    if (
        project_containers != expected_containers
        or project_volumes != expected_volumes
        or project_networks != expected_networks
    ):
        raise BackupError("disposable project resource identities are ambiguous")
    external = {
        "containers": {
            key: val
            for key, val in fresh["containers"].items()
            if val["labels"].get("com.docker.compose.project") != project
        },
        "volumes": {
            key: val
            for key, val in fresh["volumes"].items()
            if val["labels"].get("com.docker.compose.project") != project
        },
        "networks": {
            key: val
            for key, val in fresh["networks"].items()
            if val["labels"].get("com.docker.compose.project") != project
        },
    }
    target_now = _target_ids(repo, project, override, credentials)
    _target_guard(target_now, project, source, external, expected=expected)
    target_membership = {network["name"]: network["id"] for network in target_now["networks"]}
    for helper in helpers:
        current = _inspect_container(helper["id"])
        service = helper["labels"].get("com.docker.compose.service")
        if (
            current["id"] != helper["id"]
            or not _labels_ok(current["labels"], project, service)
            or current["mounts"] != helper["mounts"]
            or (
                not _target_network_attachment_matches(current, target_membership)
                if service == "reference-migration-helper"
                else not _helper_uses_only_none_network(current, source["none_network_id"])
            )
        ):
            raise BackupError("disposable helper identity changed")
        helper.update(current)
    return target_now


def _assert_source_current(live: dict[str, Any], source: dict[str, Any]) -> None:
    for recorded in source["containers"].values():
        current = live["containers"].get(recorded["id"])
        if current is None or not _same_container_identity(recorded, current):
            raise BackupError("source container identity changed during restore")
    for recorded in source["volumes"].values():
        current = live["volumes"].get(recorded["name"])
        if current is None or current != recorded:
            raise BackupError("source volume identity changed during restore")
    for recorded in source["networks"]:
        current = live["networks"].get(recorded["id"])
        if current is None or current != recorded:
            raise BackupError("source network identity changed during restore")


def _target_guard(
    target: dict[str, Any],
    project: str,
    source: dict[str, Any],
    live: dict[str, Any],
    *,
    expected: dict[str, Any] | None = None,
    service_state: str | None = None,
) -> None:
    if set(target["containers"]) != {"db", "server"}:
        raise BackupError("disposable target services are ambiguous")
    recorded = source.get("recorded", {})
    source_ids = {c["id"] for c in source["containers"].values()} | set(recorded.get("container_ids", []))
    source_volume_names = {v["name"] for v in source["volumes"].values()} | set(
        recorded.get("volume_names", [])
    )
    source_networks = [*source["networks"], *recorded.get("networks", [])]
    source_network_ids = {n["id"] for n in source_networks}
    source_network_names = {n["name"] for n in source_networks}
    live_ids = set(live["containers"])
    live_volume_names = set(live["volumes"])
    live_network_ids = set(live["networks"])
    live_network_names = {n["name"] for n in live["networks"].values()}
    if len(target["volumes"]) != 2:
        raise BackupError("disposable resource set is not exact")
    target_volumes = {volume["name"]: volume for volume in target["volumes"]}
    if len(target_volumes) != 2:
        raise BackupError("disposable target volumes are not distinct")
    if set(target_volumes) & (live_volume_names | source_volume_names):
        raise BackupError("disposable target uses an existing/source volume")
    for volume in target_volumes.values():
        if (
            not isinstance(volume.get("labels"), dict)
            or volume["labels"].get("com.docker.compose.project") != project
            or not volume["name"].startswith(project + "_")
            or not isinstance(volume["mountpoint"], str)
        ):
            raise BackupError("target volume ownership is ambiguous")
    for service, expected_dest in (("db", "/var/lib/postgresql/data"), ("server", "/data")):
        container = target["containers"][service]
        if (
            not _labels_ok(container["labels"], project, service)
            or container["id"] in source_ids
            or container["id"] in live_ids
            or not isinstance(container.get("image_id"), str)
        ):
            raise BackupError("disposable container identity is not fresh and isolated")
        if service_state is not None and container["state"] != service_state:
            raise BackupError("disposable container state changed")
        if expected is not None:
            recorded = expected["containers"][service]
            if not _same_container_identity(recorded, container):
                raise BackupError("disposable container identity changed")
        recorded_images = source.get("recorded_images", {})
        if service in recorded_images and container["image_id"] != recorded_images[service]:
            raise BackupError("disposable container image differs from recorded image")
        if container["ports"] not in (None, {}) and any(bindings for bindings in container["ports"].values()):
            raise BackupError("disposable container has host port bindings")
        mounts = container["mounts"]
        if (
            not isinstance(mounts, list)
            or len(mounts) != 1
            or not isinstance(mounts[0], dict)
            or mounts[0].get("Destination") != expected_dest
            or mounts[0].get("Type") != "volume"
            or not isinstance(mounts[0].get("Name"), str)
            or not isinstance(mounts[0].get("Source"), str)
            or not isinstance(mounts[0].get("RW"), bool)
        ):
            raise BackupError("disposable container has an unexpected data mount")
        volume = mounts[0]["Name"]
        if volume in live_volume_names or volume in source_volume_names:
            raise BackupError("disposable target uses an existing/source volume")
        if mounts[0]["RW"] is not True:
            raise BackupError("disposable target mount has unexpected mode")
        if volume not in target_volumes:
            raise BackupError("target volume is not project-owned")
        if mounts[0]["Source"] != target_volumes[volume]["mountpoint"]:
            raise BackupError("disposable target mount source is inconsistent")
        expected_volume_key = source.get("target_volume_keys", {}).get(service)
        if (
            expected_volume_key is not None
            and target_volumes[volume]["labels"].get("com.docker.compose.volume") != expected_volume_key
        ):
            raise BackupError("disposable target volume role is inconsistent")
    mounted_volumes = {container["mounts"][0]["Name"] for container in target["containers"].values()}
    if mounted_volumes != set(target_volumes):
        raise BackupError("database and raw target volumes are not distinct and exact")
    if not target["networks"]:
        raise BackupError("disposable network is missing")
    if len(target["networks"]) != 1:
        raise BackupError("disposable resource set is not exact")
    expected_membership = {network["name"]: network["id"] for network in target["networks"]}
    if any(
        not _target_network_attachment_matches(container, expected_membership)
        for container in target["containers"].values()
    ):
        raise BackupError("disposable container network attachment changed")
    for network in target["networks"]:
        if (
            not isinstance(network.get("labels"), dict)
            or network["labels"].get("com.docker.compose.project") != project
            or network["id"] in source_network_ids
            or network["id"] in live_network_ids
            or network["name"] in source_network_names
            or network["name"] in live_network_names
            or not network["name"].startswith(project + "_")
        ):
            raise BackupError("disposable network identity is not fresh")
    if expected is not None and (
        target["volumes"] != expected["volumes"] or target["networks"] != expected["networks"]
    ):
        raise BackupError("disposable volume or network identity changed")


def _db_sql(container_id: str, sql: str, db: str = "crowley") -> str:
    return _docker(
        [
            "exec",
            container_id,
            "psql",
            "-X",
            "-w",
            "-A",
            "-t",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "crowley",
            "-d",
            db,
            "-c",
            sql,
        ]
    ).stdout.strip()


def _json_sql(container_id: str, sql: str, db: str = "crowley") -> Any:
    text = _db_sql(container_id, sql, db)
    try:
        return json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise BackupError("restored database returned invalid metadata JSON") from exc


def _schema_filter(alias: str) -> str:
    excluded = ", ".join(f"'{name}'" for name in sorted(TIMESCALE_INTERNAL_SCHEMAS))
    return (
        f"{alias}.nspname NOT LIKE 'pg_%' "
        f"AND {alias}.nspname <> 'information_schema' "
        f"AND {alias}.nspname NOT IN ({excluded})"
    )


def _catalog_queries() -> tuple[str, ...]:
    schema_filter = _schema_filter("n")
    return (
        "SELECT coalesce(json_agg(json_build_object('schema', n.nspname, 'name', c.relname) "
        f"ORDER BY n.nspname, c.relname), '[]'::json) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind IN ('r', 'p') AND {schema_filter}",
        "SELECT coalesce(json_agg(json_build_object("
        "'schema', n.nspname, 'table', c.relname, 'name', a.attname, "
        "'data_type', format_type(a.atttypid, a.atttypmod), 'nullable', NOT a.attnotnull, "
        "'default_expression', pg_get_expr(d.adbin, d.adrelid)) "
        f"ORDER BY n.nspname, c.relname, a.attnum), '[]'::json) FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
        f"WHERE c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped AND {schema_filter}",
        "SELECT coalesce(json_agg(json_build_object("
        "'schema', n.nspname, 'table', c.relname, 'name', con.conname, "
        "'constraint_type', con.contype, 'definition', pg_get_constraintdef(con.oid, true)) "
        f"ORDER BY n.nspname, c.relname, con.conname), '[]'::json) FROM pg_constraint con "
        "JOIN pg_class c ON c.oid=con.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind IN ('r', 'p') AND {schema_filter}",
        "SELECT coalesce(json_agg(json_build_object("
        "'schema', n.nspname, 'table', table_class.relname, 'name', index_class.relname, "
        "'definition', pg_get_indexdef(index_class.oid)) "
        f"ORDER BY n.nspname, table_class.relname, index_class.relname), '[]'::json) "
        "FROM pg_index index_info "
        "JOIN pg_class index_class ON index_class.oid=index_info.indexrelid "
        "JOIN pg_class table_class ON table_class.oid=index_info.indrelid "
        "JOIN pg_namespace n ON n.oid=table_class.relnamespace "
        f"WHERE table_class.relkind IN ('r', 'p') AND {schema_filter}",
        "SELECT coalesce(json_agg(json_build_object("
        "'schema', n.nspname, 'name', c.relname, 'definition', pg_get_viewdef(c.oid, true)) "
        f"ORDER BY n.nspname, c.relname), '[]'::json) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        f"WHERE c.relkind='v' AND {schema_filter}",
    )


def _catalog_rows(value: object, category: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise BackupError(f"invalid catalog {category} inventory")
    return [row for row in value if isinstance(row, dict)]


def _row_text(row: dict[str, object], key: str, category: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise BackupError(f"invalid catalog {category} {key}")
    return value


def _row_nullable(row: dict[str, object], category: str) -> bool:
    value = row.get("nullable")
    if type(value) is not bool:
        raise BackupError(f"invalid catalog {category} nullable")
    return value


def _row_default(row: dict[str, object], category: str) -> str | None:
    value = row.get("default_expression")
    if value is not None and not isinstance(value, str):
        raise BackupError(f"invalid catalog {category} default expression")
    return value


def catalog_inventory(query: Callable[[str], object]) -> SchemaInventory:
    """Build an application-schema inventory from catalog JSON query results."""
    tables, columns, constraints, indexes, views = (
        _catalog_rows(query(sql), category)
        for sql, category in zip(
            _catalog_queries(),
            ("tables", "columns", "constraints", "indexes", "views"),
            strict=True,
        )
    )
    return SchemaInventory(
        tables=tuple(
            sorted(
                TableInventory(_row_text(row, "schema", "tables"), _row_text(row, "name", "tables"))
                for row in tables
            )
        ),
        columns=tuple(
            sorted(
                ColumnInventory(
                    _row_text(row, "schema", "columns"),
                    _row_text(row, "table", "columns"),
                    _row_text(row, "name", "columns"),
                    _row_text(row, "data_type", "columns"),
                    _row_nullable(row, "columns"),
                    _row_default(row, "columns"),
                )
                for row in columns
            )
        ),
        constraints=tuple(
            sorted(
                ConstraintInventory(
                    _row_text(row, "schema", "constraints"),
                    _row_text(row, "table", "constraints"),
                    _row_text(row, "name", "constraints"),
                    _row_text(row, "constraint_type", "constraints"),
                    _row_text(row, "definition", "constraints"),
                )
                for row in constraints
            )
        ),
        indexes=tuple(
            sorted(
                IndexInventory(
                    _row_text(row, "schema", "indexes"),
                    _row_text(row, "table", "indexes"),
                    _row_text(row, "name", "indexes"),
                    _row_text(row, "definition", "indexes"),
                )
                for row in indexes
            )
        ),
        views=tuple(
            sorted(
                ViewInventory(
                    _row_text(row, "schema", "views"),
                    _row_text(row, "name", "views"),
                    _row_text(row, "definition", "views"),
                )
                for row in views
            )
        ),
    )


def _catalog_inventory(container_id: str, database: str) -> SchemaInventory:
    return catalog_inventory(lambda sql: _json_sql(container_id, sql, database))


def _inventory_category_difference[InventoryRecord](
    category: str,
    reference: tuple[InventoryRecord, ...],
    restored: tuple[InventoryRecord, ...],
    key: Callable[[InventoryRecord], tuple[str, ...]],
) -> str | None:
    reference_by_key = {key(item): item for item in reference}
    restored_by_key = {key(item): item for item in restored}
    missing = sorted(set(reference_by_key) - set(restored_by_key))
    if missing:
        return f"missing {category} {'.'.join(missing[0])}"
    extra = sorted(set(restored_by_key) - set(reference_by_key))
    if extra:
        return f"extra {category} {'.'.join(extra[0])}"
    for item_key in sorted(reference_by_key):
        if reference_by_key[item_key] != restored_by_key[item_key]:
            return (
                f"changed {category} {'.'.join(item_key)}: "
                f"reference={reference_by_key[item_key]!r}; restored={restored_by_key[item_key]!r}"
            )
    return None


def compare_schema_inventories(reference: SchemaInventory, restored: SchemaInventory) -> None:
    category_difference = _inventory_category_difference(
        "table", reference.tables, restored.tables, lambda item: (item.schema, item.name)
    )
    if category_difference is None:
        category_difference = _inventory_category_difference(
            "column", reference.columns, restored.columns, lambda item: (item.schema, item.table, item.name)
        )
    if category_difference is None:
        category_difference = _inventory_category_difference(
            "constraint",
            reference.constraints,
            restored.constraints,
            lambda item: (item.schema, item.table, item.name),
        )
    if category_difference is None:
        category_difference = _inventory_category_difference(
            "index", reference.indexes, restored.indexes, lambda item: (item.schema, item.table, item.name)
        )
    if category_difference is None:
        category_difference = _inventory_category_difference(
            "view", reference.views, restored.views, lambda item: (item.schema, item.name)
        )
    if category_difference is not None:
        raise BackupError(f"restore schema mismatch: {category_difference}")


def _table_count_key(table: TableInventory) -> str:
    return table.name if table.schema == "public" else f"{table.schema}.{table.name}"


def db_count_provenance(
    counts: dict[str, int], source: str, reference_table_keys: tuple[str, ...]
) -> dict[str, Any]:
    if source != "restored_dump":
        raise BackupError("acceptance counts must come from the restored dump")
    if not counts or any(not isinstance(table, str) or not table for table in counts):
        raise BackupError("restored table count set is incomplete")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise BackupError("invalid restored row count")
    if not reference_table_keys or reference_table_keys != tuple(sorted(set(reference_table_keys))):
        raise BackupError("reference table-key list is incomplete or unsorted")
    if set(counts) != set(reference_table_keys):
        raise BackupError("restored table counts do not match reference table-key list")
    return {
        "source": source,
        "reference_table_keys": list(reference_table_keys),
        "tables": dict(sorted(counts.items())),
    }


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def build_reference_inventory(
    database_name: str,
    create_database: Callable[[str], None],
    prepare_migration: Callable[[str], None],
    migrate_database: Callable[[str], None],
    inventory_database: Callable[[str], SchemaInventory],
    drop_database: Callable[[str], None],
) -> SchemaInventory:
    """Create, migrate, inspect, and always remove one disposable database."""
    if not database_name:
        raise BackupError("reference database name is empty")
    created = False
    try:
        create_database(database_name)
        created = True
        prepare_migration(database_name)
        migrate_database(database_name)
        return inventory_database(database_name)
    finally:
        if created:
            drop_database(database_name)


def _make_reference_migration_helper(
    project: str,
    database_name: str,
    network_name: str,
    image_id: str,
    credentials: Path,
) -> tuple[str, dict[str, Any]]:
    name = f"{project}-reference-migration"
    helper_id = _docker(
        [
            "create",
            "--name",
            name,
            "--label",
            f"com.docker.compose.project={project}",
            "--label",
            "com.docker.compose.service=reference-migration-helper",
            "--network",
            network_name,
            "--env-file",
            str(credentials),
            "--env",
            f"CROWLEY_REFERENCE_DATABASE={database_name}",
            "--entrypoint",
            "uv",
            image_id,
            "run",
            "--no-sync",
            "python",
            "-c",
            REFERENCE_MIGRATION_SCRIPT,
        ]
    ).stdout.strip()
    if not helper_id:
        raise BackupError("reference migration helper creation failed")
    info = _inspect_container(helper_id)
    if (
        not _labels_ok(info["labels"], project, "reference-migration-helper")
        or info["state"] != "created"
        or info["mounts"]
        or info["ports"] not in (None, {})
        or set(info["networks"]) != {network_name}
    ):
        raise BackupError("reference migration helper identity is not safe")
    return helper_id, info


def _reference_database_inventory(
    db_id: str,
    server_image_id: str,
    network_name: str,
    credentials: Path,
    project: str,
    register_helper: Callable[[dict[str, Any]], None],
    expected_migrations: list[dict[str, str]],
) -> SchemaInventory:
    reference_database = f"crowley_reference_{uuid.uuid4().hex[:16]}"
    migration_helper_id: str | None = None

    def create_database(database_name: str) -> None:
        _db_sql(db_id, f"CREATE DATABASE {_sql_identifier(database_name)}", "postgres")

    def prepare_migration(database_name: str) -> None:
        nonlocal migration_helper_id
        migration_helper_id, helper = _make_reference_migration_helper(
            project,
            database_name,
            network_name,
            server_image_id,
            credentials,
        )
        register_helper(helper)

    def migrate_database(database_name: str) -> None:
        if migration_helper_id is None:
            raise BackupError("reference migration helper was not created")
        result = _docker(["start", "-a", migration_helper_id], check=False)
        if result.returncode:
            raise BackupError("reference database migration failed")
        applied = _json_sql(
            db_id,
            "SELECT coalesce(json_agg(name ORDER BY name),'[]'::json) FROM schema_migrations",
            database_name,
        )
        expected_names = sorted(Path(item["path"]).name for item in expected_migrations)
        if applied != expected_names:
            raise BackupError("reference database migration manifest differs from packaged manifest")

    def inventory_database(database_name: str) -> SchemaInventory:
        return _catalog_inventory(db_id, database_name)

    def drop_database(database_name: str) -> None:
        _db_sql(db_id, f"DROP DATABASE {_sql_identifier(database_name)}", "postgres")

    return build_reference_inventory(
        reference_database,
        create_database,
        prepare_migration,
        migrate_database,
        inventory_database,
        drop_database,
    )


def _count_restored(
    container_id: str, reference: SchemaInventory, expected_migrations: list[dict[str, str]]
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    reference_table_keys = tuple(sorted(_table_count_key(table) for table in reference.tables))
    for table in reference.tables:
        key = _table_count_key(table)
        if key in counts:
            raise BackupError(f"duplicate application table count key: {key}")
        qualified = f"{_sql_identifier(table.schema)}.{_sql_identifier(table.name)}"
        counts[key] = int(_db_sql(container_id, f"SELECT count(*) FROM {qualified}"))
    count_proof = db_count_provenance(counts, "restored_dump", reference_table_keys)
    groups = {
        "scans": _json_sql(
            container_id,
            "SELECT coalesce(json_agg(x), '[]'::json) FROM ("
            "SELECT s.client_build, s.interface, h.key AS house_key, count(*)::bigint AS rows "
            "FROM scans s JOIN houses h ON h.id=s.house_id "
            "GROUP BY 1,2,3 ORDER BY 1,2,3) x",
        ),
        "market_sweeps": _json_sql(
            container_id,
            "SELECT coalesce(json_agg(x), '[]'::json) FROM ("
            "SELECT s.client_build, s.interface, h.key AS house_key, s.plan_hash, count(*)::bigint AS rows "
            "FROM market_sweeps s JOIN houses h ON h.id=s.house_id "
            "GROUP BY 1,2,3,4 ORDER BY 1,2,3,4) x",
        ),
    }
    houses = _json_sql(
        container_id,
        "SELECT json_build_object('count',count(*),'keys',coalesce(json_agg(key ORDER BY key),'[]'::json)) "
        "FROM houses",
    )
    plans = _json_sql(
        container_id,
        "SELECT json_build_object('count',count(*),'hashes',"
        "coalesce(json_agg(plan_hash ORDER BY plan_hash),'[]'::json)) "
        "FROM market_plans",
    )
    migrations = _json_sql(
        container_id, "SELECT coalesce(json_agg(name ORDER BY name),'[]'::json) FROM schema_migrations"
    )
    expected_names = sorted(Path(item["path"]).name for item in expected_migrations)
    if migrations != expected_names:
        raise BackupError("restored migration names do not match snapshot source")
    return {
        "provenance": count_proof,
        "groups": groups,
        "houses": houses,
        "market_plans": plans,
        "migrations": {"expected": expected_names, "applied": migrations, "match": True},
    }


def _raw_references(
    db_id: str, archive: Path, manifest: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    members = {member["path"]: member for member in manifest["raw_archive"]["members"]}
    by_path: dict[str, bytes] = {}
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            stream = tar.extractfile(member)
            if stream is None:
                raise BackupError("raw archive member vanished")
            by_path[member.name] = stream.read()
    scans = _json_sql(
        db_id,
        "SELECT coalesce(json_agg(x), '[]'::json) FROM ("
        "SELECT id, scan_uid, raw_path FROM scans ORDER BY id) x",
    )
    if scans:
        raise BackupError("unsupported_legacy_reference")
    rows = _json_sql(
        db_id,
        "SELECT coalesce(json_agg(x), '[]'::json) FROM ("
        "SELECT s.id, s.sweep_uid, h.key AS house_key, s.plan_hash, s.client_build, "
        "s.interface, s.content_digest, s.raw_path, p.plan_json "
        "FROM market_sweeps s JOIN houses h ON h.id=s.house_id "
        "JOIN market_plans p ON p.plan_hash=s.plan_hash ORDER BY s.id) x",
    )
    plans = {
        plan["plan_hash"]: plan["plan_json"]
        for plan in _json_sql(
            db_id,
            "SELECT coalesce(json_agg(x), '[]'::json) FROM ("
            "SELECT plan_hash, plan_json FROM market_plans ORDER BY plan_hash) x",
        )
    }
    references: list[dict[str, Any]] = []
    referenced: set[str] = set()
    for row in rows:
        path = raw_reference_path(row["raw_path"], set(members))
        if path in referenced:
            raise BackupError("duplicate format-2 archive reference")
        referenced.add(path)
        member = members[path]
        proof_row = {**row, "inventory_path": path}
        logical = validate_format2_member(by_path[path], proof_row, plans)
        references.append(
            {
                "source_table": "market_sweeps",
                "row_id": row["id"],
                "inventory_path": path,
                "member_sha256": member["sha256"],
                "validation": "valid_format2",
                "logical": logical,
            }
        )
    orphan_rows = []
    for path, member in members.items():
        if path in referenced:
            continue
        if path.startswith("raw/format2/") and path.endswith(".json.gz"):
            try:
                data = by_path[path]
                env = strict_json(gzip.decompress(data))
                from crowley.core.scanformat import validate_v2_envelope

                validated = validate_v2_envelope(env)
                from crowley.server.ingest import _logical_digest

                digest = _logical_digest(env, validated["decoded"])
                expected = f"raw/format2/{digest[:2]}/{digest}.json.gz"
                is_valid = path == expected
                orphan_rows.append(
                    {
                        "path": path,
                        "sha256": member["sha256"],
                        "validation": "valid_format2" if is_valid else "unvalidated",
                        "warning": None if is_valid else "digest path mismatch",
                    }
                )
            except Exception:
                orphan_rows.append(
                    {
                        "path": path,
                        "sha256": member["sha256"],
                        "validation": "unvalidated",
                        "warning": "format-2 orphan failed independent validation",
                    }
                )
        else:
            orphan_rows.append(
                {
                    "path": path,
                    "sha256": member["sha256"],
                    "validation": "unvalidated",
                    "warning": "not a format-2 archive object",
                }
            )
    return references, orphan_rows


def _write_override(path: Path, project: str) -> None:
    text = """services:
  db:
    image: ${MARKET_BACKUP_DB_IMAGE:?}
    restart: "no"
    ports: !reset []
    environment:
      POSTGRES_USER: crowley
      POSTGRES_PASSWORD: ${CROWLEY_DB_PASSWORD:?}
      POSTGRES_DB: postgres
  server:
    image: ${MARKET_BACKUP_SERVER_IMAGE:?}
    build: !reset null
    restart: "no"
    ports: !reset []
    environment:
      CROWLEY_DATABASE_URL: postgresql://crowley:${CROWLEY_DB_PASSWORD:?}@db:5432/crowley
      CROWLEY_API_TOKEN: ${CROWLEY_API_TOKEN:?}
      CROWLEY_DATA_DIR: /data
      CROWLEY_HOST: 0.0.0.0
      CROWLEY_PORT: "8080"
networks:
  default:
    internal: true
"""
    _set_private(path, text.encode("utf-8"))


def _target_image_migrations(image_id: str) -> list[dict[str, str]]:
    script = "\n".join(
        (
            "import glob,hashlib,json,os",
            "d='/app/src/crowley/server/migrations'",
            "rows=[]",
            "for path in sorted(glob.glob(d+'/*.sql')):",
            " with open(path,'rb') as handle:",
            "  digest=hashlib.sha256(handle.read()).hexdigest()",
            " rows.append({'path':'src/crowley/server/migrations/'+os.path.basename(path),'sha256':digest})",
            "print(json.dumps(rows,sort_keys=True,separators=(',',':')))",
        )
    )
    result = _docker(
        ["run", "--rm", "--network", "none", "--entrypoint", "/usr/local/bin/python", image_id, "-c", script]
    ).stdout.strip()
    try:
        return json.loads(result)
    except json.JSONDecodeError as exc:
        raise BackupError("recorded server image migration inspection failed") from exc


def validate_source_compatibility(recorded: dict[str, Any], current: dict[str, Any]) -> None:
    """Check restore compatibility while leaving source revision fields as provenance."""
    if recorded["compose_file_sha256"] != current["compose_file_sha256"]:
        raise BackupError("snapshot Compose file no longer matches")
    if recorded["migrations"] != current["migrations"]:
        raise BackupError("snapshot migration files no longer match")
    for field in ("postgres_server_version", "timescaledb_extension_version"):
        if recorded[field] != current[field]:
            raise BackupError("snapshot source database versions no longer match")
    for key in ("database", "server"):
        expected, actual = recorded["containers"][key], current["containers"][key]
        if expected["image_id"] != actual["image_id"] or expected["repo_digests"] != actual["repo_digests"]:
            raise BackupError("snapshot source image identity no longer matches")
    if current["server_image_migrations"] != recorded["migrations"]:
        raise BackupError("recorded server image migrations differ from manifest")


def _safe_failure_stage(error: Exception) -> str | None:
    if not isinstance(error, BackupError):
        return None
    stage = str(error)
    return (
        stage
        if stage
        in {
            "identity_before_database",
            "database_readiness",
            "start_disposable_database",
            "create_database",
            "identity_after_database",
            "create_extension",
            "pg_restore_single_job_no_owner",
        }
        else None
    )


def _safe_failure_reason(error: Exception) -> str:
    if not isinstance(error, BackupError):
        return "unexpected_error"
    return {
        "source container identity changed during restore": "source_container_changed",
        "source volume identity changed during restore": "source_volume_changed",
        "source network identity changed during restore": "source_network_changed",
        "disposable project resource identities are ambiguous": "project_inventory_changed",
        "disposable container set is incomplete or ambiguous": "container_set_incomplete",
        "disposable container identity changed": "container_identity_changed",
        "disposable container network attachment changed": "network_attachment_changed",
        "disposable container identity is not fresh and isolated": "target_not_disjoint",
        "disposable database did not become ready": "database_not_ready",
        "bounded backup operation failed": "local_operation_failed",
    }.get(str(error), "safe_operation_failed")


def _readiness_status(returncode: int) -> str:
    return {
        0: "accepting_connections",
        1: "rejecting_connections",
        2: "no_response",
        3: "invalid_probe_parameters",
    }.get(returncode, "probe_failed")


def _disposable_setup_metadata(container_id: str) -> dict[str, Any]:
    postgres = _json_sql(
        container_id,
        "SELECT json_build_object("
        "'crowley_database_exists', EXISTS (SELECT 1 FROM pg_database WHERE datname='crowley'),"
        "'timescaledb_available', EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'),"
        "'role_superuser', r.rolsuper, 'role_createdb', r.rolcreatedb,"
        "'timescaledb_preloaded', EXISTS ("
        "SELECT 1 FROM unnest(string_to_array(current_setting('shared_preload_libraries'), ',')) AS lib "
        "WHERE btrim(lib)='timescaledb'), 'server_version', current_setting('server_version')) "
        "FROM pg_roles r WHERE r.rolname=current_user",
        "postgres",
    )
    expected = {
        "crowley_database_exists",
        "timescaledb_available",
        "role_superuser",
        "role_createdb",
        "timescaledb_preloaded",
        "server_version",
    }
    if not isinstance(postgres, dict) or set(postgres) != expected:
        raise BackupError("disposable database metadata is incomplete")
    result = {"postgres": postgres, "crowley_extension_enabled": None}
    if postgres["crowley_database_exists"]:
        extension = _json_sql(
            container_id,
            "SELECT json_build_object('timescaledb_enabled', "
            "EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb'))",
            "crowley",
        )
        if not isinstance(extension, dict) or set(extension) != {"timescaledb_enabled"}:
            raise BackupError("disposable extension metadata is incomplete")
        result["crowley_extension_enabled"] = extension["timescaledb_enabled"]
    if any(type(postgres[field]) is not bool for field in expected - {"server_version"}):
        raise BackupError("disposable database metadata has invalid boolean fields")
    if not isinstance(postgres["server_version"], str):
        raise BackupError("disposable database metadata has invalid version")
    return result


def _disposable_restore_metadata(container_id: str) -> dict[str, Any]:
    result = _json_sql(
        container_id,
        "SELECT json_build_object("
        "'crowley_database_exists', EXISTS (SELECT 1 FROM pg_database WHERE datname='crowley'),"
        "'timescaledb_enabled', EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb'),"
        "'public_schema_exists', EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='public'),"
        "'schema_migrations_exists', to_regclass('public.schema_migrations') IS NOT NULL,"
        "'public_relation_count', (SELECT count(*) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','f'))) ",
        "crowley",
    )
    expected = {
        "crowley_database_exists",
        "timescaledb_enabled",
        "public_schema_exists",
        "schema_migrations_exists",
        "public_relation_count",
    }
    if not isinstance(result, dict) or set(result) != expected:
        raise BackupError("disposable restore metadata is incomplete")
    for field in expected - {"public_relation_count"}:
        if type(result[field]) is not bool:
            raise BackupError("disposable restore metadata has invalid flags")
    _integer(result["public_relation_count"], "disposable public relation count")
    return result


def _target_version(db_id: str) -> tuple[str, str]:
    sql = (
        "SELECT current_setting('server_version'), extversion "
        "FROM pg_extension JOIN pg_namespace n ON n.oid=extnamespace "
        "WHERE extname='timescaledb'"
    )
    text = _db_sql(db_id, sql)
    parts = text.split("|")
    if len(parts) != 2:
        raise BackupError("restored target version inspection failed")
    return parts[0], parts[1]


def _make_helper(repo: Path, project: str, raw_volume: str, image_id: str) -> tuple[str, dict[str, Any]]:
    name = f"{project}-raw-restore"
    script = r"""import os,sys,tarfile
root='/data'
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as tar:
    for member in tar:
        name=member.name
        parts=name.split('/')
        invalid_path = not name or name.startswith('/') or '\\' in name or '\x00' in name
        invalid_parts = any(part in ('', '.', '..') for part in parts)
        if invalid_path or invalid_parts or not member.isfile():
            raise SystemExit(51)
        dest=os.path.join(root,*parts)
        cur=root
        for part in parts[:-1]:
            cur=os.path.join(cur,part)
            try: os.mkdir(cur,0o700)
            except FileExistsError:
                if not os.path.isdir(cur) or os.path.islink(cur): raise SystemExit(52)
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as out:
            src=tar.extractfile(member)
            if src is None: raise SystemExit(53)
            while True:
                block=src.read(1024*1024)
                if not block: break
                out.write(block)
            out.flush(); os.fsync(out.fileno())
"""
    args = [
        "create",
        "--name",
        name,
        "--label",
        f"com.docker.compose.project={project}",
        "--label",
        "com.docker.compose.service=raw-restore-helper",
        "--network",
        "none",
        "--interactive",
        "--attach",
        "stdin",
        "--mount",
        f"type=volume,src={raw_volume},dst=/data",
        "--entrypoint",
        "/usr/local/bin/python",
        image_id,
        "-c",
        script,
    ]
    helper_id = _docker(args).stdout.strip()
    if not helper_id:
        raise BackupError("raw extraction helper creation failed")
    info = _inspect_container(helper_id)
    if not _labels_ok(info["labels"], project, "raw-restore-helper") or info["state"] != "created":
        raise BackupError("raw extraction helper identity is not safe")
    mounts = [m for m in info["mounts"] if m["Destination"] == "/data" and m["Type"] == "volume"]
    if len(mounts) != 1 or mounts[0]["Name"] != raw_volume or not mounts[0]["RW"]:
        raise BackupError("raw extraction helper mount is not the disposable raw volume")
    return helper_id, info


def _extract_raw(
    repo: Path,
    project: str,
    raw_volume: str,
    image_id: str,
    archive: Path,
    source: dict[str, Any],
    live: dict[str, Any],
    target: dict[str, Any],
    override: Path,
    credentials: Path,
) -> tuple[str, dict[str, Any]]:
    helper_id, info = _make_helper(repo, project, raw_volume, image_id)
    if info["id"] in live["containers"] or info["id"] in {c["id"] for c in source["containers"].values()}:
        raise BackupError("raw helper container identity collides with source")
    now = _inspect_container(helper_id)
    if now["id"] != info["id"] or not _labels_ok(now["labels"], project, "raw-restore-helper"):
        raise BackupError("raw helper ownership changed before write")
    _guard_current(repo, project, override, credentials, source, target, [info])
    with archive.open("rb") as source_handle:
        code = _stream_to_process(["docker", "start", "-ai", helper_id], source_handle)
    if code:
        raise BackupError("raw archive extraction failed")
    return helper_id, info


def _verify_raw_volume(
    repo: Path,
    project: str,
    raw_volume: str,
    image_id: str,
    manifest: dict[str, Any],
    live: dict[str, Any],
    source: dict[str, Any],
    target: dict[str, Any],
    override: Path,
    credentials: Path,
    helpers: list[dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    script = r"""import hashlib,json,os,stat
root='/data'; found=[]; stack=[(root,'')]
while stack:
    absolute,relative=stack.pop()
    with os.scandir(absolute) as it: entries=sorted(list(it),key=lambda e:e.name.encode('utf-8'),reverse=True)
    for entry in entries:
        path=(relative+'/'+entry.name).lstrip('/'); info=entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode): raise SystemExit(61)
        if stat.S_ISDIR(info.st_mode): stack.append((entry.path,path)); continue
        if not stat.S_ISREG(info.st_mode): raise SystemExit(62)
        digest=hashlib.sha256()
        with open(entry.path,'rb') as f:
            while True:
                block=f.read(1024*1024)
                if not block: break
                digest.update(block)
        found.append({'path':path,'size_bytes':info.st_size,'sha256':digest.hexdigest()})
print(json.dumps(sorted(found,key=lambda x:x['path'].encode('utf-8')),sort_keys=True,separators=(',',':')))
"""
    name = f"{project}-raw-verify-helper"
    helper_id = _docker(
        [
            "create",
            "--name",
            name,
            "--label",
            f"com.docker.compose.project={project}",
            "--label",
            "com.docker.compose.service=raw-verify-helper",
            "--network",
            "none",
            "--attach",
            "stdout",
            "--mount",
            f"type=volume,src={raw_volume},dst=/data,readonly",
            "--entrypoint",
            "/usr/local/bin/python",
            image_id,
            "-c",
            script,
        ]
    ).stdout.strip()
    if not helper_id:
        raise BackupError("raw verification helper creation failed")
    info = _inspect_container(helper_id)
    if (
        not _labels_ok(info["labels"], project, "raw-verify-helper")
        or info["state"] != "created"
        or info["id"] in live["containers"]
        or info["id"] in {c["id"] for c in source["containers"].values()}
    ):
        raise BackupError("raw verification helper identity is unsafe")
    mounts = [m for m in info["mounts"] if m["Destination"] == "/data" and m["Type"] == "volume"]
    if len(mounts) != 1 or mounts[0]["Name"] != raw_volume or mounts[0]["RW"]:
        raise BackupError("raw verification helper mount is not read-only target volume")
    _guard_current(repo, project, override, credentials, source, target, [*helpers, info])
    proc = _docker(["start", "-a", helper_id], binary=True, check=False)
    if proc.returncode:
        raise BackupError("raw volume inventory verification failed")
    try:
        observed = json.loads(proc.stdout.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("raw volume inventory result is invalid") from exc
    if observed != manifest["raw_archive"]["members"]:
        raise BackupError("extracted raw volume differs from archive inventory")
    return helper_id, info


def _probe_api(server_id: str, authenticated: bool) -> bool:
    if authenticated:
        script = "\n".join(
            (
                "import os,urllib.request",
                "headers={'Authorization':'Bearer '+os.environ['CROWLEY_API_TOKEN']}",
                "request=urllib.request.Request('http://server:8080/v1/houses',headers=headers)",
                "response=urllib.request.urlopen(request,timeout=10)",
                "print(response.status)",
            )
        )
    else:
        script = "\n".join(
            (
                "import urllib.request",
                "response=urllib.request.urlopen('http://server:8080/healthz',timeout=10)",
                "print(response.status)",
            )
        )
    result = _docker(["exec", server_id, "/usr/local/bin/python", "-c", script], check=False)
    return result.returncode == 0 and result.stdout.strip() == "200"


def _safe_target_receipt(target: dict[str, Any], project: str) -> dict[str, Any]:
    return {
        "project": project,
        "services": {
            service: {
                "id": item["id"],
                "labels": item["labels"],
                "image_id": item["image_id"],
                "mounts": _receipt_mounts(item["mounts"]),
                "networks": _receipt_network_membership(item["networks"]),
            }
            for service, item in target["containers"].items()
        },
        "volumes": target["volumes"],
        "networks": target["networks"],
        "helpers": [],
    }


def _receipt_mounts(mounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Type": item["Type"],
            "Name": item.get("Name", ""),
            "Source": item["Source"],
            "Destination": item["Destination"],
            "RW": item["RW"],
        }
        for item in mounts
    ]


def _receipt_network_membership(networks: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: {"NetworkID": attachment.get("NetworkID")} for name, attachment in networks.items()}


def _safe_helper_receipt(helper: dict[str, Any], service: str) -> dict[str, Any]:
    return {
        "id": helper["id"],
        "service": service,
        "labels": helper["labels"],
        "image_id": helper["image_id"],
        "mounts": _receipt_mounts(helper["mounts"]),
        "networks": _receipt_network_membership(helper["networks"]),
    }


def _receipt_labels(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(label, str) for key, label in value.items()
    ):
        raise BackupError(f"invalid {where} labels")
    return value


def _receipt_mount_inventory(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise BackupError(f"invalid {where} mounts")
    for mount in value:
        item = _exact_keys(mount, {"Type", "Name", "Source", "Destination", "RW"}, f"{where} mount")
        for key in ("Type", "Name", "Source", "Destination"):
            _string(item[key], f"{where} mount {key}", empty=True)
        if not isinstance(item["RW"], bool):
            raise BackupError(f"invalid {where} mount mode")
    return value


def _receipt_networks(value: Any, where: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise BackupError(f"invalid {where} networks")
    for name, attachment in value.items():
        _string(name, f"{where} network name")
        item = _exact_keys(attachment, {"NetworkID"}, f"{where} network attachment")
        if item["NetworkID"] is not None:
            _string(item["NetworkID"], f"{where} network ID", empty=True)
    return value


def _receipt_migrations(value: Any, where: str) -> dict[str, Any]:
    item = _exact_keys(value, {"expected", "applied", "match"}, where)
    for key in ("expected", "applied"):
        if not isinstance(item[key], list) or any(
            not isinstance(name, str) or not name for name in item[key]
        ):
            raise BackupError(f"invalid {where} {key}")
        if item[key] != sorted(set(item[key])):
            raise BackupError(f"unsorted or duplicate {where} {key}")
    if not isinstance(item["match"], bool):
        raise BackupError(f"invalid {where} match")
    return item


def _receipt_counts(value: Any) -> dict[str, Any]:
    counts = _exact_keys(value, {"provenance", "groups", "houses", "market_plans", "migrations"}, "counts")
    proof = _exact_keys(
        counts["provenance"], {"source", "reference_table_keys", "tables"}, "count provenance"
    )
    if proof["source"] != "restored_dump":
        raise BackupError("receipt counts are not restored-dump-derived")
    reference_table_keys = proof["reference_table_keys"]
    if not isinstance(reference_table_keys, list) or any(
        not isinstance(table, str) or not table for table in reference_table_keys
    ):
        raise BackupError("invalid reference table-key list")
    if reference_table_keys != sorted(set(reference_table_keys)):
        raise BackupError("reference table-key list is unsorted or duplicated")
    table_counts = proof["tables"]
    if (
        not isinstance(table_counts, dict)
        or not table_counts
        or any(not isinstance(table, str) or not table for table in table_counts)
    ):
        raise BackupError("invalid receipt table counts")
    required_tables = {"houses", "market_sweeps", "market_plans", "scans", "schema_migrations"}
    missing_tables = sorted(required_tables - set(table_counts))
    if missing_tables:
        raise BackupError(f"invalid receipt table counts: missing {', '.join(missing_tables)}")
    if set(table_counts) != set(reference_table_keys):
        raise BackupError("receipt table counts differ from reference table-key list")
    for table, count in table_counts.items():
        _integer(count, f"{table} row count")

    groups = _exact_keys(counts["groups"], {"scans", "market_sweeps"}, "group counts")
    group_fields = {
        "scans": {"client_build", "interface", "house_key", "rows"},
        "market_sweeps": {"client_build", "interface", "house_key", "plan_hash", "rows"},
    }
    for table, rows in groups.items():
        if not isinstance(rows, list):
            raise BackupError(f"invalid {table} groups")
        keys: set[tuple[Any, ...]] = set()
        for row in rows:
            item = _exact_keys(row, group_fields[table], f"{table} group")
            _string(item["client_build"], f"{table} client build")
            if type(item["interface"]) is not int or item["interface"] < 0:
                raise BackupError(f"invalid {table} interface")
            _string(item["house_key"], f"{table} house key")
            if table == "market_sweeps":
                _sha(item["plan_hash"], "market sweep plan hash")
            _integer(item["rows"], f"{table} group row count")
            if item["rows"] == 0:
                raise BackupError(f"invalid empty {table} group")
            keys.add(
                tuple(
                    item[key]
                    for key in ("client_build", "interface", "house_key", "plan_hash")
                    if key in item
                )
            )
        if len(keys) != len(rows):
            raise BackupError(f"duplicate {table} group")

    houses = _exact_keys(counts["houses"], {"count", "keys"}, "house counts")
    _integer(houses["count"], "houses count")
    if not isinstance(houses["keys"], list) or any(
        not isinstance(key, str) or not key for key in houses["keys"]
    ):
        raise BackupError("invalid house keys")
    if houses["keys"] != sorted(set(houses["keys"])) or houses["count"] != len(houses["keys"]):
        raise BackupError("house count and keys differ")

    plans = _exact_keys(counts["market_plans"], {"count", "hashes"}, "market plan counts")
    _integer(plans["count"], "market plans count")
    if not isinstance(plans["hashes"], list):
        raise BackupError("invalid market plan hashes")
    for plan_hash in plans["hashes"]:
        _sha(plan_hash, "market plan hash")
    if plans["hashes"] != sorted(set(plans["hashes"])) or plans["count"] != len(plans["hashes"]):
        raise BackupError("market plan count and hashes differ")
    migrations = _receipt_migrations(counts["migrations"], "count migrations")
    if table_counts["schema_migrations"] != len(migrations["applied"]):
        raise BackupError("schema migration count and evidence differ")
    if migrations["match"] and migrations["expected"] != migrations["applied"]:
        raise BackupError("count migration match is inconsistent")
    if table_counts["houses"] != houses["count"]:
        raise BackupError("house count differs from restored table count")
    if table_counts["market_plans"] != plans["count"]:
        raise BackupError("market plan count differs from restored table count")
    if sum(row["rows"] for row in groups["scans"]) != table_counts["scans"]:
        raise BackupError("scan groups differ from restored table count")
    if sum(row["rows"] for row in groups["market_sweeps"]) != table_counts["market_sweeps"]:
        raise BackupError("market sweep groups differ from restored table count")
    return counts


def _receipt_target(value: Any) -> dict[str, Any]:
    target = _exact_keys(value, {"project", "services", "volumes", "networks", "helpers"}, "target")
    if target["project"] is not None:
        _string(target["project"], "target project")
    services = target["services"]
    if not isinstance(services, dict) or set(services) not in (set(), {"db", "server"}):
        raise BackupError("invalid target services")
    for name, service in services.items():
        item = _exact_keys(service, {"id", "labels", "image_id", "mounts", "networks"}, "target service")
        _string(item["id"], f"{name} container ID")
        _receipt_labels(item["labels"], f"{name} service")
        _string(item["image_id"], f"{name} image ID")
        _receipt_mount_inventory(item["mounts"], f"{name} service")
        _receipt_networks(item["networks"], f"{name} service")
    if not isinstance(target["volumes"], list) or not isinstance(target["networks"], list):
        raise BackupError("invalid target resource inventory")
    for volume in target["volumes"]:
        item = _exact_keys(volume, {"name", "mountpoint", "labels"}, "target volume")
        _string(item["name"], "target volume name")
        _string(item["mountpoint"], "target volume mountpoint")
        _receipt_labels(item["labels"], "target volume")
    for network in target["networks"]:
        item = _exact_keys(network, {"id", "name", "labels"}, "target network")
        _string(item["id"], "target network ID")
        _string(item["name"], "target network name")
        _receipt_labels(item["labels"], "target network")
    if not isinstance(target["helpers"], list):
        raise BackupError("invalid target helpers")
    for helper in target["helpers"]:
        item = _exact_keys(
            helper, {"id", "service", "labels", "image_id", "mounts", "networks"}, "target helper"
        )
        _string(item["id"], "target helper ID")
        _string(item["service"], "target helper service")
        _receipt_labels(item["labels"], "target helper")
        _string(item["image_id"], "target helper image ID")
        _receipt_mount_inventory(item["mounts"], "target helper")
        _receipt_networks(item["networks"], "target helper")
    return target


def _validate_verified_target(target: dict[str, Any]) -> set[str]:
    project = target["project"]
    if not isinstance(project, str) or not re.fullmatch(r"crowley-drill-[0-9a-f]{16}", project):
        raise BackupError("verified receipt target project is incomplete")
    services = target["services"]
    if set(services) != {"db", "server"}:
        raise BackupError("verified receipt service inventory is incomplete")
    service_ids: set[str] = set()
    for name, destination in (("db", "/var/lib/postgresql/data"), ("server", "/data")):
        service = services[name]
        if (
            service["id"] in service_ids
            or service["labels"].get("com.docker.compose.project") != project
            or service["labels"].get("com.docker.compose.service") != name
            or len(service["mounts"]) != 1
        ):
            raise BackupError("verified receipt service identity is incomplete")
        service_ids.add(service["id"])
        mount = service["mounts"][0]
        if mount["Type"] != "volume" or mount["Destination"] != destination or mount["RW"] is not True:
            raise BackupError("verified receipt service mount is invalid")
    if len(target["volumes"]) != 2 or len(target["networks"]) != 1:
        raise BackupError("verified receipt target resource inventory is incomplete")
    volume_names = [volume["name"] for volume in target["volumes"]]
    if len(set(volume_names)) != 2:
        raise BackupError("verified receipt target volumes are not distinct")
    volume_by_name = {volume["name"]: volume for volume in target["volumes"]}
    for volume in target["volumes"]:
        if (
            not volume["name"].startswith(project + "_")
            or not volume["mountpoint"].startswith("/")
            or volume["labels"].get("com.docker.compose.project") != project
        ):
            raise BackupError("verified receipt target volume ownership is invalid")
    mounted_names = set()
    for name in ("db", "server"):
        mount = services[name]["mounts"][0]
        volume = volume_by_name.get(mount["Name"])
        expected_volume_key = "crowley-db" if name == "db" else "crowley-raw"
        if (
            volume is None
            or mount["Source"] != volume["mountpoint"]
            or volume["labels"].get("com.docker.compose.volume") != expected_volume_key
        ):
            raise BackupError("verified receipt target mount volume is inconsistent")
        mounted_names.add(mount["Name"])
    if mounted_names != set(volume_names):
        raise BackupError("verified receipt database and raw volumes are shared")
    network = target["networks"][0]
    if (
        not network["name"].startswith(project + "_")
        or network["labels"].get("com.docker.compose.project") != project
    ):
        raise BackupError("verified receipt target network ownership is invalid")
    network_id = network["id"]
    for service in services.values():
        if service["networks"] != {network["name"]: {"NetworkID": network_id}}:
            raise BackupError("verified receipt service network identity is incomplete")

    helper_ids: set[str] = set()
    helper_names = {helper["service"] for helper in target["helpers"]}
    expected_helper_names = {
        "raw-restore-helper",
        "raw-verify-helper",
        "reference-migration-helper",
    }
    if helper_names != expected_helper_names or len(target["helpers"]) != len(expected_helper_names):
        raise BackupError("verified receipt helper inventory is incomplete")
    raw_volume_name = services["server"]["mounts"][0]["Name"]
    raw_volume = volume_by_name[raw_volume_name]
    for helper in target["helpers"]:
        name = helper["service"]
        if (
            helper["id"] in service_ids | helper_ids
            or helper["labels"].get("com.docker.compose.project") != project
            or helper["labels"].get("com.docker.compose.service") != name
            or helper["image_id"] != services["server"]["image_id"]
        ):
            raise BackupError("verified receipt helper identity is incomplete")
        helper_ids.add(helper["id"])
        if name == "reference-migration-helper":
            if helper["mounts"] or helper["networks"] != {network["name"]: {"NetworkID": network_id}}:
                raise BackupError("verified receipt migration helper inventory is invalid")
        else:
            if len(helper["mounts"]) != 1 or helper["networks"].keys() != {"none"}:
                raise BackupError("verified receipt helper inventory is incomplete")
            mount = helper["mounts"][0]
            expected_rw = name == "raw-restore-helper"
            if (
                mount["Type"] != "volume"
                or mount["Name"] != raw_volume_name
                or mount["Source"] != raw_volume["mountpoint"]
                or mount["Destination"] != "/data"
                or mount["RW"] is not expected_rw
            ):
                raise BackupError("verified receipt helper mount is invalid")
            _string(helper["networks"]["none"]["NetworkID"], "verified helper none network ID")
    return service_ids | helper_ids | set(volume_names) | {network_id}


def validate_receipt(receipt: Any) -> dict[str, Any]:
    if isinstance(receipt, str):
        receipt = strict_json(receipt.encode("utf-8"))
    elif isinstance(receipt, bytes):
        receipt = strict_json(receipt)
    expected = {
        "schema",
        "schema_version",
        "backup_id",
        "manifest_sha256",
        "result",
        "started_at_utc",
        "ended_at_utc",
        "target_versions",
        "counts",
        "migrations",
        "references",
        "orphans",
        "api",
        "operations",
        "cleanup",
        "target",
    }
    result = _exact_keys(receipt, expected, "receipt")
    if (
        result["schema"] != RECEIPT_SCHEMA
        or type(result["schema_version"]) is not int
        or result["schema_version"] != 1
    ):
        raise BackupError("invalid receipt schema")
    backup_id = _string(result["backup_id"], "receipt backup_id")
    if not BACKUP_ID_RE.fullmatch(backup_id):
        raise BackupError("invalid receipt backup ID")
    _sha(result["manifest_sha256"], "receipt manifest")
    if not isinstance(result["result"], str) or result["result"] not in {
        "restore_verified",
        "failed",
        "blocked",
    }:
        raise BackupError("invalid receipt result")
    timestamps = []
    for key in ("started_at_utc", "ended_at_utc"):
        value = _string(result[key], key)
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BackupError(f"invalid {key}") from exc
        if not value.endswith("Z") or parsed.utcoffset() != dt.timedelta(0):
            raise BackupError(f"invalid {key}")
        timestamps.append(parsed)
    if timestamps[1] < timestamps[0]:
        raise BackupError("receipt end precedes start")

    versions = _exact_keys(
        result["target_versions"],
        {"postgres_server_version", "timescaledb_extension_version", "match"},
        "target versions",
    )
    for key in ("postgres_server_version", "timescaledb_extension_version"):
        if versions[key] is not None:
            _string(versions[key], key)
    if not isinstance(versions["match"], bool):
        raise BackupError("invalid target version match")

    counts = result["counts"]
    if counts != {}:
        _receipt_counts(counts)
    migrations = _receipt_migrations(result["migrations"], "migrations")

    if not isinstance(result["references"], list) or not isinstance(result["orphans"], list):
        raise BackupError("invalid receipt raw classifications")
    reference_paths: set[str] = set()
    reference_ids: set[int] = set()
    for reference in result["references"]:
        ref = _exact_keys(
            reference,
            {"source_table", "row_id", "inventory_path", "member_sha256", "validation", "logical"},
            "raw reference",
        )
        if ref["source_table"] != "market_sweeps":
            raise BackupError("invalid raw reference source")
        _integer(ref["row_id"], "reference row id")
        _path(ref["inventory_path"], "reference inventory path")
        _sha(ref["member_sha256"], "reference member")
        if not isinstance(ref["validation"], str) or ref["validation"] != "valid_format2":
            raise BackupError("invalid raw reference result")
        logical = _exact_keys(
            ref["logical"], {"logical_digest", "sweep_uid", "house_key", "plan_hash"}, "logical reference"
        )
        _sha(logical["logical_digest"], "logical reference digest")
        _string(logical["sweep_uid"], "logical sweep UID")
        _string(logical["house_key"], "logical house key")
        _sha(logical["plan_hash"], "logical plan hash")
        if ref["inventory_path"] != (
            f"raw/format2/{logical['logical_digest'][:2]}/{logical['logical_digest']}.json.gz"
        ):
            raise BackupError("logical reference digest and inventory path differ")
        if ref["inventory_path"] in reference_paths or ref["row_id"] in reference_ids:
            raise BackupError("duplicate logical reference evidence")
        reference_paths.add(ref["inventory_path"])
        reference_ids.add(ref["row_id"])
    orphan_paths: set[str] = set()
    for orphan in result["orphans"]:
        item = _exact_keys(orphan, {"path", "sha256", "validation", "warning"}, "raw orphan")
        _path(item["path"], "orphan path")
        _sha(item["sha256"], "orphan member")
        if (
            not isinstance(item["validation"], str)
            or item["validation"]
            not in {
                "valid_format2",
                "unvalidated",
            }
            or (item["warning"] is not None and not isinstance(item["warning"], str))
        ):
            raise BackupError("invalid orphan validation result")
        if (item["validation"] == "valid_format2") != (item["warning"] is None):
            raise BackupError("orphan warning and validation disagree")
        if item["path"] in orphan_paths or item["path"] in reference_paths:
            raise BackupError("duplicate raw inventory classification")
        orphan_paths.add(item["path"])

    api = _exact_keys(result["api"], {"health_probe", "authenticated_read"}, "API checks")
    if any(not isinstance(value, bool) for value in api.values()):
        raise BackupError("invalid API check result")
    if not isinstance(result["operations"], list) or [
        entry.get("name") if isinstance(entry, dict) else None for entry in result["operations"]
    ] != list(OPERATIONS):
        raise BackupError("invalid receipt operations")
    for entry in result["operations"]:
        operation = _exact_keys(entry, {"name", "state"}, "operation")
        if not isinstance(operation["state"], str) or operation["state"] not in {
            "passed",
            "failed",
            "blocked",
            "not_run",
        }:
            raise BackupError("invalid operation state")
    cleanup = _exact_keys(result["cleanup"], {"verified", "removed"}, "cleanup")
    if not isinstance(cleanup["verified"], bool) or not isinstance(cleanup["removed"], list):
        raise BackupError("invalid cleanup result")
    for value in cleanup["removed"]:
        _string(value, "removed resource")
        prefix, separator, identity = value.partition(":")
        if not separator or prefix not in {"container", "volume", "network"} or not identity:
            raise BackupError("invalid removed resource identity")
    if len(cleanup["removed"]) != len(set(cleanup["removed"])):
        raise BackupError("duplicate removed resource identity")

    target = _receipt_target(result["target"])
    if result["result"] == "restore_verified":
        if any(operation["state"] != "passed" for operation in result["operations"]):
            raise BackupError("verified receipt has incomplete operation evidence")
        if (
            not versions["match"]
            or not versions["postgres_server_version"]
            or not versions["timescaledb_extension_version"]
        ):
            raise BackupError("verified receipt has incomplete version evidence")
        if not counts:
            raise BackupError("verified receipt has no restored-dump counts")
        if not migrations["match"] or migrations["expected"] != migrations["applied"]:
            raise BackupError("verified receipt has incomplete migration evidence")
        if counts["migrations"] != migrations:
            raise BackupError("verified receipt migration evidence differs")
        if not api["health_probe"] or not api["authenticated_read"]:
            raise BackupError("verified receipt has incomplete API evidence")
        if not cleanup["verified"]:
            raise BackupError("verified receipt cleanup is not verified")
        _validate_verified_target(target)
        expected_removed = {
            *[f"container:{item['id']}" for item in target["services"].values()],
            *[f"container:{item['id']}" for item in target["helpers"]],
            *[f"volume:{item['name']}" for item in target["volumes"]],
            *[f"network:{item['id']}" for item in target["networks"]],
        }
        if set(cleanup["removed"]) != expected_removed:
            raise BackupError("verified receipt cleanup inventory is incomplete")
        if reference_paths & orphan_paths:
            raise BackupError("verified receipt raw classifications overlap")
        if (
            not counts["migrations"]["match"]
            or counts["migrations"]["expected"] != counts["migrations"]["applied"]
        ):
            raise BackupError("verified receipt restored migration inventory is inconsistent")
        if counts["provenance"]["tables"]["market_sweeps"] != len(result["references"]):
            raise BackupError("verified receipt reference coverage is incomplete")
        if counts["provenance"]["tables"]["scans"] != 0 or counts["groups"]["scans"]:
            raise BackupError("verified receipt contains unsupported legacy scan references")
        house_keys = set(counts["houses"]["keys"])
        plan_hashes = set(counts["market_plans"]["hashes"])
        if any(
            reference["logical"]["house_key"] not in house_keys
            or reference["logical"]["plan_hash"] not in plan_hashes
            for reference in result["references"]
        ):
            raise BackupError("verified receipt reference differs from restored house or plan inventory")
    return result


def _receipt_template(manifest: dict[str, Any], manifest_digest: str, started: dt.datetime) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "schema_version": 1,
        "backup_id": manifest["backup_id"],
        "manifest_sha256": manifest_digest,
        "result": "blocked",
        "started_at_utc": started.isoformat().replace("+00:00", "Z"),
        "ended_at_utc": started.isoformat().replace("+00:00", "Z"),
        "target_versions": {
            "postgres_server_version": None,
            "timescaledb_extension_version": None,
            "match": False,
        },
        "counts": {},
        "migrations": {"match": False, "expected": [], "applied": []},
        "references": [],
        "orphans": [],
        "api": {"health_probe": False, "authenticated_read": False},
        "operations": [{"name": name, "state": "not_run"} for name in OPERATIONS],
        "cleanup": {"verified": False, "removed": []},
        "target": {"project": None, "services": {}, "volumes": [], "networks": [], "helpers": []},
    }


def _mark(receipt: dict[str, Any], name: str, state: str) -> None:
    for operation in receipt["operations"]:
        if operation["name"] == name:
            operation["state"] = state
            return


def _create_and_capture_target(
    repo: Path,
    project: str,
    override: Path,
    credentials: Path,
    creation: dict[str, Any],
) -> dict[str, Any]:
    # Set the attempt flag before invoking Compose: it may create a subset and
    # then fail, leaving resources even when discovery never begins.
    creation["attempted"] = True
    _target_compose(repo, project, override, credentials, ["create", "--no-build"])
    target = _target_ids(repo, project, override, credentials)
    creation["target"] = target
    return target


def _cleanup_after_creation(
    creation: dict[str, Any],
    helpers: list[dict[str, Any]],
    project: str,
    source: dict[str, Any],
    live_before: dict[str, Any],
) -> dict[str, Any]:
    target = creation["target"]
    if not creation["attempted"]:
        _assert_project_absent(_live_inventory(), project)
        return {"verified": True, "removed": []}
    if not target.get("containers"):
        # Discovery failed or Compose failed after a partial create. The exact
        # resource set is unknown, so leave it untouched and retain blocked
        # cleanup evidence instead of blessing an empty list.
        return {"verified": False, "removed": []}
    return _remove_owned(target, helpers, project, source, live_before)


def _remove_owned(
    target: dict[str, Any],
    helpers: list[dict[str, Any]],
    project: str,
    source: dict[str, Any],
    live_before: dict[str, Any],
) -> dict[str, Any]:
    # Remove only exact resources whose identity and Compose ownership labels
    # still match the pre-cleanup record. Never use compose down or prune.
    removed: list[str] = []
    try:
        containers = list(target.get("containers", {}).values()) if target else []
        if not containers:
            return {"verified": False, "removed": removed}
        containers.extend(helpers)
        expected_membership = {network["name"]: network["id"] for network in target.get("networks", [])}
        recorded = source.get("recorded", {})
        forbidden_container_ids = {c["id"] for c in source["containers"].values()} | set(
            recorded.get("container_ids", [])
        )
        forbidden_volume_names = {v["name"] for v in source["volumes"].values()} | set(
            recorded.get("volume_names", [])
        )
        forbidden_networks = [*source["networks"], *recorded.get("networks", [])]
        forbidden_network_ids = {n["id"] for n in forbidden_networks}
        forbidden_network_names = {n["name"] for n in forbidden_networks}
        current = _live_inventory()
        _assert_source_current(current, source)
        expected_container_ids = {item["id"] for item in containers}
        expected_volume_names = {item["name"] for item in target.get("volumes", [])}
        expected_network_ids = {item["id"] for item in target.get("networks", [])}
        if (
            {
                cid
                for cid, item in current["containers"].items()
                if item["labels"].get("com.docker.compose.project") == project
            }
            != expected_container_ids
            or {
                name
                for name, item in current["volumes"].items()
                if item["labels"].get("com.docker.compose.project") == project
            }
            != expected_volume_names
            or {
                nid
                for nid, item in current["networks"].items()
                if item["labels"].get("com.docker.compose.project") == project
            }
            != expected_network_ids
        ):
            return {"verified": False, "removed": removed}
        for old in containers:
            cur = _inspect_container(old["id"])
            expected_service = old["labels"].get("com.docker.compose.service")
            if cur["id"] != old["id"] or not _labels_ok(cur["labels"], project, expected_service):
                return {"verified": False, "removed": removed}
            if cur["id"] in live_before["containers"] or cur["id"] in forbidden_container_ids:
                return {"verified": False, "removed": removed}
            if not _same_container_identity(old, cur):
                return {"verified": False, "removed": removed}
            if expected_service in {"db", "server"}:
                if not _target_network_attachment_matches(cur, expected_membership):
                    return {"verified": False, "removed": removed}
            elif expected_service == "reference-migration-helper":
                if not _target_network_attachment_matches(cur, expected_membership):
                    return {"verified": False, "removed": removed}
            elif not _helper_uses_only_none_network(cur, source["none_network_id"]):
                return {"verified": False, "removed": removed}
        for volume in target.get("volumes", []):
            cur = _inspect_volume(volume["name"])
            if (
                cur != volume
                or cur["labels"].get("com.docker.compose.project") != project
                or cur["name"] in live_before["volumes"]
                or cur["name"] in forbidden_volume_names
            ):
                return {"verified": False, "removed": removed}
        for network in target.get("networks", []):
            cur = _inspect_network(network["id"])
            if (
                cur != network
                or cur["labels"].get("com.docker.compose.project") != project
                or cur["id"] in live_before["networks"]
                or cur["id"] in forbidden_network_ids
                or cur["name"] in forbidden_network_names
            ):
                return {"verified": False, "removed": removed}
        # All cleanup identities have been checked before the first removal.
        ordered = sorted(
            containers,
            key=lambda c: {
                "server": 0,
                "reference-migration-helper": 1,
                "raw-restore-helper": 1,
                "raw-verify-helper": 1,
                "db": 2,
            }.get(c["labels"].get("com.docker.compose.service"), 1),
        )
        for old in ordered:
            _docker(["rm", "-f", old["id"]])
            current = _live_inventory()
            _assert_source_current(current, source)
            if old["id"] in current["containers"]:
                return {"verified": False, "removed": removed}
            removed.append(f"container:{old['id']}")
        for volume in target.get("volumes", []):
            _docker(["volume", "rm", volume["name"]])
            current = _live_inventory()
            _assert_source_current(current, source)
            if volume["name"] in current["volumes"]:
                return {"verified": False, "removed": removed}
            removed.append(f"volume:{volume['name']}")
        for network in target.get("networks", []):
            _docker(["network", "rm", network["id"]])
            current = _live_inventory()
            _assert_source_current(current, source)
            if network["id"] in current["networks"]:
                return {"verified": False, "removed": removed}
            removed.append(f"network:{network['id']}")
        current = _live_inventory()
        _assert_source_current(current, source)
        _assert_project_absent(current, project)
        expected_removed = sorted(
            [f"container:{item['id']}" for item in containers]
            + [f"volume:{item['name']}" for item in target.get("volumes", [])]
            + [f"network:{item['id']}" for item in target.get("networks", [])]
        )
        if sorted(removed) != expected_removed:
            return {"verified": False, "removed": sorted(removed)}
        return {"verified": True, "removed": sorted(removed)}
    except Exception:
        return {"verified": False, "removed": sorted(removed)}


def drill(repo: Path, snapshot_path: Path, root: Path) -> Path:
    repo = repo.resolve(strict=True)
    _ensure_root(root)
    started = dt.datetime.now(dt.UTC).replace(microsecond=0)
    manifest, manifest_digest = validate_snapshot(snapshot_path)
    source_config, source_containers, source_meta = _source_resources(repo)
    live_before = source_meta["live_inventory"]
    source = {
        "containers": source_containers,
        "volumes": {
            key: live_before["volumes"][value["name"]] for key, value in source_meta["volumes"].items()
        },
        "networks": [live_before["networks"][network["id"]] for network in source_meta["networks"]],
        "none_network_id": _builtin_none_network_id(live_before),
        "recorded": {
            "container_ids": [item["id"] for item in manifest["source"]["containers"].values()],
            "volume_names": [item["name"] for item in manifest["source"]["volumes"].values()],
            "networks": manifest["source"]["networks"],
        },
        "recorded_images": {
            "db": manifest["source"]["containers"]["database"]["image_id"],
            "server": manifest["source"]["containers"]["server"]["image_id"],
        },
        "target_volume_keys": {"db": "crowley-db", "server": "crowley-raw"},
    }
    src = manifest["source"]
    # Recorded source resource names and IDs remain provenance and deny-set
    # entries. Restore compatibility below is pinned to content and images.
    source_images = {}
    for key, service in (("database", "db"), ("server", "server")):
        identity = _image_identity(source_containers[service]["image_id"])
        source_images[key] = {
            "image_id": identity["id"],
            "repo_digests": identity["repo_digests"],
        }
    postgres_version, timescale_version = _local_version(source_containers["db"]["id"])
    compatibility = {
        "compose_file_sha256": _repo_digest(repo / COMPOSE),
        "migrations": _migration_inventory(repo),
        "postgres_server_version": postgres_version,
        "timescaledb_extension_version": timescale_version,
        "containers": source_images,
        "server_image_migrations": _target_image_migrations(src["containers"]["server"]["image_id"]),
    }
    validate_source_compatibility(src, compatibility)
    project = f"crowley-drill-{uuid.uuid4().hex[:16]}"
    _assert_project_absent(live_before, project)
    receipt_dir = root / f"drill-{manifest['backup_id']}-{uuid.uuid4().hex[:8]}"
    receipt_dir.mkdir(mode=0o700)
    override = receipt_dir / "compose.override.yml"
    credentials = receipt_dir / "credentials.env"
    receipt_path = receipt_dir / "receipt.json"
    _write_override(override, project)
    database_password = uuid.uuid4().hex + uuid.uuid4().hex
    api_token = uuid.uuid4().hex + uuid.uuid4().hex
    env = "\n".join(
        (
            f"CROWLEY_DB_PASSWORD={database_password}",
            f"CROWLEY_API_TOKEN={api_token}",
            f"MARKET_BACKUP_DB_IMAGE={src['containers']['database']['image_id']}",
            f"MARKET_BACKUP_SERVER_IMAGE={src['containers']['server']['image_id']}",
            "",
        )
    )
    _set_private(credentials, env.encode("ascii"))
    receipt = _receipt_template(manifest, manifest_digest, started)
    receipt["target"]["project"] = project
    target: dict[str, Any] = {"containers": {}, "volumes": [], "networks": []}
    creation = {"attempted": False, "target": target}
    helpers: list[dict[str, Any]] = []
    error: Exception | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None
    failure_metadata: dict[str, Any] | None = None
    try:
        config = _compose_config(
            repo, ["-f", str(override), "--env-file", str(credentials), "--project-name", project]
        )
        validate_compose_isolation(
            config,
            project,
            expected_images={
                "db": src["containers"]["database"]["image_id"],
                "server": src["containers"]["server"]["image_id"],
            },
        )
        _mark(receipt, "validate_snapshot", "passed")
        before_create = _live_inventory()
        _assert_project_absent(before_create, project)
        _assert_source_current(before_create, source)
        target = _create_and_capture_target(repo, project, override, credentials, creation)
        receipt["target"] = _safe_target_receipt(target, project)
        _target_guard(target, project, source, live_before, service_state="created")
        _mark(receipt, "create_disposable_resources", "passed")
        db_id = target["containers"]["db"]["id"]
        server_id = target["containers"]["server"]["id"]
        _guard_current(repo, project, override, credentials, source, target, helpers)
        failure_stage = "start_disposable_database"
        try:
            _target_compose(repo, project, override, credentials, ["start", "db"])
        except BackupError as exc:
            failure_reason = _safe_failure_reason(exc)
            raise BackupError(failure_stage) from None
        _mark(receipt, "start_disposable_database", "passed")
        failure_stage = None
        failure_stage = "database_readiness"
        readiness_query = "not_ready"
        for _ in range(60):
            try:
                if _db_sql(db_id, "SELECT 1", "postgres") == "1":
                    failure_stage = None
                    break
                readiness_query = "unexpected_result"
            except Exception:
                readiness_query = "query_failed"
            time.sleep(1)
        else:
            failure_stage = "database_readiness"
            try:
                _guard_current(repo, project, override, credentials, source, target, helpers)
            except Exception:
                failure_metadata = {"identity_recheck": "blocked", "metadata_read": "not_run"}
            else:
                probe = _docker(["exec", db_id, "pg_isready", "-U", "crowley", "-d", "postgres"], check=False)
                failure_metadata = {
                    "identity_recheck": "passed",
                    "pg_isready": _readiness_status(probe.returncode),
                    "readiness_query": readiness_query,
                }
                try:
                    failure_metadata["database"] = _disposable_setup_metadata(db_id)
                    failure_metadata["metadata_read"] = "passed"
                except Exception:
                    failure_metadata["metadata_read"] = "failed"
            raise BackupError("disposable database did not become ready")
        failure_stage = "identity_before_database"
        try:
            _guard_current(repo, project, override, credentials, source, target, helpers)
        except BackupError as exc:
            failure_reason = _safe_failure_reason(exc)
            raise BackupError(failure_stage) from None
        failure_stage = "create_database"
        try:
            _db_sql(db_id, "CREATE DATABASE crowley", "postgres")
        except BackupError as exc:
            failure_reason = _safe_failure_reason(exc)
            raise BackupError(failure_stage) from None
        failure_stage = "identity_after_database"
        try:
            _guard_current(repo, project, override, credentials, source, target, helpers)
        except BackupError as exc:
            failure_reason = _safe_failure_reason(exc)
            raise BackupError(failure_stage) from None
        failure_stage = "create_extension"
        try:
            _db_sql(db_id, "CREATE EXTENSION IF NOT EXISTS timescaledb", "crowley")
        except BackupError as exc:
            failure_reason = _safe_failure_reason(exc)
            raise BackupError(failure_stage) from None
        _mark(receipt, "create_database_extension", "passed")
        failure_stage = None
        _guard_current(repo, project, override, credentials, source, target, helpers)
        _db_sql(db_id, "SELECT timescaledb_pre_restore()")
        _mark(receipt, "timescaledb_pre_restore", "passed")
        _guard_current(repo, project, override, credentials, source, target, helpers)
        with (snapshot_path / "database.dump").open("rb") as dump_handle:
            failure_stage = "pg_restore_single_job_no_owner"
            code, pg_restore_category = _stream_pg_restore(
                _pg_restore_args(db_id),
                dump_handle,
            )
        if code:
            failure_reason = pg_restore_category
            raise BackupError(failure_stage) from None
        _mark(receipt, "pg_restore_single_job_no_owner", "passed")
        failure_stage = None
        _guard_current(repo, project, override, credentials, source, target, helpers)
        _db_sql(db_id, "SELECT timescaledb_post_restore()")
        _mark(receipt, "timescaledb_post_restore", "passed")
        _guard_current(repo, project, override, credentials, source, target, helpers)
        _db_sql(db_id, "ANALYZE")
        _mark(receipt, "analyze", "passed")
        pg_version, ts_version = _target_version(db_id)
        receipt["target_versions"] = {
            "postgres_server_version": pg_version,
            "timescaledb_extension_version": ts_version,
            "match": pg_version == src["postgres_server_version"]
            and ts_version == src["timescaledb_extension_version"],
        }
        if not receipt["target_versions"]["match"]:
            raise BackupError("target database versions differ from snapshot")
        failure_stage = "count_restored_tables"
        network_names = {network["name"] for network in target["networks"]}
        if len(network_names) != 1:
            raise BackupError("disposable database network is not unique")

        def register_reference_helper(helper: dict[str, Any]) -> None:
            if helper["id"] in live_before["containers"] or helper["id"] in {
                container["id"] for container in source["containers"].values()
            }:
                raise BackupError("reference migration helper identity collides with source")
            helpers.append(helper)
            _guard_current(repo, project, override, credentials, source, target, helpers)

        _guard_current(repo, project, override, credentials, source, target, helpers)
        reference_inventory = _reference_database_inventory(
            db_id,
            src["containers"]["server"]["image_id"],
            next(iter(network_names)),
            credentials,
            project,
            register_reference_helper,
            src["migrations"],
        )
        _guard_current(repo, project, override, credentials, source, target, helpers)
        restored_inventory = _catalog_inventory(db_id, "crowley")
        compare_schema_inventories(reference_inventory, restored_inventory)
        receipt["counts"] = _count_restored(db_id, reference_inventory, src["migrations"])
        receipt["migrations"] = receipt["counts"]["migrations"]
        _mark(receipt, "count_restored_tables", "passed")
        failure_stage = None
        raw_volume = next(
            m["Name"] for m in target["containers"]["server"]["mounts"] if m["Destination"] == "/data"
        )
        _guard_current(repo, project, override, credentials, source, target, helpers)
        helper_id, helper_info = _extract_raw(
            repo,
            project,
            raw_volume,
            src["containers"]["server"]["image_id"],
            snapshot_path / "raw.tar",
            source,
            live_before,
            target,
            override,
            credentials,
        )
        helpers.append(helper_info)
        receipt["target"]["helpers"].append(_safe_helper_receipt(helper_info, "raw-restore-helper"))
        _mark(receipt, "extract_raw_archive", "passed")
        _verify_id, verify_info = _verify_raw_volume(
            repo,
            project,
            raw_volume,
            src["containers"]["server"]["image_id"],
            manifest,
            live_before,
            source,
            target,
            override,
            credentials,
            helpers,
        )
        helpers.append(verify_info)
        receipt["target"]["helpers"].append(_safe_helper_receipt(verify_info, "raw-verify-helper"))
        references, orphans = _raw_references(db_id, snapshot_path / "raw.tar", manifest)
        receipt["references"], receipt["orphans"] = references, orphans
        _mark(receipt, "validate_raw_references", "passed")
        _guard_current(repo, project, override, credentials, source, target, helpers)
        _target_compose(repo, project, override, credentials, ["start", "server"])
        _mark(receipt, "start_server", "passed")
        for _ in range(60):
            if _probe_api(server_id, authenticated=False):
                receipt["api"]["health_probe"] = True
                break
            time.sleep(1)
        _mark(receipt, "health_probe", "passed" if receipt["api"]["health_probe"] else "failed")
        if not receipt["api"]["health_probe"]:
            raise BackupError("private health probe failed")
        applied_after = _json_sql(
            db_id, "SELECT coalesce(json_agg(name ORDER BY name),'[]'::json) FROM schema_migrations"
        )
        if applied_after != receipt["migrations"]["expected"]:
            receipt["migrations"]["match"] = False
            raise BackupError("server startup applied an unreviewed migration")
        for _ in range(15):
            if _probe_api(server_id, authenticated=True):
                receipt["api"]["authenticated_read"] = True
                break
            time.sleep(1)
        _mark(receipt, "authenticated_read", "passed" if receipt["api"]["authenticated_read"] else "failed")
        if not receipt["api"]["authenticated_read"]:
            raise BackupError("private authenticated read probe failed")
    except Exception as exc:
        error = exc
        failure_stage = failure_stage or _safe_failure_stage(exc)
        failure_reason = failure_reason or _safe_failure_reason(exc)
        if failure_stage in {"create_database", "create_extension"}:
            try:
                _guard_current(repo, project, override, credentials, source, target, helpers)
            except Exception:
                failure_metadata = {"identity_recheck": "blocked", "metadata_read": "not_run"}
            else:
                try:
                    failure_metadata = {
                        "identity_recheck": "passed",
                        "metadata_read": "passed",
                        "database": _disposable_setup_metadata(db_id),
                    }
                except Exception:
                    failure_metadata = {"identity_recheck": "passed", "metadata_read": "failed"}
        elif failure_stage == "pg_restore_single_job_no_owner":
            try:
                _guard_current(repo, project, override, credentials, source, target, helpers)
            except Exception:
                failure_metadata = {"identity_recheck": "blocked", "metadata_read": "not_run"}
            else:
                try:
                    failure_metadata = {
                        "identity_recheck": "passed",
                        "metadata_read": "passed",
                        "database": _disposable_restore_metadata(db_id),
                    }
                except Exception:
                    failure_metadata = {"identity_recheck": "passed", "metadata_read": "failed"}
        # Do not persist exception text: subprocess errors and SQL details may
        # include data. Result is captured as a categorical state only.
        message = str(exc).lower()
        blocked = isinstance(exc, BackupError) and any(
            token in message
            for token in ("identity", "ownership", "ambiguous", "disjoint", "fresh and isolated")
        )
        receipt["result"] = "blocked" if blocked else "failed"
        for operation in receipt["operations"]:
            if operation["state"] == "not_run":
                operation["state"] = "blocked" if blocked else "failed"
                break
    finally:
        try:
            if creation["target"]["containers"]:
                target = creation["target"]
                _guard_current(repo, project, override, credentials, source, target, helpers)
                target = _target_ids(repo, project, override, credentials)
                creation["target"] = target
                receipt["target"] = _safe_target_receipt(target, project)
                receipt["target"]["helpers"] = [
                    _safe_helper_receipt(helper, helper["labels"]["com.docker.compose.service"])
                    for helper in helpers
                ]
            cleanup = _cleanup_after_creation(creation, helpers, project, source, live_before)
            cleanup_ok = cleanup["verified"]
            receipt["cleanup"] = cleanup
            _mark(receipt, "cleanup_owned_resources", "passed" if cleanup_ok else "blocked")
        except Exception:
            cleanup_ok = False
            receipt["cleanup"] = {"verified": False, "removed": []}
            _mark(receipt, "cleanup_owned_resources", "blocked")
        try:
            credentials.unlink(missing_ok=True)
            override.unlink(missing_ok=True)
            receipt["ended_at_utc"] = (
                dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
            if error is None and cleanup_ok:
                receipt["result"] = "restore_verified"
            elif error is None:
                receipt["result"] = "blocked"
            validate_receipt(receipt)
            receipt_bytes = canonical_json(receipt)
            _set_private(receipt_path, receipt_bytes)
            if failure_stage is not None:
                failure_record = {
                    "schema": "crowley.market-backup-failure-stage",
                    "schema_version": 1,
                    "backup_id": receipt["backup_id"],
                    "manifest_sha256": manifest_digest,
                    "receipt_sha256": sha256_bytes(receipt_bytes),
                    "result": receipt["result"],
                    "stage": failure_stage,
                    "reason": failure_reason,
                    "database_metadata": failure_metadata,
                }
                _set_private(receipt_dir / "failure-stage.json", canonical_json(failure_record))
            _fsync_dir(receipt_dir)
        except Exception as exc:
            if error is None:
                error = exc
    if error is not None:
        if failure_stage is not None:
            raise BackupError(
                f"restore drill {receipt['result']} at {failure_stage}; private receipt retained"
            ) from None
        raise BackupError(f"restore drill {receipt['result']}; private receipt retained") from None
    return receipt_path


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot")
    drill_parser = subparsers.add_parser("drill")
    drill_parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            result = snapshot(args.repo, args.root)
            print(f"snapshot complete: {result.name}")
        else:
            result = drill(args.repo, args.snapshot, args.root)
            receipt = strict_json(result.read_bytes())
            print(f"restore drill {receipt['result']}: {result.name}")
        return 0
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print(
            "bounded backup operation failed; inspect private receipt or incomplete snapshot", file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
