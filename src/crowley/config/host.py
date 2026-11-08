"""Per-machine host configuration."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]*\Z")
_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class HostConfigError(ValueError):
    """Raised when the untracked host configuration is unavailable or invalid."""


def _path(value: str, field: str) -> str:
    candidate = Path(value)
    if not candidate.is_absolute() or candidate == Path("/") or ".." in candidate.parts:
        raise ValueError(f"{field} must be an absolute non-root path without traversal")
    return value


def _pair(value: object, field: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must contain exactly two integers")
    values = cast(Sequence[object], value)
    if len(values) != 2:
        raise ValueError(f"{field} must contain exactly two integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        raise ValueError(f"{field} must contain exactly two integers")
    return cast(tuple[int, int], tuple(values))


def _crop(value: object, field: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must contain exactly four integers")
    values = cast(Sequence[object], value)
    if len(values) != 4:
        raise ValueError(f"{field} must contain exactly four integers")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in values):
        raise ValueError(f"{field} must contain exactly four integers")
    return cast(tuple[int, int, int, int], tuple(values))


def _field_name(info: ValidationInfo) -> str:
    field = info.field_name
    if field is None:
        raise ValueError("validator field name is unavailable")
    return field


class HostConfig(BaseModel):
    """Strict identity and measurement facts for one machine and character.

    ``game_root`` is always an absolute path.  It is not interpreted relative
    to ``wine_prefix``; keeping both paths absolute prevents an accidental
    change of Wine layout from changing the selected client.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = (
        "wine_prefix",
        "game_root",
        "wtf_account",
        "character",
        "display",
        "server_url",
        "window_title",
        "client_x",
        "client_y",
        "client_width",
        "client_height",
        "outer_width",
        "outer_height",
        "window_client_offset",
        "canvas_origin",
        "canvas_crop",
        "model_crop",
    )

    wine_prefix: StrictStr
    game_root: StrictStr
    wtf_account: StrictStr
    character: StrictStr
    display: StrictStr
    server_url: StrictStr
    window_title: StrictStr
    client_x: StrictInt
    client_y: StrictInt
    client_width: StrictInt
    client_height: StrictInt
    outer_width: StrictInt
    outer_height: StrictInt
    window_client_offset: tuple[StrictInt, StrictInt]
    canvas_origin: tuple[StrictInt, StrictInt]
    canvas_crop: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    model_crop: tuple[StrictInt, StrictInt, StrictInt, StrictInt]

    @field_validator("wine_prefix", "game_root")
    @classmethod
    def validate_path(cls, value: str, info: ValidationInfo) -> str:
        return _path(value, _field_name(info))

    @field_validator("wtf_account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        if _ACCOUNT_RE.fullmatch(value) is None or value in {".", ".."}:
            raise ValueError("wtf_account must be one literal account directory name")
        return value

    @field_validator("character")
    @classmethod
    def validate_character(cls, value: str) -> str:
        if _COMPONENT_RE.fullmatch(value) is None:
            raise ValueError("character must be one literal state-directory component")
        return value

    @field_validator("display", "server_url", "window_title")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("value must be a non-empty string")
        return value

    @field_validator("window_client_offset", "canvas_origin", mode="before")
    @classmethod
    def validate_pair(cls, value: object, info: ValidationInfo) -> tuple[int, int]:
        return _pair(value, _field_name(info))

    @field_validator("canvas_crop", "model_crop", mode="before")
    @classmethod
    def validate_crop(cls, value: object, info: ValidationInfo) -> tuple[int, int, int, int]:
        return _crop(value, _field_name(info))

    @field_validator("client_width", "client_height", "outer_width", "outer_height", mode="after")
    @classmethod
    def validate_positive_dimension(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("window dimensions must be positive")
        return value

    @model_validator(mode="after")
    def validate_geometry(self) -> HostConfig:
        if self.outer_width < self.client_width or self.outer_height < self.client_height:
            raise ValueError("outer window dimensions must contain the client dimensions")
        offset_x, offset_y = self.window_client_offset
        if offset_x < 0 or offset_y < 0:
            raise ValueError("window_client_offset must be non-negative")
        if (
            offset_x + self.client_width > self.outer_width
            or offset_y + self.client_height > self.outer_height
        ):
            raise ValueError("window_client_offset and client dimensions exceed outer geometry")
        origin_x, origin_y = self.canvas_origin
        if origin_x < 0 or origin_y < 0:
            raise ValueError("canvas_origin must be non-negative")
        for name, crop in (("canvas_crop", self.canvas_crop), ("model_crop", self.model_crop)):
            left, top, width, height = crop
            if left < 0 or top < 0 or width <= 0 or height <= 0:
                raise ValueError(f"{name} must have a positive rectangle within the client")
            if left + width > self.client_width or top + height > self.client_height:
                raise ValueError(f"{name} exceeds client geometry")
        return self

    @property
    def wine_prefix_path(self) -> Path:
        return Path(self.wine_prefix)

    @property
    def game_root_path(self) -> Path:
        return Path(self.game_root)


def host_config_path(environ: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    config_home = values.get("XDG_CONFIG_HOME")
    if not config_home:
        home = values.get("HOME")
        if not home:
            raise HostConfigError("HOME must be set when XDG_CONFIG_HOME is unset")
        config_home = str(Path(home) / ".config")
    path = Path(config_home)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise HostConfigError("XDG_CONFIG_HOME must be an absolute non-root path without traversal")
    return path / "crowley" / "host.toml"


def _validation_message(error: ValidationError) -> str:
    details: list[str] = []
    for item in error.errors():
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location}: {item['msg']}")
    return "; ".join(details)


def load_host_config(path: str | Path) -> HostConfig:
    """Load and strictly validate one host file, reporting all invalid keys."""

    host_path = Path(path)
    try:
        with host_path.open("rb") as handle:
            raw = cast(object, tomllib.load(handle))
    except FileNotFoundError as exc:
        missing = ", ".join(HostConfig.REQUIRED_KEYS)
        raise HostConfigError(f"host.toml is missing; required keys: {missing}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HostConfigError(f"host.toml is unreadable: {host_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise HostConfigError("host.toml must contain a top-level table")
    try:
        return HostConfig.model_validate(raw)
    except ValidationError as exc:
        raise HostConfigError(f"host.toml is invalid: {_validation_message(exc)}") from exc


__all__ = ["HostConfig", "HostConfigError", "host_config_path", "load_host_config"]
