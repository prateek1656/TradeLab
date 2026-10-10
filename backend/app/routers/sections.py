from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import BrokerAccount, User
from ..schemas import CommandIn, SectionIn
from ..services import scheduler, trading
from ..services.sections import ORDER, SECTIONS, SectionSpec

router = APIRouter(prefix="/api/sections", tags=["sections"])


def _spec(section: str) -> SectionSpec:
    sp = SECTIONS.get(section)
    if sp is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section {section!r}")
    return sp


@router.get("")
def list_sections(user: User = Depends(current_user), db: Session = Depends(get_db)):
    out = [trading.section_info(db, user.id, key) for key in ORDER]
    db.commit()
    return {"sections": out}


@router.put("/{section}")
def update_section(section: str, body: SectionIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sp = _spec(section)
    s = trading.get_section(db, user.id, section)
    data = body.model_dump(exclude_none=True)
    data.pop("clear_account_ids", None)
    if sp.scheduled:
        entry = data.get("entry_time", s.entry_time)
        opens, until = sp.opens.strftime("%H:%M:%S"), sp.buy_until.strftime("%H:%M:%S")
        if sp.has_exit:
            exit_ = data.get("exit_time", s.exit_time)
            last = sp.sell_until.strftime("%H:%M:%S")
            if not (opens <= entry <= until and entry < exit_ <= last):
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"{sp.label}: buy between {opens[:5]} and {until[:5]}, square-off after the buy "
                                    f"and by {last[:5]}")
        elif not (opens <= entry <= until):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{sp.label}: buy between {opens[:5]} and {until[:5]}")
    if "product" in data and data["product"] not in sp.products:
        if sp.lots:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{sp.label} uses carry-forward (NRML) orders only, so "
                                "nothing is ever closed by time — only when you tap Sell")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{sp.label} trades as {' or '.join(sp.products)}")
    if "account_ids" in data:
        own = set(db.scalars(select(BrokerAccount.id).where(BrokerAccount.user_id == user.id)))
        ids = sorted(set(data["account_ids"]))
        if any(i not in own for i in ids):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown account selected")
        data["account_ids"] = ids  # [] = no accounts (the section is paused)
    if body.clear_account_ids:
        data["account_ids"] = None
    for key, value in data.items():
        if key in ("entry_time",) and not sp.scheduled:
            continue
        if key in ("exit_time", "auto_exit") and not sp.has_exit:
            continue
        if key == "max_lots" and not sp.lots:
            continue
        setattr(s, key, value)
    db.commit()
    scheduler.sync_jobs()
    out = trading.section_info(db, user.id, section)
    db.commit()
    return out


@router.get("/{section}/positions")
async def positions(section: str, user: User = Depends(current_user)):
    """What this section bought and still holds, per account (Holding, F&O, Commodity)."""
    sp = _spec(section)
    if not sp.can_sell:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{sp.label} positions are squared off every day")
    return await run_in_threadpool(trading.section_positions, user.id, section)


@router.get("/{section}/preview")
async def preview(section: str, action: str = Query(pattern="^(buy|sell)$"),
                  symbols: list[str] | None = Query(None), user: User = Depends(current_user)):
    """What Buy or Sell would send right now (nothing is sent)."""
    _spec(section)
    try:
        return await run_in_threadpool(trading.command_preview, user.id, section, action,
                                       set(symbols) if symbols else None)
    except trading.SessionError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.post("/{section}/buy")
async def buy(section: str, body: CommandIn, user: User = Depends(current_user)):
    """The user said "Yes" to Buy: buy in every account of the section now."""
    _spec(section)
    try:
        run_id = await run_in_threadpool(trading.buy_now, user.id, section, body.fingerprint,
                                         set(body.symbols) if body.symbols else None)
    except trading.SessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"run_id": run_id}


@router.post("/{section}/sell")
async def sell(section: str, body: CommandIn, user: User = Depends(current_user)):
    """The user said "Yes" to Sell: sell what this section bought, in every account, now."""
    _spec(section)
    try:
        run_id = await run_in_threadpool(trading.sell_now, user.id, section, body.fingerprint,
                                         set(body.symbols) if body.symbols else None)
    except trading.SessionError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"run_id": run_id}
