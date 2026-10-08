from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool

from ..deps import current_user
from ..models import User
from ..services.scrips import scrip_master

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/search")
async def search(q: str = Query("", max_length=40), limit: int = Query(40, ge=1, le=100), _: User = Depends(current_user)):
    await run_in_threadpool(scrip_master.ensure_loaded)
    return {"results": [asdict(i) for i in scrip_master.search(q, limit)], **scrip_master.status()}


@router.get("/status")
def stocks_status(_: User = Depends(current_user)):
    return scrip_master.status()
