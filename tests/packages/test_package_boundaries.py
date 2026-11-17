from __future__ import annotations

import ast
import os
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

_REPOSITORY = Path(__file__).resolve().parents[2]
_PACKAGES = _REPOSITORY / "packages"


def test_scanning_and_startup_have_no_model_clients_or_agent_launches() -> None:
    roots = [
        _REPOSITORY / "apps/host/src/crowley_host/scan",
        _PACKAGES / "scanner/src/crowley_scanner",
    ]
    sources = [path for root in roots for path in root.rglob("*.py")]
    sources.extend((_PACKAGES / "automation/src/crowley_automation").glob("startup*.py"))
    forbidden = {"openai", "anthropic", "litellm", "langchain", "ollama", "codex", "claude"}
    for path in sources:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {alias.name.split(".")[0] for alias in node.names} & forbidden, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, path
            elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    assert Path(first.value).name not in forbidden, path


def _source_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _wheel_files(archive: ZipFile, namespace: str) -> dict[str, bytes]:
    prefix = namespace + "/"
    return {
        name[len(prefix) :]: archive.read(name)
        for name in archive.namelist()
        if name.startswith(prefix) and not name.endswith("/")
    }


def _sdist_files(archive: tarfile.TarFile, namespace: str) -> dict[str, bytes]:
    marker = f"/src/{namespace}/"
    result: dict[str, bytes] = {}
    for member in archive.getmembers():
        if marker not in member.name or not member.isfile():
            continue
        extracted = archive.extractfile(member)
        assert extracted is not None
        result[member.name.split(marker, 1)[1]] = extracted.read()
    return result


def _import_roots(*names: str) -> list[str]:
    return [str(_PACKAGES / name / "src") for name in names]


def _isolated_python(source: str, *packages: str) -> subprocess.CompletedProcess[str]:
    roots = _import_roots(*packages)
    setup = f"import sys; sys.path[:0] = {roots!r}; "
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-I", "-c", setup + source],
        cwd="/",
        env=env,
        capture_output=True,
        check=True,
        text=True,
    )


def test_common_market_and_scanner_import_from_only_their_new_sources() -> None:
    market = _isolated_python(
        "from crowley_common.game_target import selected_target; "
        "from crowley_common.watchlist import load_catalog; "
        "from crowley_market.server.app import app; "
        "from crowley_market.server.db import MigrationManifest; "
        "from crowley_market.backup import _packaged_migrations; "
        "assert selected_target().target_id == 'forever-us-camwow7'; "
        "assert load_catalog().id == 'first-run'; "
        "assert len(MigrationManifest.from_package().migrations) == 12; "
        "assert len(_packaged_migrations()) == 12; "
        "assert app.title == 'Crowley'",
        "common",
        "market",
    )
    assert market.returncode == 0

    scanner = _isolated_python(
        "from crowley_common.game_target import selected_target; "
        "from crowley_scanner.companion.config import ScannerConfig, render_scanner_toml; "
        "from crowley_scanner.companion.sync import Companion; "
        "config = ScannerConfig.model_validate({'game_root': '/opt/game', 'wtf_account': 'ACCOUNT', "
        "'character': 'Tester', 'server_url': 'https://scanner.example.invalid'}); "
        "rendered = render_scanner_toml(config); "
        "assert '[scanner]' in rendered and 'character = \\\"Tester\\\"' in rendered; "
        "assert selected_target().product_dir == '_forever_'; "
        "assert Companion.__name__ == 'Companion'",
        "common",
        "scanner",
    )
    assert scanner.returncode == 0


def test_imports_follow_component_boundaries() -> None:
    forbidden = {
        "common": ("crowley", "crowley_market", "crowley_scanner", "crowley_automation", "crowley_host"),
        "market": ("crowley", "crowley_scanner", "crowley_automation", "crowley_host"),
        "scanner": ("crowley", "crowley_market", "crowley_automation", "crowley_host"),
        "automation": ("crowley", "crowley_market", "crowley_scanner", "crowley_host"),
    }
    for component, roots in forbidden.items():
        source_root = _PACKAGES / component / "src"
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    assert not any(name == root or name.startswith(root + ".") for root in roots), (
                        component,
                        path,
                        name,
                    )
                    if component == "automation":
                        assert name not in {"crowley_common.game_target", "crowley_common.watchlist"}, (
                            path,
                            "automation must not resolve the scanner target/catalog",
                            name,
                        )


def test_distribution_dependencies_match_component_ownership() -> None:
    metadata = {
        name: tomllib.loads((_PACKAGES / name / "pyproject.toml").read_text(encoding="utf-8"))
        for name in ("common", "market", "scanner")
    }
    dependencies = {
        name: {
            dependency.split(" ", 1)[0].split("[", 1)[0] for dependency in project["project"]["dependencies"]
        }
        for name, project in metadata.items()
    }
    assert dependencies["common"] == {"pydantic>=2.14.1,<3"}
    assert "crowley-common>=0.2,<0.3" in metadata["market"]["project"]["dependencies"]
    assert "crowley-common>=0.2,<0.3" in metadata["scanner"]["project"]["dependencies"]
    assert not any("scanner" in name or "automation" in name for name in dependencies["market"])
    assert not any("market" in name or "automation" in name for name in dependencies["scanner"])


def test_frame_decoder_and_scanner_addons_have_their_final_package_owners() -> None:
    assert (_PACKAGES / "common/src/crowley_common/frames.py").is_file()
    addon_root = _PACKAGES / "scanner/src/crowley_scanner/addon"
    assert {path.name for path in addon_root.iterdir() if path.is_dir()} == {"Crowley", "Crowley_Data"}


def test_wheels_and_sdists_contain_only_owned_component_resources(tmp_path: Path) -> None:
    expected = {
        "common": {
            "namespace": "crowley_common",
            "required": {
                "crowley_common/data/first_run.json",
                "crowley_common/data/targets/selected",
                "crowley_common/data/targets/forever-us-camwow7.json",
            },
            "forbidden": ("crowley_market/", "crowley_scanner/", "verified-beta.json"),
        },
        "market": {
            "namespace": "crowley_market",
            "required": {
                "crowley_market/server/migrations/001_init.sql",
                "crowley_market/server/static/trading-dashboard.html",
                "crowley_market/resources/docker-compose.yml",
            },
            "forbidden": ("crowley_common/", "crowley_scanner/", "Crowley/"),
        },
        "scanner": {
            "namespace": "crowley_scanner",
            "required": {
                "crowley_scanner/state.py",
                "crowley_scanner/addon/Crowley/Crowley.toc",
                "crowley_scanner/addon/Crowley_Data/Crowley_Data.toc",
            },
            "forbidden": ("crowley_market/", "crowley_common/", "Crowley_Automation/"),
        },
        "automation": {
            "namespace": "crowley_automation",
            "required": {
                "crowley_automation/model.py",
                "crowley_automation/runner.py",
                "crowley_automation/addon/Crowley_Automation/Crowley_Automation.toc",
                "crowley_automation/addon/Crowley_Host/Crowley_Host.toc",
                "crowley_automation/native/resources/vendor/xdotool-source.tar.gz",
            },
            "forbidden": (
                "crowley_market/",
                "crowley_scanner/",
                "crowley_host/",
                "/Crowley/",
                "/Crowley_Data/",
            ),
        },
    }
    wheels: list[Path] = []
    for component, check in expected.items():
        output = tmp_path / component
        output.mkdir()
        subprocess.run(
            ["uv", "build", "--directory", str(_PACKAGES / component), "--out-dir", str(output)],
            cwd="/",
            check=True,
            capture_output=True,
            text=True,
        )
        wheel = next(output.glob("*.whl"))
        sdist = next(output.glob("*.tar.gz"))
        wheels.append(wheel)
        source_files = _source_files(_PACKAGES / component / "src" / check["namespace"])
        with ZipFile(wheel) as archive:
            members = set(archive.namelist())
            assert check["required"] <= members
            assert not any(any(part in member for part in check["forbidden"]) for member in members)
            assert all(
                member.startswith(check["namespace"] + "/") or ".dist-info/" in member for member in members
            )
            assert _wheel_files(archive, check["namespace"]) == source_files
        with tarfile.open(sdist, "r:gz") as archive:
            members = set(archive.getnames())
            assert _sdist_files(archive, check["namespace"]) == source_files
        assert any(member.endswith("/" + next(iter(check["required"]))) for member in members)
        assert all(
            f"/src/{check['namespace']}/" in member
            or member.endswith(("/pyproject.toml", "/PKG-INFO", "/.gitignore"))
            for member in members
        )

    install = tmp_path / "wheel-install"
    subprocess.run(
        ["uv", "pip", "install", "--target", str(install), "--no-deps", *(str(path) for path in wheels)],
        cwd="/",
        check=True,
        capture_output=True,
        text=True,
    )
    installed_smoke = (
        "import importlib.resources as resources, sys; "
        f"sys.path.insert(0, {str(install)!r}); "
        "from crowley_common.game_target import selected_target; "
        "from crowley_common.watchlist import load_catalog; "
        "from crowley_market.server.db import MigrationManifest; "
        "from crowley_market.server.app import app; "
        "from crowley_market.backup import _packaged_resources; "
        "from crowley_scanner.state import STATE_VERSION; "
        "assert selected_target().target_id == 'forever-us-camwow7'; "
        "assert load_catalog().id == 'first-run'; "
        "assert len(MigrationManifest.from_package().migrations) == 12; "
        "assert app.title == 'Crowley' and STATE_VERSION == 3; "
        "\nwith _packaged_resources() as packaged:\n"
        "    assert packaged.package_name == 'crowley-market' and packaged.compose_path.is_file()\n"
        "    assert resources.files('crowley_scanner').joinpath('addon/Crowley/Crowley.toc').is_file()\n"
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    subprocess.run(
        [sys.executable, "-I", "-c", installed_smoke],
        cwd="/",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
