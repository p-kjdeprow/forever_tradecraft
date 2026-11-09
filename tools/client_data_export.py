#!/usr/bin/env python3
"""Build, export, load, and verify the private client-data pipeline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from crowley.config import host_config_path, load_host_config
from crowley.core.client_data import (
    EXPECTED_CLIENT_BUILD,
    TABLE_COLUMNS,
    ClientDataError,
    NormalizedExport,
    manifest_sha256,
    normalize_export,
)
from crowley.core.game_target import packaged_game_target
from crowley.server.db import migrate

_TARGET = packaged_game_target()
PRODUCT = _TARGET.product
PRODUCT_DIR = _TARGET.product_dir
IMAGE = f"crowley-client-data:{_TARGET.native_build}"
TABLES = (
    "SkillLine",
    "SkillLineAbility",
    "Spell",
    "SpellReagents",
    "SpellEffect",
    "SpellName",
    "SpellCooldowns",
    "SpellCategories",
    "SpellCategory",
    "SpellTotems",
    "SpellCastingRequirements",
    "ItemSparse",
    "Item",
    "ItemEffect",
    "ItemXItemEffect",
)
MANIFEST_VERSION = 1
EXPECTED_TOOL_PINS = {
    "sdk_image": (
        "mcr.microsoft.com/dotnet/sdk@sha256:747cbade5bfd918a175ac32f8f1d931edec2c7d0d5397a3de72eaef1ac1f66d2"
    ),
    "runtime_image": (
        "mcr.microsoft.com/dotnet/runtime@sha256:0cf09aaccf6a9f68900aed4c73fb28fe25bb59ec85de4dea08ba02240a8635f3"
    ),
    "TACTSharp": "wowdev/TACTSharp@d89204e07fa062ccc8f2738ebd0209782912da48",
    "DBCD": "wowdev/DBCD@e732093f8864240fc5884bd1bba6b02f3dfc0d56",
    "DBDefsLib": "1.1.0",
    "WoWDBDefs": "wowdev/WoWDBDefs@7eaa562b62037d12327543fe22719a59870bb905",
    "fdids": "app/fdids.csv",
    "fdids_sha256": "dd2191b0331e57f8c14ddbb084be8d66aaab4ebfcb47f83ce02cd1d2f262badf",
    "listfile_release": "202610202236",
    "listfile_sha256": "4af8062799478d6f7a038ac9b9f3df1d9d519230e369e1bb399ff0ab41507fd6",
}
MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "export_key",
        "client_build",
        "product",
        "build_name",
        "build_key",
        "cdn_key",
        "build_info_sha256",
        "dbcache_sha256",
        "dbcache_build",
        "dbcache_version",
        "tool_pins",
        "tables",
    }
)
TABLE_INFO_KEYS = frozenset(
    {
        "fdid",
        "tableHash",
        "layoutHash",
        "baseRows",
        "finalRows",
        "hotfixEntries",
        "hotfixEntriesPushed",
        "hotfixEntriesPushIdMinus1",
        "hotfixOps",
        "hotfixResults",
        "distinctRecordsTouched",
        "hotfixAdded",
        "hotfixChanged",
        "hotfixIdentical",
        "hotfixDeleted",
        "deletesOfAbsentRows",
        "rowsHiddenByMissingKeys",
        "hiddenIds",
        "encryptedSections",
        "columns",
        "csvSha256",
    }
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ExportCLIError(RuntimeError):
    """Raised for a refused or invalid export operation."""


class ExportConflictError(ExportCLIError):
    """Raised when an export key already has different content."""


@dataclass(frozen=True)
class InstallMetadata:
    client_build: str
    build_key: str
    cdn_key: str
    dbcache_path: Path
    dbcache_sha256: str
    dbcache_build: int
    build_info_sha256: str

    @property
    def export_key(self) -> str:
        return f"{self.client_build}-{self.dbcache_sha256[:12]}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_info(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ExportCLIError(".build.info is empty")
    headers = [header.split("!", 1)[0] for header in lines[0].split("|")]
    records = []
    for line in lines[1:]:
        if line:
            fields = line.split("|")
            if len(fields) != len(headers):
                raise ExportCLIError(".build.info row has the wrong number of fields")
            records.append(dict(zip(headers, fields, strict=True)))
    active = [row for row in records if row.get("Product") == PRODUCT and row.get("Active") == "1"]
    if len(active) != 1:
        raise ExportCLIError(f"expected exactly one active {PRODUCT} row, found {len(active)}")
    row = active[0]
    if row.get("Version") != EXPECTED_CLIENT_BUILD:
        raise ExportCLIError(f"client build {row.get('Version')!r} != {EXPECTED_CLIENT_BUILD}")
    for key in ("Build Key", "CDN Key"):
        if not row.get(key):
            raise ExportCLIError(f"active .build.info row has no {key}")
    return row


def _dbcache_metadata(path: Path) -> tuple[int, str]:
    with path.open("rb") as handle:
        header = handle.read(12)
    if len(header) != 12:
        raise ExportCLIError("DBCache.bin is shorter than its XFTH header")
    magic, version, build = struct.unpack("<4sii", header)
    if magic != b"XFTH" or version != 9:
        raise ExportCLIError(f"DBCache.bin is not XFTH v9: magic={magic!r} version={version}")
    return build, _sha256(path)


def inspect_install(install: str | Path) -> InstallMetadata:
    """Validate the exact client identity before Docker is invoked."""

    root = Path(install).expanduser().resolve(strict=True)
    build_info = root / ".build.info"
    flavor = root / PRODUCT_DIR / ".flavor.info"
    dbcache = root / PRODUCT_DIR / "Cache" / "ADB" / "enUS" / "DBCache.bin"
    if not build_info.is_file() or not flavor.is_file() or not dbcache.is_file():
        raise ExportCLIError("install is missing .build.info, .flavor.info, or DBCache.bin")
    flavor_lines = flavor.read_text(encoding="utf-8").splitlines()
    if len(flavor_lines) < 2 or flavor_lines[1].strip() != PRODUCT:
        actual = flavor_lines[1].strip() if len(flavor_lines) >= 2 else ""
        raise ExportCLIError(f".flavor.info product {actual!r} != {PRODUCT}")
    row = _build_info(build_info)
    dbcache_build, dbcache_sha = _dbcache_metadata(dbcache)
    build_number = int(EXPECTED_CLIENT_BUILD.split(".")[-1])
    if dbcache_build != build_number:
        raise ExportCLIError(f"DBCache build {dbcache_build} != client build {build_number}")
    return InstallMetadata(
        client_build=EXPECTED_CLIENT_BUILD,
        build_key=row["Build Key"],
        cdn_key=row["CDN Key"],
        dbcache_path=dbcache,
        dbcache_sha256=dbcache_sha,
        dbcache_build=dbcache_build,
        build_info_sha256=_sha256(build_info),
    )


def _chmod_private(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.chmod(stat.S_IRWXU)
        else:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    root.chmod(stat.S_IRWXU)


def _canonical_export_key(client_build: str, dbcache_sha256: str) -> str:
    return f"{client_build}-{dbcache_sha256[:12]}"


def _read_manifest(export_dir: Path) -> dict[str, Any]:
    path = export_dir / "manifest.json"
    if not path.is_file():
        raise ExportCLIError("export requires manifest.json")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExportCLIError("manifest.json is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise ExportCLIError("manifest must be a JSON object")
    return manifest


def _validate_manifest_schema(manifest: dict[str, Any], *, metadata: InstallMetadata | None = None) -> str:
    if set(manifest) != MANIFEST_KEYS:
        raise ExportCLIError(f"manifest has keys {sorted(manifest)}; expected {sorted(MANIFEST_KEYS)}")
    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise ExportCLIError("unsupported manifest version")
    if manifest["client_build"] != EXPECTED_CLIENT_BUILD:
        raise ExportCLIError(f"manifest client build {manifest['client_build']!r} != {EXPECTED_CLIENT_BUILD}")
    if manifest["product"] != PRODUCT:
        raise ExportCLIError(f"manifest product {manifest['product']!r} != {PRODUCT}")
    if not isinstance(manifest["build_name"], str) or not manifest["build_name"]:
        raise ExportCLIError("manifest build_name is missing")
    for field in ("build_key", "cdn_key", "build_info_sha256", "dbcache_sha256"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise ExportCLIError(f"manifest {field} is missing")
    for field in ("build_info_sha256", "dbcache_sha256"):
        if not HEX64.fullmatch(manifest[field]):
            raise ExportCLIError(f"manifest {field} is not a lowercase SHA-256")
    if not isinstance(manifest["dbcache_build"], int) or not isinstance(manifest["dbcache_version"], int):
        raise ExportCLIError("manifest DBCache metadata is not typed")
    if manifest["dbcache_build"] != int(EXPECTED_CLIENT_BUILD.split(".")[-1]):
        raise ExportCLIError("manifest DBCache build does not match client build")
    if manifest["dbcache_version"] != 9:
        raise ExportCLIError("manifest DBCache version is not XFTH v9")
    tool_pins = manifest["tool_pins"]
    if not isinstance(tool_pins, dict) or set(tool_pins) != set(EXPECTED_TOOL_PINS):
        raise ExportCLIError("manifest tool_pins schema is invalid")
    if tool_pins != EXPECTED_TOOL_PINS:
        raise ExportCLIError("manifest tool_pins do not match the pinned exporter")
    tables = manifest["tables"]
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ExportCLIError("manifest table set is invalid")
    for table in TABLES:
        info = tables[table]
        if not isinstance(info, dict) or set(info) != TABLE_INFO_KEYS:
            raise ExportCLIError(f"manifest schema for table {table} is invalid")
        if info["columns"] != len(TABLE_COLUMNS[table]):
            raise ExportCLIError(f"manifest column count for {table} is invalid")
        if not isinstance(info["finalRows"], int) or info["finalRows"] < 0:
            raise ExportCLIError(f"manifest row count for {table} is invalid")
        if not isinstance(info["csvSha256"], str) or not HEX64.fullmatch(info["csvSha256"]):
            raise ExportCLIError(f"manifest CSV hash for {table} is invalid")
        if not isinstance(info["hiddenIds"], list) or not all(
            isinstance(item, int) for item in info["hiddenIds"]
        ):
            raise ExportCLIError(f"manifest hidden IDs for {table} are invalid")
        if not isinstance(info["encryptedSections"], list):
            raise ExportCLIError(f"manifest encrypted sections for {table} are invalid")
    export_key = manifest["export_key"]
    if not isinstance(export_key, str) or export_key != _canonical_export_key(
        manifest["client_build"], manifest["dbcache_sha256"]
    ):
        raise ExportCLIError("manifest export_key is not canonical")
    if metadata is not None:
        expected = {
            "client_build": metadata.client_build,
            "build_key": metadata.build_key,
            "cdn_key": metadata.cdn_key,
            "build_info_sha256": metadata.build_info_sha256,
            "dbcache_sha256": metadata.dbcache_sha256,
            "dbcache_build": metadata.dbcache_build,
        }
        if any(manifest[field] != value for field, value in expected.items()):
            raise ExportCLIError("manifest source identity does not match the inspected install")
    return export_key


def _validate_export(
    export_dir: Path,
    *,
    metadata: InstallMetadata | None = None,
    require_directory_name: bool,
) -> dict[str, Any]:
    manifest = _read_manifest(export_dir)
    export_key = _validate_manifest_schema(manifest, metadata=metadata)
    if require_directory_name and export_dir.name != export_key:
        raise ExportCLIError(
            f"export directory {export_dir.name!r} does not equal manifest export_key {export_key!r}"
        )
    csv_dir = export_dir / "csv"
    if not csv_dir.is_dir():
        raise ExportCLIError("export is missing csv directory")
    csv_files = {path.name.removesuffix(".csv") for path in csv_dir.glob("*.csv")}
    if csv_files != set(TABLES):
        raise ExportCLIError(f"CSV table set {sorted(csv_files)} does not match manifest")
    for table in TABLES:
        path = csv_dir / f"{table}.csv"
        raw = path.read_bytes()
        info = manifest["tables"][table]
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != info["csvSha256"]:
            raise ExportCLIError(f"CSV hash mismatch for {table}")
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise ExportCLIError(f"CSV {table} is empty") from exc
            expected_header = list(TABLE_COLUMNS[table])
            if header != expected_header:
                raise ExportCLIError(f"CSV header mismatch for {table}")
            row_count = sum(1 for _ in reader)
        if row_count != info["finalRows"]:
            raise ExportCLIError(f"CSV row count mismatch for {table}: {row_count} != {info['finalRows']}")
    return manifest


def _docker_source_revisions() -> dict[str, str]:
    revisions: dict[str, str] = {}
    for argument, pin_name in (
        ("TACTSHARP_SHA", "TACTSharp"),
        ("DBCD_SHA", "DBCD"),
        ("WOWDBDEFS_SHA", "WoWDBDefs"),
    ):
        pinned = EXPECTED_TOOL_PINS[pin_name]
        prefix, separator, revision = pinned.rpartition("@")
        if not separator or not prefix or not revision:
            raise ExportCLIError(f"tool pin {pin_name} is not a repository revision")
        revisions[argument] = revision
    return revisions


def build_image(*, tag: str = IMAGE, runner: Callable[..., Any] = subprocess.run) -> None:
    command = ["docker", "build"]
    for argument, revision in _docker_source_revisions().items():
        command.extend(("--build-arg", f"{argument}={revision}"))
    command.extend(("-t", tag, str(Path(__file__).with_name("client_data"))))
    runner(command, check=True)


def export_client_data(
    install: str | Path,
    out_root: str | Path | None = None,
    *,
    image: str = IMAGE,
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """Run the read-only exporter and atomically publish one private export."""

    install_root = Path(install).expanduser().resolve(strict=True)
    metadata = inspect_install(install_root)
    root = (
        Path(out_root).expanduser()
        if out_root is not None
        else Path.home() / ".local/state/crowley/client-data"
    )
    root = root.resolve()
    if root == install_root or install_root in root.parents:
        raise ExportCLIError("output root may not be inside the game install")
    # Bulk game data must never land where it could be committed: ask git whether the nearest
    # existing ancestor is inside any work tree or git directory (linked worktrees included).
    existing = next(candidate for candidate in (root, *root.parents) if candidate.exists())
    probe = subprocess.run(
        ["git", "-C", str(existing), "rev-parse", "--is-inside-work-tree", "--is-inside-git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and "true" in probe.stdout.split():
        raise ExportCLIError(f"output root may not be inside the repository at {existing}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(stat.S_IRWXU)
    target = root / metadata.export_key
    if target.exists():
        raise ExportCLIError(f"refusing to overwrite existing export {target}")

    with tempfile.TemporaryDirectory(prefix="client-data-", dir=root) as temp_name:
        staging = Path(temp_name) / "export"
        cache = Path(temp_name) / "cache"
        staging.mkdir(mode=0o700)
        cache.mkdir(mode=0o700)
        command = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{install_root}:/wow:ro",
            "-v",
            f"{staging.resolve()}:/work/out",
            "-v",
            f"{cache.resolve()}:/work/cache",
            image,
            "--wow",
            "/wow",
            "--product",
            PRODUCT,
            "--productdir",
            PRODUCT_DIR,
            "--expected-build",
            EXPECTED_CLIENT_BUILD,
            "--out",
            "/work/out",
            "--cache",
            "/work/cache",
            "--tables",
            ",".join(TABLES),
        ]
        runner(command, check=True)
        _validate_export(staging, metadata=metadata, require_directory_name=False)
        _chmod_private(staging)
        staging.rename(target)
    _validate_export(target, require_directory_name=True)
    _chmod_private(target)
    return target


def _manifest_for_load(export_dir: Path) -> dict[str, Any]:
    return _validate_export(export_dir, require_directory_name=True)


def _insert_normalized(conn: psycopg.Connection, export_dir: Path, data: NormalizedExport) -> dict[str, Any]:
    manifest = _manifest_for_load(export_dir)
    digest = manifest_sha256(manifest)
    export_key = manifest["export_key"]
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (export_key,)).fetchone()
    existing = conn.execute(
        "SELECT id, manifest_sha256 FROM client_data_exports WHERE export_key=%s FOR UPDATE", (export_key,)
    ).fetchone()
    if existing:
        if existing[1] != digest:
            raise ExportConflictError(f"export key {export_key} has a different manifest")
        return {"status": "duplicate", "export_key": export_key, "id": existing[0]}

    export_id = conn.execute(
        """
        INSERT INTO client_data_exports (export_key, client_build, manifest, manifest_sha256)
        VALUES (%s,%s,%s,%s) RETURNING id
        """,
        (export_key, EXPECTED_CLIENT_BUILD, Jsonb(manifest), digest),
    ).fetchone()[0]
    for recipe in data.recipes:
        conn.execute(
            """
            INSERT INTO client_recipes
              (export_id, spell_id, name, kind, learn_kind, learn_evidence, cooldown_ms,
               category_cooldown_ms, category_id, category_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                export_id,
                recipe.spell_id,
                recipe.name,
                recipe.kind,
                recipe.learn_kind,
                recipe.learn_evidence,
                recipe.cooldown_ms,
                recipe.category_cooldown_ms,
                recipe.category_id,
                recipe.category_name,
            ),
        )
        for association in recipe.associations:
            conn.execute(
                """
                INSERT INTO client_recipe_skill_lines
                  (export_id, spell_id, ability_id, skill_line_id, profession, min_rank, yellow,
                   green_derived, grey, acquire_method, flags, supersedes_spell)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    export_id,
                    association.spell_id,
                    association.ability_id,
                    association.skill_line_id,
                    association.profession,
                    association.min_rank,
                    association.yellow,
                    association.green_derived,
                    association.grey,
                    association.acquire_method,
                    association.flags,
                    association.supersedes_spell,
                ),
            )
        for reagent in recipe.reagents:
            conn.execute(
                "INSERT INTO client_recipe_reagents VALUES (%s,%s,%s,%s)",
                (export_id, recipe.spell_id, reagent.item_id, reagent.count),
            )
        for output in recipe.outputs:
            conn.execute(
                "INSERT INTO client_recipe_outputs VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    export_id,
                    recipe.spell_id,
                    output.item_id,
                    output.base_quantity,
                    output.variance,
                    output.yield_kind,
                ),
            )
        for teaching_item_id in recipe.teaching_items:
            conn.execute(
                "INSERT INTO client_recipe_teachers VALUES (%s,%s,%s)",
                (export_id, recipe.spell_id, teaching_item_id),
            )
        for ordinal, totem_id in enumerate(recipe.totems):
            conn.execute(
                "INSERT INTO client_recipe_totems VALUES (%s,%s,%s,%s)",
                (export_id, recipe.spell_id, ordinal, totem_id),
            )
        for ordinal, category_id in enumerate(recipe.totem_categories):
            conn.execute(
                "INSERT INTO client_recipe_totem_categories VALUES (%s,%s,%s,%s)",
                (export_id, recipe.spell_id, ordinal, category_id),
            )
        if recipe.spell_focus is not None:
            conn.execute(
                "INSERT INTO client_recipe_spell_focus VALUES (%s,%s,%s)",
                (export_id, recipe.spell_id, recipe.spell_focus),
            )
    for item in data.items:
        conn.execute(
            """
            INSERT INTO client_items
              (export_id, item_id, status, name, quality, item_level, class_id, subclass_id,
               sell_price, buy_price, vendor_stack_count, bonding, max_stack, required_skill,
               required_skill_rank)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                export_id,
                item.item_id,
                item.status,
                item.name,
                item.quality,
                item.item_level,
                item.class_id,
                item.subclass_id,
                item.sell_price,
                item.buy_price,
                item.vendor_stack_count,
                item.bonding,
                item.max_stack,
                item.required_skill,
                item.required_skill_rank,
            ),
        )
    return {"status": "created", "export_key": export_key, "id": export_id}


def load_export(export_dir: str | Path, database_url: str) -> dict[str, Any]:
    """Load a normalized export in one transaction, applying migrations first."""

    directory = Path(export_dir).expanduser().resolve(strict=True)
    _manifest_for_load(directory)
    data = normalize_export(directory)
    with psycopg.connect(database_url) as conn:
        migrate(conn)
        try:
            with conn.transaction():
                result = _insert_normalized(conn, directory, data)
        except ExportConflictError:
            raise
    return {**result, "recipes": len(data.recipes), "items": len(data.items)}


COMPARE_FIELDS = (
    "sell_price",
    "max_stack",
    "bonding",
    "class_id",
    "subclass_id",
    "quality",
    "item_level",
)


def verify_live(database_url: str, house: str) -> list[str]:
    """Compare client facts with the latest in-game reference for one house."""

    query = """
        SELECT ci.item_id, ci.sell_price AS client_sell_price, ci.max_stack AS client_max_stack,
               ci.bonding AS client_bonding, ci.class_id AS client_class_id,
               ci.subclass_id AS client_subclass_id, ci.quality AS client_quality,
               ci.item_level AS client_item_level,
               ir.sell_price AS live_sell_price, ir.max_stack AS live_max_stack,
               ir.bind_type AS live_bonding, ir.class_id AS live_class_id,
               ir.subclass_id AS live_subclass_id, ir.quality AS live_quality,
               ir.item_level AS live_item_level
        FROM latest_client_export e
        JOIN client_items ci ON ci.export_id = e.id
        JOIN latest_item_reference ir ON ir.item_id = ci.item_id AND ir.client_build = e.client_build
        JOIN houses h ON h.id = ir.house_id
        WHERE h.key = %s
        ORDER BY ci.item_id
    """
    mismatches: list[str] = []
    compared = 0
    with psycopg.connect(database_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        for row in cur.execute(query, (house,)):
            compared += 1
            for field in COMPARE_FIELDS:
                client_field = f"client_{field}"
                live_field = f"live_{field}"
                if row[client_field] != row[live_field]:
                    mismatches.append(
                        f"item_id={row['item_id']} field={field} "
                        f"client={row[client_field]!r} live={row[live_field]!r}"
                    )
    print(f"verify-live compared_items={compared}")
    if compared == 0:
        raise ExportCLIError(
            f"verify-live compared 0 items for house {house!r}; check the house, client build, and overlap"
        )
    for mismatch in mismatches:
        print(mismatch)
    return mismatches


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--tag", default=IMAGE)
    export = subparsers.add_parser("export")
    export.add_argument(
        "--install",
        help="parent game installation root (defaults to the host config's game_root)",
    )
    export.add_argument("--out-root")
    export.add_argument("--image", default=IMAGE)
    load = subparsers.add_parser("load")
    load.add_argument("--export", required=True)
    load.add_argument("--database-url", required=True)
    verify = subparsers.add_parser("verify-live")
    verify.add_argument("--database-url", required=True)
    verify.add_argument("--house", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            build_image(tag=args.tag)
            return 0
        if args.command == "export":
            install = args.install or load_host_config(host_config_path()).game_root
            path = export_client_data(install, args.out_root, image=args.image)
            print(path)
            return 0
        if args.command == "load":
            print(json.dumps(load_export(args.export, args.database_url), sort_keys=True))
            return 0
        mismatches = verify_live(args.database_url, args.house)
        return 1 if mismatches else 0
    except (ClientDataError, ExportCLIError, OSError, json.JSONDecodeError, psycopg.Error) as exc:
        print(f"client-data: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
