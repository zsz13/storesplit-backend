from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas import BasketRequest, BasketResponse
from app.services.basket import BasketItemError, compare_basket

router = APIRouter(prefix="/basket", tags=["basket"])


@router.post("/compare", response_model=BasketResponse)
async def compare(request: BasketRequest, db: AsyncSession = Depends(get_db)) -> BasketResponse:
    try:
        return await compare_basket(db, request)
    except BasketItemError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
