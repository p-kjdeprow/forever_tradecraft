from __future__ import annotations

import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from psycopg import Connection as PsycopgConnection
from psycopg_pool import ConnectionPool

from .config import ServerSettings, get_settings

Settings = Annotated[ServerSettings, Depends(get_settings)]


def require_token(settings: Settings, authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.api_token.get_secret_value()}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


Authed = Depends(require_token)


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


Pool = Annotated[ConnectionPool, Depends(get_pool)]


def get_connection(pool: Pool) -> Iterator[PsycopgConnection[tuple[object, ...]]]:
    with pool.connection() as connection:
        yield connection


Connection = Annotated[PsycopgConnection[tuple[object, ...]], Depends(get_connection)]
