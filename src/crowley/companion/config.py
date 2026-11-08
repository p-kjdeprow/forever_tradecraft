"""Companion settings assembled from the selected target and host contract."""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from crowley.config import (
    CompanionResolvedConfig,
    host_config_path,
    load_companion_tunables,
    load_host_config,
    resolve_companion,
)
from crowley.core.game_target import GameTarget, selected_target
from crowley.core.watchlist import Catalog

_ACCOUNT_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._#-]*\Z")


def config_path() -> Path:
    return host_config_path().with_name("companion.toml")


def state_path(settings: CompanionSettings | None = None) -> Path:
    if settings is not None:
        if settings.state_file is not None:
            return settings.state_file
        raise ValueError("resolved companion state_file is required")
    resolved = _load_resolved()
    return resolved.state_file


def _load_resolved() -> CompanionResolvedConfig:
    target = selected_target()
    host = load_host_config(host_config_path())
    tunables = load_companion_tunables(config_path())
    return resolve_companion(target, host, tunables)


def _secret_token() -> SecretStr:
    value = os.environ.get("CROWLEY_API_TOKEN")
    if value:
        return SecretStr(value)
    secret_path = config_path().with_name("secrets.env")
    try:
        for line in secret_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CROWLEY_API_TOKEN="):
                token = line.partition("=")[2]
                if token:
                    return SecretStr(token)
    except OSError as exc:
        raise ValueError("CROWLEY_API_TOKEN is missing from the environment and secrets.env") from exc
    raise ValueError("CROWLEY_API_TOKEN is missing from the environment and secrets.env")


class CompanionSettings(BaseModel):
    """Runtime settings; path fields are derived, not TOML configuration."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        arbitrary_types_allowed=True,
    )

    target: GameTarget
    catalog: Catalog
    canonical_plan: dict[str, object]
    route_provenance: str
    wow_dir: Path
    account_dir: Path
    server_url: str
    api_token: SecretStr
    poll_seconds: float = 3.0
    refresh_minutes: float = 5.0
    wtf_account: str
    house_key: str | None = None
    character: str | None = None
    state_file: Path | None = None
    saved_variables: Path
    data_lua: Path
    queue_lua: Path
    addons_dir: Path

    def __init__(self, **values: object) -> None:
        if not values:
            resolved = _load_resolved()
            values = {
                "wow_dir": resolved.flavor_dir,
                "target": resolved.target,
                "catalog": resolved.catalog,
                "canonical_plan": resolved.canonical_plan,
                "route_provenance": resolved.route_provenance,
                "account_dir": resolved.account_dir,
                "server_url": resolved.server_url,
                "api_token": _secret_token(),
                "poll_seconds": resolved.tunables.poll_seconds,
                "refresh_minutes": resolved.tunables.refresh_minutes,
                "wtf_account": resolved.host.wtf_account,
                "house_key": resolved.target.house,
                "character": resolved.host.character,
                "state_file": resolved.state_file,
                "saved_variables": resolved.saved_variables,
                "data_lua": resolved.data_lua,
                "queue_lua": resolved.queue_lua,
                "addons_dir": resolved.addons_dir,
            }
        super().__init__(**values)

    @model_validator(mode="after")
    def validate_target_facts(self) -> CompanionSettings:
        if self.wow_path.name != self.target.product_dir:
            raise ValueError("wow_dir must end in the selected target productDir")
        if self.house_key is not None and self.house_key != self.target.house:
            raise ValueError("house_key must match the selected target house when configured")
        return self

    @field_validator("wtf_account")
    @classmethod
    def validate_account(cls, value: str) -> str:
        if _ACCOUNT_COMPONENT.fullmatch(value) is None or value in {".", ".."}:
            raise ValueError("wtf_account must be one literal account directory name")
        return value

    @property
    def wow_path(self) -> Path:
        return self.wow_dir.expanduser()

    def saved_variables_file(self) -> Path:
        saved_variables = self.saved_variables
        account_dir = self.account_dir
        if not account_dir.is_dir():
            raise FileNotFoundError(f"configured WTF account directory does not exist: {account_dir}")
        resolved_account = account_dir.resolve()
        try:
            relative_account = resolved_account.relative_to(self.wow_path.resolve())
        except ValueError as exc:
            raise ValueError("configured WTF account directory escapes WTF/Account") from exc
        if relative_account.parts != ("WTF", "Account", self.wtf_account):
            raise ValueError("configured WTF account directory escapes WTF/Account")
        try:
            saved_variables.resolve().relative_to(resolved_account)
        except ValueError as exc:
            raise ValueError("configured SavedVariables file escapes the selected account") from exc
        if not saved_variables.is_file():
            raise FileNotFoundError(f"configured SavedVariables file does not exist: {saved_variables}")
        return saved_variables

    def saved_variables_files(self) -> list[Path]:
        """Return the one explicitly selected SavedVariables file."""

        return [self.saved_variables_file()]


__all__ = ["CompanionSettings", "config_path", "state_path"]
