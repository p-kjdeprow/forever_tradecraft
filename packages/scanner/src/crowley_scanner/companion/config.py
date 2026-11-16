"""Standalone scanner companion configuration."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from pathlib import Path
from typing import cast

from crowley_common.game_target import GameTarget, selected_target
from crowley_common.platform.paths import XdgDirs
from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    StrictFloat,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

_ACCOUNT_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]*\Z")
_CHARACTER_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _xdg_dirs() -> XdgDirs:
    try:
        return XdgDirs.from_environ(os.environ)
    except ValueError as exc:
        raise ValueError(f"XDG directories are unavailable: {exc}") from exc


class ScannerConfig(BaseModel):
    """The complete scanner TOML contract, independent of host GUI settings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    game_root: StrictStr
    wtf_account: StrictStr
    character: StrictStr
    server_url: StrictStr
    poll_seconds: StrictFloat = 3.0
    refresh_minutes: StrictFloat = 5.0

    @field_validator("game_root")
    @classmethod
    def validate_game_root(cls, value: str) -> str:
        path = Path(value)
        if not value or not path.is_absolute() or path == Path("/") or ".." in path.parts:
            raise ValueError("game_root must be an absolute non-root path without traversal")
        return value

    @field_validator("wtf_account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        if _ACCOUNT_COMPONENT.fullmatch(value) is None or value in {".", ".."}:
            raise ValueError("wtf_account must be one literal account directory name")
        return value

    @field_validator("character")
    @classmethod
    def validate_character(cls, value: str) -> str:
        if _CHARACTER_COMPONENT.fullmatch(value) is None or value in {".", ".."}:
            raise ValueError("character must be one literal state-directory component")
        return value

    @field_validator("server_url")
    @classmethod
    def validate_server_url(cls, value: str) -> str:
        if not value:
            raise ValueError("server_url must not be empty")
        return value

    @field_validator("poll_seconds", "refresh_minutes")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("scanner intervals must be finite and greater than zero")
        return value

    @model_validator(mode="after")
    def validate_refresh(self) -> ScannerConfig:
        if self.refresh_minutes * 60 < self.poll_seconds:
            raise ValueError("refresh_minutes must be at least one poll interval")
        return self


class CompanionSettings(ScannerConfig):
    """Validated scanner settings with the token and derived runtime paths."""

    api_token: SecretStr

    @property
    def target(self) -> GameTarget:
        return selected_target()

    @property
    def wow_path(self) -> Path:
        return Path(self.game_root).expanduser() / self.target.product_dir

    @property
    def account_dir(self) -> Path:
        return self.wow_path / "WTF" / "Account" / self.wtf_account

    @property
    def addons_dir(self) -> Path:
        return self.wow_path / "Interface" / "AddOns"

    @property
    def saved_variables(self) -> Path:
        return self.account_dir / "SavedVariables" / "Crowley.lua"

    @property
    def data_lua(self) -> Path:
        return self.addons_dir / "Crowley_Data" / "Data.lua"

    @property
    def state_file(self) -> Path:
        dirs = _xdg_dirs()
        target = self.target
        return (
            dirs.crowley_state
            / target.house
            / target.target_id
            / self.wtf_account
            / self.character
            / "companion.json"
        )


def config_path() -> Path:
    return _xdg_dirs().crowley_config / "scanner.toml"


def _secret_token(path: Path) -> SecretStr:
    value = os.environ.get("CROWLEY_API_TOKEN")
    if value:
        return SecretStr(value)
    secret_path = path.with_name("secrets.env")
    try:
        for line in secret_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CROWLEY_API_TOKEN="):
                token = line.partition("=")[2]
                if token:
                    return SecretStr(token)
    except OSError as exc:
        raise ValueError("CROWLEY_API_TOKEN is missing from the environment and secrets.env") from exc
    raise ValueError("CROWLEY_API_TOKEN is missing from the environment and secrets.env")


def load_settings(path: str | Path | None = None) -> CompanionSettings:
    source = config_path() if path is None else Path(path)
    try:
        with source.open("rb") as stream:
            raw = cast(object, tomllib.load(stream))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"scanner config {source} is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"scanner"}:
        raise ValueError(f"{source} must contain only a [scanner] table")
    values = raw["scanner"]
    if not isinstance(values, dict):
        raise ValueError(f"[scanner] in {source} must be a table")
    try:
        config = ScannerConfig.model_validate(values)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in exc.errors()
        )
        raise ValueError(f"scanner config is invalid: {details}") from exc
    return CompanionSettings(**config.model_dump(), api_token=_secret_token(source))


def render_scanner_toml(config: ScannerConfig) -> str:
    """Render the standalone scanner configuration from its validated schema."""

    values = config.model_dump()
    rows = ["[scanner]"]
    for key in ("game_root", "wtf_account", "character", "server_url"):
        rows.append(f"{key} = {json.dumps(values[key], ensure_ascii=False)}")
    for key in ("poll_seconds", "refresh_minutes"):
        rows.append(f"{key} = {values[key]!r}")
    return "\n".join(rows) + "\n"


__all__ = ["CompanionSettings", "ScannerConfig", "config_path", "load_settings", "render_scanner_toml"]
