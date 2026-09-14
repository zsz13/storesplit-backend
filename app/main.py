"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import basket, health, location, products, scrape
from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging
from app.retailers.clients import RetailerClients
from app.services.refresh import registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pooled HTTP clients once, close them (and the DB pool) once."""
    app.state.http_clients = RetailerClients()
    try:
        yield
    finally:
        # Background refreshes hold their own sessions and HTTP clients, so they have to
        # stop before either is closed under them.
        await registry.aclose()
        await app.state.http_clients.aclose()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="StoreSplit API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(location.router)
    app.include_router(products.router)
    app.include_router(basket.router)
    app.include_router(scrape.router)
    return app


app = create_app()
