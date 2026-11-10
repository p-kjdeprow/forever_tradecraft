"""Strict service tunables loaded from tracked TOML templates."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import TypeVar, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictFloat,
    ValidationError,
    field_validator,
    model_validator,
)

T = TypeVar("T", bound=BaseModel)


class TunablesError(ValueError):
    """Raised when a service tunables file is not exact."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CompanionTunables(_StrictModel):
    poll_seconds: StrictFloat
    refresh_minutes: StrictFloat

    @field_validator("poll_seconds", "refresh_minutes")
    @classmethod
    def validate_intervals(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("companion intervals must be finite and greater than zero")
        return value

    @model_validator(mode="after")
    def validate_refresh(self) -> CompanionTunables:
        if self.refresh_minutes * 60 < self.poll_seconds:
            raise ValueError("refresh_minutes must be at least one poll interval")
        return self


class OpsTunables(_StrictModel):
    """Reserved strict surface for operational tunables."""


def _error_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )


def _load_section(path: Path, section: str) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            raw = cast(object, tomllib.load(handle))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TunablesError(f"{path} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise TunablesError(f"{path} must contain a TOML table")
    raw_table = cast(dict[str, object], raw)
    actual = set(raw_table)
    if actual != {section}:
        raise TunablesError(
            f"{path} must contain only [{section}]; unknown or missing tables: {sorted(actual ^ {section})}"
        )
    value = raw_table[section]
    if not isinstance(value, dict):
        raise TunablesError(f"[{section}] in {path} must be a TOML table")
    return cast(dict[str, object], value)


def load_companion_tunables(path: str | Path) -> CompanionTunables:
    source = Path(path)
    try:
        return CompanionTunables.model_validate(_load_section(source, "companion"))
    except ValidationError as exc:
        raise TunablesError(f"companion tunables are invalid: {_error_message(exc)}") from exc


__all__ = [
    "CompanionTunables",
    "OpsTunables",
    "TunablesError",
    "load_companion_tunables",
]
