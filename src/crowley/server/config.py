from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CROWLEY_", env_file=".env", extra="ignore")

    def __init__(self, **values: object) -> None:
        initializer = cast(Callable[..., None], super().__init__)
        initializer(**values)

    database_url: str = "postgresql://crowley:crowley@127.0.0.1:5432/crowley"
    api_token: SecretStr
    # Raw scan payloads are archived here so stats/models can be recomputed later.
    data_dir: Path = Path("./data")
    # Bind to the Tailscale address (or 0.0.0.0) when the game runs on another machine.
    host: str = "127.0.0.1"
    port: int = 8080


@lru_cache
def get_settings() -> ServerSettings:
    return ServerSettings()
