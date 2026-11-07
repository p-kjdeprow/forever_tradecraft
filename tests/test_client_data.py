"""Synthetic, non-client-data tests for the DB2 normalizer and CLI guards."""

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import shutil
import struct
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from crowley.core.client_data import ClientDataError, compare_profession_pages, normalize_export
from crowley.core.watchlist import Catalog, CatalogItem

DB_URL = os.environ.get("CROWLEY_TEST_DATABASE_URL")

FIXTURE = Path(__file__).parent / "fixtures" / "client_data"


def _write_valid_fixture_catalog(path: Path, item_ids: tuple[int, ...] = (999,)) -> Path:
    catalog = Catalog(
        schema_version=1,
        id="first-run",
        revision=1,
        client_build="1.60.1.70450",
        reviewed_on="2026-11-07",
        initial_price_levels=1,
        items=[
            CatalogItem(
                item_id=item_id,
                name=f"Fixture item {item_id}",
                category="trade_goods",
                tags=["fixture"],
                professions=["alchemy"],
                roles=["input"],
                status="active",
                pool="core",
                reason="Synthetic fixture item",
                evidence="client_data",
                auction_eligibility="client_tradeable",
                binding="none_in_tooltip",
                source_urls=[f"https://example.com/items/{item_id}"],
            )
            for item_id in item_ids
        ],
    )
    path.write_text(catalog.model_dump_json() + "\n")
    return path


def _fixture_export(tmp_path: Path, suffix: str = "b") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    export = tmp_path / f"1.60.1.70450-{suffix * 12}"
    shutil.copytree(FIXTURE, export)
    if suffix != "b":
        manifest = json.loads((export / "manifest.json").read_text())
        manifest["dbcache_sha256"] = suffix * 64
        manifest["export_key"] = f"1.60.1.70450-{suffix * 12}"
        (export / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return export


def _refresh_manifest(export: Path) -> None:
    import csv
    import hashlib

    module = importlib.import_module("tools.client_data_export")
    manifest = json.loads((export / "manifest.json").read_text())
    for table in module.TABLES:
        path = export / "csv" / f"{table}.csv"
        with path.open(newline="") as handle:
            rows = list(csv.reader(handle))
        manifest["tables"][table]["finalRows"] = len(rows) - 1
        manifest["tables"][table]["baseRows"] = len(rows) - 1
        manifest["tables"][table]["columns"] = len(rows[0])
        manifest["tables"][table]["csvSha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (export / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def test_normalizer_preserves_recipe_facts_and_item_coverage(tmp_path: Path) -> None:
    data = normalize_export(FIXTURE, catalog_path=_write_valid_fixture_catalog(tmp_path / "catalog.json"))
    by_spell = {recipe.spell_id: recipe for recipe in data.recipes}

    assert set(by_spell) == {100, 101, 102}
    assert by_spell[100].kind == "item"
    assert by_spell[100].outputs[0].base_quantity == 1
    assert by_spell[100].reagents == (type(by_spell[100].reagents[0])(300, 2),)
    assert by_spell[100].cooldown_ms == 60000
    assert by_spell[100].category_cooldown_ms == 120000
    assert by_spell[100].category_name == "Test Category"
    assert by_spell[100].teaching_items == (400,)
    assert by_spell[100].learn_kind == "recipe_item"
    assert by_spell[100].learn_evidence == "teaching_item_with_item_data"
    assert by_spell[100].totems == (99,)
    assert by_spell[100].totem_categories == (12,)
    assert by_spell[100].spell_focus == 88
    assert len(data.recipe_associations) == 4
    assert data.recipes_by_profession["alchemy"][0].spell_id == 100
    assert data.recipes_by_profession["engineering"][0].spell_id == 100

    assert by_spell[101].kind == "non_item"
    assert by_spell[101].outputs == ()
    assert by_spell[101].learn_evidence == "no_teaching_item"
    assert by_spell[102].teaching_items == (401,)
    assert by_spell[102].learn_evidence == "teaching_item_without_item_data"
    assert by_spell[102].outputs[0].base_quantity == 2.5
    assert by_spell[102].outputs[0].variance == 0.25
    assert by_spell[102].outputs[0].yield_kind == "random"
    assert data.random_yield_count == 1
    assert data.no_sparse_row_count == 3
    assert {item.item_id for item in data.items} == {200, 201, 300, 400, 401, 999}
    assert next(item for item in data.items if item.item_id == 201).status == "no_sparse_row"


def test_recipe_selection_rejects_non_profession_and_non_create_rows(tmp_path: Path) -> None:
    broken = tmp_path / "export"
    shutil.copytree(FIXTURE, broken)
    _write_valid_fixture_catalog(broken / "catalog.json")
    ability = broken / "csv" / "SkillLineAbility.csv"
    ability.write_text(
        ability.read_text()
        + ",".join(["5", "", "", "999", "100"] + [""] * 15)
        + "\n"
        + ",".join(["6", "", "", "171", "999"] + [""] * 15)
        + "\n"
    )
    _refresh_manifest(broken)
    data = normalize_export(broken, catalog_path=broken / "catalog.json")
    assert {recipe.spell_id for recipe in data.recipes} == {100, 101, 102}


def test_profession_name_and_manifest_build_are_strict(tmp_path: Path) -> None:
    broken = tmp_path / "export"
    broken.mkdir()
    for source in FIXTURE.iterdir():
        if source.is_dir():
            import shutil

            shutil.copytree(source, broken / source.name)
        else:
            (broken / source.name).write_bytes(source.read_bytes())
    skillline = broken / "csv" / "SkillLine.csv"
    skillline.write_text(skillline.read_text().replace("Alchemy", "Alchemy Broken"))
    with pytest.raises(ClientDataError, match="SkillLine 171"):
        normalize_export(broken, catalog_path=broken / "catalog.json")

    skillline.write_text(skillline.read_text().replace("Alchemy Broken", "Alchemy"))
    manifest = json.loads((broken / "manifest.json").read_text())
    manifest["client_build"] = "wrong"
    (broken / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ClientDataError, match="client build"):
        normalize_export(broken, catalog_path=broken / "catalog.json")


def test_export_tables_require_exact_real_schema(tmp_path: Path) -> None:
    broken = tmp_path / "export"
    shutil.copytree(FIXTURE, broken)
    (broken / "csv" / "ItemXItemEffect.csv").unlink()
    with pytest.raises(ClientDataError, match="missing required table ItemXItemEffect.csv"):
        normalize_export(broken, catalog_path=broken / "catalog.json")

    shutil.copytree(FIXTURE, broken / "copy")
    header_path = broken / "copy" / "csv" / "ItemEffect.csv"
    _header, *rows = header_path.read_text().splitlines()
    header_path.write_text(
        "ID,WrongColumn,TriggerType,Charges,CoolDownMSec,CategoryCoolDownMSec,SpellCategoryID,SpellID,ChrSpecializationID,PlayerConditionID\n"
        + "\n".join(rows)
        + "\n"
    )
    with pytest.raises(ClientDataError, match="ItemEffect.csv has columns"):
        normalize_export(broken / "copy", catalog_path=broken / "copy" / "catalog.json")


def test_saved_profession_pages_are_a_report_only(tmp_path: Path) -> None:
    data = normalize_export(FIXTURE, catalog_path=_write_valid_fixture_catalog(tmp_path / "catalog.json"))
    page = tmp_path / "prof-alchemy.html"
    page.write_text('<ul><li class="rc" id="r-100"></li><li class="rc" id="r-999"></li></ul>')
    comparison = compare_profession_pages(data, {"alchemy": page})[0]
    assert comparison.client_count == 1
    assert comparison.expected_count == 2
    assert comparison.client_only == ()
    assert comparison.expected_only == (999,)
    assert comparison.evidence_counts == {
        "teaching_item_with_item_data": 1,
        "teaching_item_without_item_data": 0,
        "no_teaching_item": 0,
    }

    # A second association of the same spell on the same profession must not double-count evidence.
    recipe = next(r for r in data.recipes if r.spell_id == 100)
    extra = dataclasses.replace(recipe.associations[0], ability_id=recipe.associations[0].ability_id + 1)
    duplicated = dataclasses.replace(recipe, associations=(*recipe.associations, extra))
    doubled = dataclasses.replace(
        data, recipes=tuple(duplicated if r.spell_id == 100 else r for r in data.recipes)
    )
    assert len(doubled.recipes_by_profession["alchemy"]) == 2
    again = compare_profession_pages(doubled, {"alchemy": page})[0]
    assert again.client_count == 1
    assert sum(again.evidence_counts.values()) == again.client_count


def test_export_cli_checks_build_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("tools.client_data_export")
    install = tmp_path / "install"
    (install / "_classic_beta_" / "Cache" / "ADB" / "enUS").mkdir(parents=True)
    (install / ".build.info").write_text(
        "Branch!STRING:0|Active!DEC:1|Build Key!HEX:16|CDN Key!HEX:16|CDN Path!STRING:0|Version!STRING:0|Product!STRING:0\n"
        "us|1|build|cdn|tpr/wow|1.60.1.70450|wow_classic_beta\n"
    )
    (install / "_classic_beta_" / ".flavor.info").write_text("Product Flavor!STRING:0\nwow_classic_beta\n")
    dbcache = install / "_classic_beta_" / "Cache" / "ADB" / "enUS" / "DBCache.bin"
    dbcache.write_bytes(struct.pack("<4sii", b"XFTH", 9, 70450) + b"x")
    metadata = module.inspect_install(install)
    assert metadata.export_key.startswith("1.60.1.70450-")
    bad = install / ".build.info"
    bad.write_text(bad.read_text().replace("1.60.1.70450", "1.60.1.70532"))
    with pytest.raises(module.ExportCLIError, match="client build"):
        module.inspect_install(install)
    bad.write_text(bad.read_text().replace("1.60.1.70532", "1.60.1.70450"))

    output = tmp_path / "out"

    def fake_runner(command: list[str], check: bool) -> None:
        assert "--network" in command and command[command.index("--network") + 1] == "none"
        assert "--read-only" in command
        assert f"{install.resolve()}:/wow:ro" in command
        assert command[command.index("--expected-build") + 1] == module.EXPECTED_CLIENT_BUILD
        staging = Path(command[command.index("-v") + 3].split(":", 1)[0])
        shutil.copytree(FIXTURE, staging, dirs_exist_ok=True)
        manifest = json.loads((staging / "manifest.json").read_text())
        manifest.update(
            {
                "export_key": metadata.export_key,
                "build_key": metadata.build_key,
                "cdn_key": metadata.cdn_key,
                "build_info_sha256": metadata.build_info_sha256,
                "dbcache_sha256": metadata.dbcache_sha256,
                "dbcache_build": metadata.dbcache_build,
            }
        )
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    first = module.export_client_data(install, output, runner=fake_runner)
    assert first.is_dir()
    with pytest.raises(module.ExportCLIError, match="overwrite"):
        module.export_client_data(install, output, runner=fake_runner)
    assert (first / "manifest.json").stat().st_mode & 0o777 == 0o600

    def wrong_source_runner(command: list[str], check: bool) -> None:
        staging = Path(command[command.index("-v") + 3].split(":", 1)[0])
        shutil.copytree(FIXTURE, staging, dirs_exist_ok=True)
        manifest = json.loads((staging / "manifest.json").read_text())
        manifest["export_key"] = metadata.export_key
        manifest["build_key"] = "wrong-build"
        manifest["build_info_sha256"] = metadata.build_info_sha256
        manifest["dbcache_sha256"] = metadata.dbcache_sha256
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(module.ExportCLIError, match="source identity"):
        module.export_client_data(install, tmp_path / "source-mismatch", runner=wrong_source_runner)

    with pytest.raises(module.ExportCLIError, match="inside the game install"):
        module.export_client_data(install, install / "exports", runner=fake_runner)
    install_link = tmp_path / "install-link"
    install_link.symlink_to(install, target_is_directory=True)
    with pytest.raises(module.ExportCLIError, match="inside the game install"):
        module.export_client_data(install, install_link / "exports", runner=fake_runner)
    with pytest.raises(module.ExportCLIError, match="inside the repository"):
        module.export_client_data(install, Path(__file__).parents[1] / "export-test", runner=fake_runner)
    # Any other git working tree is refused too, including a linked worktree of it.
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
    with pytest.raises(module.ExportCLIError, match="inside the repository"):
        module.export_client_data(install, other_repo / "nested" / "exports", runner=fake_runner)
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    linked_worktree = tmp_path / "linked-worktree"
    subprocess.run(["git", "-C", str(other_repo), "worktree", "add", "-q", str(linked_worktree)], check=True)
    with pytest.raises(module.ExportCLIError, match="inside the repository"):
        module.export_client_data(install, linked_worktree / "exports", runner=fake_runner)
    assert not (other_repo / "nested").exists()
    assert not (linked_worktree / "exports").exists()

    corrupted = _fixture_export(tmp_path, "c")
    csv_path = corrupted / "csv" / "Item.csv"
    csv_path.write_bytes(csv_path.read_bytes() + b"\n")
    with pytest.raises(module.ExportCLIError, match="CSV hash mismatch"):
        module.load_export(corrupted, "postgresql://not-used")


def test_migration_contains_typed_tables_and_latest_view() -> None:
    sql = (Path(__file__).parents[1] / "src/crowley/server/migrations/005_client_data.sql").read_text()
    for name in (
        "client_data_exports",
        "client_recipes",
        "client_recipe_skill_lines",
        "client_recipe_reagents",
        "client_recipe_outputs",
        "client_recipe_teachers",
        "client_recipe_totems",
        "client_recipe_totem_categories",
        "client_recipe_spell_focus",
        "client_items",
        "latest_client_export",
    ):
        assert name in sql


@pytest.mark.skipif(not DB_URL, reason="CROWLEY_TEST_DATABASE_URL not set")
def test_loader_is_typed_idempotent_conflict_safe_and_verifiable(tmp_path: Path) -> None:
    module = importlib.import_module("tools.client_data_export")
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    export = _fixture_export(tmp_path)
    first = module.load_export(export, DB_URL)
    duplicate = module.load_export(export, DB_URL)
    assert first["status"] == "created"
    assert duplicate["status"] == "duplicate"
    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("SELECT count(*) FROM client_recipes").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM client_recipe_skill_lines").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM client_recipe_totems WHERE spell_id=100").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT category_id FROM client_recipe_totem_categories WHERE spell_id=100"
            ).fetchone()[0]
            == 12
        )
        assert (
            conn.execute(
                "SELECT spell_focus_id FROM client_recipe_spell_focus WHERE spell_id=100"
            ).fetchone()[0]
            == 88
        )
        assert conn.execute("SELECT count(*) FROM latest_client_export").fetchone()[0] == 1
        assert conn.execute("SELECT count FROM client_recipe_reagents WHERE spell_id=100").fetchone()[0] == 2

    conflicting = _fixture_export(tmp_path / "conflict")
    manifest = json.loads((conflicting / "manifest.json").read_text())
    manifest["build_key"] = "different"
    (conflicting / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(module.ExportConflictError, match="different manifest"):
        module.load_export(conflicting, DB_URL)

    concurrent_export = _fixture_export(tmp_path / "concurrent", "d")
    concurrent_manifest = json.loads((concurrent_export / "manifest.json").read_text())
    concurrent_manifest["dbcache_sha256"] = "d" * 64
    concurrent_manifest["export_key"] = "1.60.1.70450-" + "d" * 12
    (concurrent_export / "manifest.json").write_text(json.dumps(concurrent_manifest, indent=2) + "\n")
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent_results = list(pool.map(lambda _: module.load_export(concurrent_export, DB_URL), range(2)))
    assert sorted(result["status"] for result in concurrent_results) == ["created", "duplicate"]

    rollback_export = _fixture_export(tmp_path / "rollback", "e")
    ability = rollback_export / "csv" / "SkillLineAbility.csv"
    ability.write_text(ability.read_text() + ability.read_text().splitlines()[1] + "\n")
    _refresh_manifest(rollback_export)
    with pytest.raises(psycopg.Error):
        module.load_export(rollback_export, DB_URL)
    with psycopg.connect(DB_URL) as conn:
        assert conn.execute("SELECT count(*) FROM client_data_exports").fetchone()[0] == 2

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        house_id = conn.execute(
            "INSERT INTO houses (key, region, ruleset, faction) VALUES ('us-normal-horde','us','normal','horde') RETURNING id"
        ).fetchone()[0]
        capture_id = conn.execute(
            """
            INSERT INTO item_reference_captures
              (capture_uid, house_id, client_build, started_at, finished_at, ah_open, context, raw, content_digest)
            VALUES ('client-data-test', %s, '1.60.1.70450', now(), now(), true, '{}', '{}', %s)
            RETURNING id
            """,
            (house_id, "a" * 64),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO item_reference
              (capture_id, item_id, status, quality, item_level, class_id, subclass_id, max_stack, sell_price, bind_type)
            VALUES (%s, 200, 'ok', 1, 10, 0, 1, 20, 5, 0)
            """,
            (capture_id,),
        )
    assert module.verify_live(DB_URL, "us-normal-horde") == []
    live_fields = (
        ("sell_price", "sell_price", 6, 5),
        ("max_stack", "max_stack", 21, 20),
        ("bonding", "bind_type", 1, 0),
        ("class_id", "class_id", 1, 0),
        ("subclass_id", "subclass_id", 2, 1),
        ("quality", "quality", 2, 1),
        ("item_level", "item_level", 11, 10),
    )
    for field, column, value, expected in live_fields:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute(
                f"UPDATE item_reference SET {column}=%s WHERE capture_id=%s AND item_id=200",
                (value, capture_id),
            )
        mismatch = module.verify_live(DB_URL, "us-normal-horde")
        assert any(f"field={field}" in item for item in mismatch)
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute(
                f"UPDATE item_reference SET {column}=%s WHERE capture_id=%s AND item_id=200",
                (expected, capture_id),
            )
    with pytest.raises(module.ExportCLIError, match="compared 0 items"):
        module.verify_live(DB_URL, "unknown-house")
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        no_overlap_house = conn.execute(
            "INSERT INTO houses (key, region, ruleset, faction) VALUES ('no-overlap','eu','normal','horde') RETURNING id"
        ).fetchone()[0]
        no_overlap_capture = conn.execute(
            """
            INSERT INTO item_reference_captures
              (capture_uid, house_id, client_build, started_at, finished_at, ah_open, context, raw, content_digest)
            VALUES ('no-overlap-capture', %s, '1.60.1.70450', now(), now(), true, '{}', '{}', %s)
            RETURNING id
            """,
            (no_overlap_house, "b" * 64),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO item_reference
              (capture_id, item_id, status, quality, item_level, class_id, subclass_id, max_stack, sell_price, bind_type)
            VALUES (%s, 888, 'ok', 1, 10, 0, 1, 20, 5, 0)
            """,
            (no_overlap_capture,),
        )
        wrong_build_house = conn.execute(
            "INSERT INTO houses (key, region, ruleset, faction) VALUES ('wrong-build','us','normal','alliance') RETURNING id"
        ).fetchone()[0]
        wrong_build_capture = conn.execute(
            """
            INSERT INTO item_reference_captures
              (capture_uid, house_id, client_build, started_at, finished_at, ah_open, context, raw, content_digest)
            VALUES ('wrong-build-capture', %s, '1.60.1.70449', now(), now(), true, '{}', '{}', %s)
            RETURNING id
            """,
            (wrong_build_house, "c" * 64),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO item_reference
              (capture_id, item_id, status, quality, item_level, class_id, subclass_id, max_stack, sell_price, bind_type)
            VALUES (%s, 200, 'ok', 1, 10, 0, 1, 20, 5, 0)
            """,
            (wrong_build_capture,),
        )
    with pytest.raises(module.ExportCLIError, match="compared 0 items"):
        module.verify_live(DB_URL, "no-overlap")
    with pytest.raises(module.ExportCLIError, match="compared 0 items"):
        module.verify_live(DB_URL, "wrong-build")
