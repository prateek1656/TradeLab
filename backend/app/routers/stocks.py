from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool

from ..deps import current_user
from ..models import User
from ..services import etfs
from ..services.contracts import contract_master
from ..services.scrips import scrip_master
from ..services.sections import SECTIONS

router = APIRouter(prefix="/api/stocks", tags=["stocks"])


@router.get("/search")
async def search(q: str = Query("", max_length=40), limit: int = Query(40, ge=1, le=100), _: User = Depends(current_user)):
    await run_in_threadpool(scrip_master.ensure_loaded)
    return {"results": [asdict(i) for i in scrip_master.search(q, limit)], **scrip_master.status()}


@router.get("/status")
def stocks_status(_: User = Depends(current_user)):
    return scrip_master.status()


@router.post("/refresh")
async def refresh(_: User = Depends(current_user)):
    """Download today's NSE list from Kotak again (needs a connected Kotak account)."""
    await run_in_threadpool(scrip_master.ensure_loaded, True)
    return scrip_master.status()


@router.get("/etfs")
async def index_etfs(q: str = Query("", max_length=40), limit: int = Query(60, ge=1, le=400),
                     _: User = Depends(current_user)):
    """Index ETFs for the Holding section (most traded first when there is no search)."""
    await run_in_threadpool(scrip_master.ensure_loaded)
    results = etfs.search(q, limit)
    return {"results": [e.to_dict() for e in results], "groups": etfs.groups(), "total": len(etfs.catalogue()),
            **scrip_master.status()}


@router.get("/contracts")
async def contracts(section: str = Query(pattern="^(fno|commodity)$"), q: str = Query("", max_length=60),
                    kind: str | None = Query(None, pattern="^(future|option)$"),
                    limit: int = Query(40, ge=1, le=100), _: User = Depends(current_user)):
    """Futures and options for the F&O or Commodity section."""
    segments = SECTIONS[section].segments
    if not q.strip():
        return {"results": [], "status": contract_master.status()}
    try:
        results = await run_in_threadpool(contract_master.search, q, segments, kind, limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    status_ = {seg: contract_master.status(seg) for seg in segments}
    return {"results": [c.to_dict() for c in results], "status": status_}
