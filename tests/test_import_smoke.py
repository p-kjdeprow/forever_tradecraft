"""Every console script and shared GUI module imports, and every CLI answers ``--help``."""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import subprocess
import sys
import tomllib
from pathlib import Path

import crowley_automation.gui
import crowley_host.scan
import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILES = (
    ROOT / "pyproject.toml",
    *(ROOT / "packages" / name / "pyproject.toml" for name in ("common", "market", "scanner", "automation")),
)
PROJECT_SCRIPTS = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
SCRIPTS = {
    name: entry
    for path in PROJECT_FILES
    for name, entry in tomllib.loads(path.read_text(encoding="utf-8"))["project"].get("scripts", {}).items()
}
GUI_MODULES = sorted(
    f"crowley_automation.gui.{info.name}" for info in pkgutil.iter_modules(crowley_automation.gui.__path__)
)
SCAN_MODULES = sorted(
    f"crowley_host.scan.{info.name}" for info in pkgutil.iter_modules(crowley_host.scan.__path__)
)


def test_scripts_are_declared() -> None:
    assert SCRIPTS


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_console_script_entry_point_imports(script: str) -> None:
    module_name, _, attribute = SCRIPTS[script].partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute))


@pytest.mark.parametrize(
    "module_name",
    [
        "crowley_automation.battle_net",
        "crowley_automation.config",
        "crowley_automation.desktop_cli",
        "crowley_automation.desktop_input",
        "crowley_automation.desktop_observe",
        "crowley_automation.gui",
        "crowley_common.frames",
        "crowley_host.scan",
        *GUI_MODULES,
        *SCAN_MODULES,
    ],
)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_scan_entry_point_and_modules_are_listed() -> None:
    assert SCRIPTS["crowley-host"] == "crowley_host.scan.cli:main"
    assert {
        "crowley_host.scan.act",
        "crowley_host.scan.cli",
        "crowley_host.scan.config",
        "crowley_host.scan.loop",
        "crowley_host.scan.observe",
        "crowley_host.scan.policy",
    } <= set(SCAN_MODULES)


def test_gui_modules_are_listed() -> None:
    assert {
        "crowley_automation.gui.capture",
        "crowley_automation.gui.click_gate",
        "crowley_automation.gui.input_tool",
        "crowley_automation.gui.keyboard",
        "crowley_automation.gui.pointer",
        "crowley_automation.gui.process",
    } <= set(GUI_MODULES)


@pytest.mark.parametrize("script", sorted(PROJECT_SCRIPTS))
def test_console_script_help_exits_zero(script: str) -> None:
    executable = Path(sys.executable).parent / script
    assert executable.is_file(), f"console script {script} is not installed next to the interpreter"
    completed = subprocess.run(
        [str(executable), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()


def test_session_stack_is_gone() -> None:
    assert importlib.util.find_spec("crowley") is None
    assert set(SCRIPTS) == {
        "crowley-server",
        "crowley-companion",
        "crowley-watchlist",
        "crowley-host",
        "crowley-ops",
        "crowley-automation",
        "crowley-desktop",
        "crowley-native",
    }
    assert importlib.util.find_spec("crowley") is None
