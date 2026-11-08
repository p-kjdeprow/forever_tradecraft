from __future__ import annotations

from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI

from crowley.core.game_target import CatalogLike, packaged_game_target, target_scan_plan
from crowley.core.watchlist import load_catalog

from .config import get_settings
from .db import create_pool, verify_current
from .routes import account, addon, dashboard, health, houses, paper, queues, reference, scans


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    catalog = load_catalog()  # Fail at startup if the shipped plan is invalid.
    app.state.canonical_scan_plan = target_scan_plan(cast(CatalogLike, catalog), packaged_game_target())
    app.state.reference_item_ids = tuple(sorted(item.item_id for item in catalog.items))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    pool = create_pool(settings.database_url)
    try:
        with pool.connection() as conn:
            verify_current(conn)
        app.state.pool = pool
        yield
    finally:
        pool.close()


def create_app() -> FastAPI:
    application = FastAPI(title="Crowley", version="0.1.0", lifespan=lifespan)
    application.include_router(health.router)
    application.include_router(scans.router)
    application.include_router(houses.router)
    application.include_router(reference.router)
    application.include_router(account.router)
    application.include_router(addon.router)
    application.include_router(queues.router)
    application.include_router(paper.router)
    application.include_router(dashboard.router)
    return application


app = create_app()
