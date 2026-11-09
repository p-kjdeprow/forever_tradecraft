"""Every console script and shared GUI module imports, and every CLI answers ``--help``."""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import crowley.gui

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS: dict[str, str] = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
    "scripts"
]
GUI_MODULES = sorted(f"crowley.gui.{info.name}" for info in pkgutil.iter_modules(crowley.gui.__path__))


def test_scripts_are_declared() -> None:
    assert SCRIPTS


@pytest.mark.parametrize("script", sorted(SCRIPTS))
def test_console_script_entry_point_imports(script: str) -> None:
    module_name, _, attribute = SCRIPTS[script].partition(":")
    module = importlib.import_module(module_name)
    assert callable(getattr(module, attribute))


@pytest.mark.parametrize("module_name", ["crowley.ops.battle_net", "crowley.gui", *GUI_MODULES])
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_gui_modules_are_listed() -> None:
    assert {
        "crowley.gui.capture",
        "crowley.gui.click_gate",
        "crowley.gui.frames",
        "crowley.gui.input_tool",
        "crowley.gui.keyboard",
        "crowley.gui.pointer",
        "crowley.gui.process",
    } <= set(GUI_MODULES)


# crowley-paper-step is configured only through the environment and has no argument parser.
NO_ARGUMENT_PARSER = frozenset({"crowley-paper-step"})


@pytest.mark.parametrize("script", sorted(set(SCRIPTS) - NO_ARGUMENT_PARSER))
def test_console_script_help_exits_zero(script: str) -> None:
    executable = Path(sys.executable).parent / script
    assert executable.is_file(), f"console script {script} is not installed next to the interpreter"
    completed = subprocess.run(
        [str(executable), "--help"], capture_output=True, text=True, check=False, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip()
