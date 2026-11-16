from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

_REPOSITORY = Path(__file__).resolve().parents[2]
_COMPONENTS = {
    "common": "crowley_common",
    "market": "crowley_market",
    "scanner": "crowley_scanner",
    "automation": "crowley_automation",
}
_HOST = "crowley_host"
_SCRIPTS = (
    "crowley-host",
    "crowley-ops",
    "crowley-watchlist",
    "crowley-server",
    "crowley-companion",
    "crowley-automation",
    "crowley-desktop",
    "crowley-native",
)


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd="/", env=env, check=True, capture_output=True, text=True)


def _wheel_files(path: Path, namespace: str) -> dict[str, bytes]:
    prefix = namespace + "/"
    with ZipFile(path) as archive:
        return {
            name[len(prefix) :]: archive.read(name)
            for name in archive.namelist()
            if name.startswith(prefix) and not name.endswith("/")
        }


def _source_files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def test_host_wheel_sdist_and_installed_console_scripts_are_self_contained(tmp_path: Path) -> None:
    assert not tmp_path.resolve().is_relative_to(_REPOSITORY)
    host_out = tmp_path / "host-source-build"
    host_out.mkdir()
    _run(
        [
            "uv",
            "build",
            "--wheel",
            "--sdist",
            "--no-sources",
            str(_REPOSITORY),
            "--out-dir",
            str(host_out),
        ]
    )
    host_source_wheel = next(host_out.glob("*.whl"))
    host_sdist = next(host_out.glob("*.tar.gz"))

    extracted = tmp_path / "sdist-extracted"
    extracted.mkdir()
    with tarfile.open(host_sdist, "r:gz") as archive:
        archive.extractall(extracted, filter="data")
    host_project = next(
        path.parent
        for path in extracted.rglob("pyproject.toml")
        if (path.parent / "apps/host/src/crowley_host").is_dir()
    )
    sdist_wheel_out = tmp_path / "sdist-wheel"
    sdist_wheel_out.mkdir()
    _run(
        [
            "uv",
            "build",
            "--wheel",
            "--no-sources",
            str(host_project),
            "--out-dir",
            str(sdist_wheel_out),
        ]
    )
    host_sdist_wheel = next(sdist_wheel_out.glob("*.whl"))
    host_source = _source_files(_REPOSITORY / "apps/host/src/crowley_host")
    assert _wheel_files(host_source_wheel, _HOST) == host_source
    assert _wheel_files(host_sdist_wheel, _HOST) == host_source

    wheels = {"host": host_sdist_wheel}
    for component in _COMPONENTS:
        output = tmp_path / f"{component}-wheel"
        output.mkdir()
        _run(
            [
                "uv",
                "build",
                "--wheel",
                "--no-sources",
                str(_REPOSITORY / "packages" / component),
                "--out-dir",
                str(output),
            ]
        )
        wheels[component] = next(output.glob("*.whl"))

    namespaces = {**_COMPONENTS, "host": _HOST}
    for component, wheel in wheels.items():
        namespace = namespaces[component]
        with ZipFile(wheel) as archive:
            assert all(
                name.startswith(namespace + "/") or ".dist-info/" in name for name in archive.namelist()
            )

    host_names = set(_wheel_files(wheels["host"], _HOST))
    assert "ops/package_data/config/host.example.toml" in host_names
    assert {
        "ops/package_data/systemd/crowley-companion.service.in",
        "ops/package_data/systemd/crowley-health.service.in",
        "ops/package_data/systemd/crowley-health.timer",
        "ops/package_data/systemd/crowley-host.service.in",
        "ops/package_data/systemd/crowley-market-backup.service.in",
        "ops/package_data/systemd/crowley-market-backup.timer",
    } == {path for path in host_names if path.startswith("ops/package_data/systemd/")}

    owned = {
        component: _wheel_files(wheel, _HOST if component == "host" else _COMPONENTS[component])
        for component, wheel in wheels.items()
    }
    launcher = "resources/launcher/crowley-world-of-warcraft.sh.in"
    scanner_files = {
        "addon/Crowley/Crowley.toc",
        "addon/Crowley_Data/Crowley_Data.toc",
    }
    market_files = {
        "server/migrations/001_init.sql",
        "server/migrations/012_paper_engine_state.sql",
        "server/static/trading-dashboard.html",
    }
    assert scanner_files <= owned["scanner"].keys()
    assert not any(path.startswith(("addon/Crowley/", "addon/Crowley_Data/")) for path in owned["automation"])
    assert market_files <= owned["market"].keys()
    assert (
        len(
            [
                path
                for path in owned["market"]
                if path.startswith("server/migrations/") and path.endswith(".sql")
            ]
        )
        == 12
    )
    host_units = {path for path in host_names if path.startswith("ops/package_data/systemd/")}
    automation_resources = {launcher, "addon/Crowley_Automation/Mode.lua", "addon/Crowley_Host/Host.lua"}
    market_resources = market_files | {
        path for path in owned["market"] if path.startswith("server/migrations/") and path.endswith(".sql")
    }
    for owner, paths in (
        ("automation", automation_resources),
        ("scanner", scanner_files),
        ("market", market_resources),
        ("host", host_units),
    ):
        for path in paths:
            assert path in owned[owner]
            assert all(path not in files for name, files in owned.items() if name != owner), (owner, path)

    mode = owned["automation"]["addon/Crowley_Automation/Mode.lua"]
    assert b"ns.Coordinated = false" in mode and b"ns.Coordinated = true" not in mode

    venv = tmp_path / "installed"
    _run(["uv", "venv", "--python", sys.executable, str(venv)])
    python = venv / "bin/python"
    _run(["uv", "pip", "install", "--python", str(python), *(str(path) for path in wheels.values())])
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    environment.update(
        {
            "HOME": str(tmp_path / "empty-home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
            "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
        }
    )
    smoke = """
import importlib, importlib.resources, sys
from pathlib import Path
prefix = Path(sys.prefix).resolve()
resources = importlib.resources.files
for name in ('crowley_host', 'crowley_common', 'crowley_market', 'crowley_scanner', 'crowley_automation'):
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(prefix), (name, module.__file__)
    assert Path(resources(name)).resolve().is_relative_to(prefix), name
try:
    import crowley
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('legacy crowley namespace is installed')
assert resources('crowley_automation').joinpath('resources/launcher/crowley-world-of-warcraft.sh.in').is_file()
assert resources('crowley_scanner').joinpath('addon/Crowley/Crowley.toc').is_file()
assert resources('crowley_market').joinpath('server/migrations/012_paper_engine_state.sql').is_file()
assert resources('crowley_market').joinpath('server/static/trading-dashboard.html').is_file()
from crowley_host.ops.install import packaged_addon_files
addons = dict(packaged_addon_files())
assert b'ns.Coordinated = true' in addons['Crowley_Automation/Mode.lua']
assert 'Crowley_Host/Host.lua' in addons
"""
    _run([str(python), "-I", "-c", smoke], env=environment)

    for script in _SCRIPTS:
        result = _run([str(venv / "bin" / script), "--help"], env=environment)
        assert "usage:" in result.stdout.lower(), script

    for command in ("inspect", "preview", "start", "status", "revoke", "release-expired"):
        result = _run([str(venv / "bin/crowley-automation"), "seller", command, "--help"], env=environment)
        assert "usage:" in result.stdout.lower(), command
        if command in {"start", "release-expired"}:
            assert "--confirm-policy-id" in result.stdout and "--arm" in result.stdout
