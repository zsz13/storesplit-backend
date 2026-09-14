"""Local development/admin endpoint that triggers collection.

The request awaits the whole run, but the run itself is concurrent: retailers are fetched in
parallel and the endpoint returns once every retailer has been recorded.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import get_http_clients
from app.db.session import get_sessionmaker
from app.normalize.categories import CATEGORIES
from app.retailers import adapter_slugs
from app.retailers.clients import RetailerClients
from app.schemas import ScrapeRequest, ScrapeResponse
from app.services.scraper import run_scrape

router = APIRouter(tags=["scrape"])


@router.post("/scrape", response_model=ScrapeResponse)
async def scrape(
    request: ScrapeRequest,
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
    clients: RetailerClients = Depends(get_http_clients),
) -> ScrapeResponse:
    bad_retailers = [r for r in request.retailers or [] if r not in adapter_slugs()]
    bad_categories = [c for c in request.categories or [] if c not in CATEGORIES]
    if bad_retailers or bad_categories:
        raise HTTPException(
            status_code=422,
            detail={
                "unknown_retailers": bad_retailers,
                "unknown_categories": bad_categories,
                "retailers": adapter_slugs(),
                "categories": list(CATEGORIES),
            },
        )
    runs = await run_scrape(
        sessionmaker, clients, request.zip_code, request.retailers, request.categories
    )
    return ScrapeResponse(runs=runs)


@router.get("/scrape/options")
def scrape_options() -> dict[str, list[str]]:
    return {"retailers": adapter_slugs(), "categories": list(CATEGORIES)}
