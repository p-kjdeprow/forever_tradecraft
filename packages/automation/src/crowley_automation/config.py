"""Strict standalone desktop configuration; it contains no scanner target resolver."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import cast

from crowley_common.house import House
from crowley_common.platform.paths import XdgDirs
from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_BUILD = re.compile(r"\d+\.\d+\.\d+\.\d+\Z")


class AutomationConfigError(ValueError):
    """The standalone automation configuration is unavailable or invalid."""


class AutomationConfig(BaseModel):
    """Pinned game identity and desktop facts needed without a scanner or catalog."""

    model_config = ConfigDict(extra="forbid", strict=True)

    wine_prefix: StrictStr
    game_root: StrictStr
    display: StrictStr
    window_title: StrictStr
    game_executable: StrictStr
    game_argv0: StrictStr
    window_class: StrictStr
    character: StrictStr
    realm: StrictStr
    faction: StrictStr
    house: StrictStr
    build: StrictStr
    interface: StrictInt
    native_region: StrictInt
    canvas_crop: tuple[StrictInt, StrictInt, StrictInt, StrictInt]

    @field_validator("wine_prefix", "game_root")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = Path(value)
        if not value or not path.is_absolute() or path == Path("/") or ".." in path.parts:
            raise ValueError("path must be absolute, non-root, and contain no traversal")
        return value

    @field_validator(
        "display", "window_title", "game_executable", "game_argv0", "window_class", "realm", "faction"
    )
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("value must be a non-empty string")
        return value

    @field_validator("character")
    @classmethod
    def validate_character(cls, value: str) -> str:
        if _COMPONENT.fullmatch(value) is None:
            raise ValueError("character must be one literal state-directory component")
        return value

    @field_validator("house")
    @classmethod
    def validate_house(cls, value: str) -> str:
        try:
            House.parse(value)
        except ValueError as exc:
            raise ValueError(f"house is not a supported region-ruleset-faction key: {exc}") from exc
        return value

    @field_validator("build")
    @classmethod
    def validate_build(cls, value: str) -> str:
        if _BUILD.fullmatch(value) is None:
            raise ValueError("build must have version.build form")
        return value

    @field_validator("interface", "native_region")
    @classmethod
    def validate_positive_identity(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("client identity integers must be positive")
        return value

    @field_validator("canvas_crop", mode="before")
    @classmethod
    def validate_crop(cls, value: object) -> tuple[int, int, int, int]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("canvas_crop must contain exactly four integers")
        if any(type(item) is not int for item in cast(tuple[object, ...], value)):
            raise ValueError("canvas_crop must contain exactly four integers")
        left, top, width, height = cast(tuple[int, int, int, int], tuple(value))
        if left < 0 or top < 0 or width != 256 or height != 256:
            raise ValueError("canvas_crop must be a 256x256 rectangle at non-negative coordinates")
        return left, top, width, height

    @model_validator(mode="after")
    def validate_house_faction(self) -> AutomationConfig:
        if House.parse(self.house).faction != self.faction.lower():
            raise ValueError("house faction must match configured faction")
        return self


def config_path(environ: dict[str, str] | None = None) -> Path:
    """Return the standalone automation TOML path."""

    source = os.environ if environ is None else environ
    try:
        return XdgDirs.from_environ(source).crowley_config / "automation.toml"
    except ValueError as exc:
        raise AutomationConfigError(f"XDG directories are unavailable: {exc}") from exc


def load_config(path: str | Path | None = None) -> AutomationConfig:
    source = config_path() if path is None else Path(path)
    try:
        with source.open("rb") as stream:
            raw = cast(object, tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AutomationConfigError(f"automation config {source} is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"automation"} or not isinstance(raw["automation"], dict):
        raise AutomationConfigError(f"{source} must contain only an [automation] table")
    try:
        return AutomationConfig.model_validate(raw["automation"])
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}" for error in exc.errors()
        )
        raise AutomationConfigError(f"automation config is invalid: {details}") from exc


def render_automation_toml(config: AutomationConfig) -> str:
    """Render a standalone automation config from the validated install contract."""

    values = config.model_dump()
    rows = ["[automation]"]
    for key in (
        "wine_prefix",
        "game_root",
        "display",
        "window_title",
        "game_executable",
        "game_argv0",
        "window_class",
        "character",
        "realm",
        "faction",
        "house",
        "build",
    ):
        rows.append(f"{key} = {json.dumps(values[key], ensure_ascii=False)}")
    for key in ("interface", "native_region"):
        rows.append(f"{key} = {values[key]}")
    rows.append(f"canvas_crop = {json.dumps(values['canvas_crop'])}")
    return "\n".join(rows) + "\n"


__all__ = [
    "AutomationConfig",
    "AutomationConfigError",
    "config_path",
    "load_config",
    "render_automation_toml",
]
