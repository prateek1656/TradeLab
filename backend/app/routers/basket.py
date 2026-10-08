from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..models import BasketItem, BrokerAccount, User
from ..schemas import BasketIn, BasketOut, BasketPatch, BasketReuseIn
from ..services import trading
from ..services.scrips import scrip_master

router = APIRouter(prefix="/api/basket", tags=["basket"])

MAX_PERCENT = 500  # % of available funds (MIS leverage can exceed 100%)


def _check_sizing(mode: str, value: float) -> None:
    if mode == "qty" and (value != int(value) or value < 1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be a whole number of at least 1")
    if mode == "percent" and value > MAX_PERCENT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Percentage can be at most {MAX_PERCENT}%")


def _check_accounts(db: Session, user: User, ids: list[int] | None) -> list[int] | None:
    if not ids:
        return None
    own = set(db.scalars(select(BrokerAccount.id).where(BrokerAccount.user_id == user.id)))
    bad = [i for i in ids if i not in own]
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown account selected")
    return sorted(set(ids))


def _own(db: Session, user: User, item_id: int) -> BasketItem:
    item = db.get(BasketItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock not found in your list")
    return item


@router.get("")
def list_items(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Stocks for the next trading session (`plan_date`) plus earlier lists, which never trade by themselves."""
    day = trading.plan_date(user.id)
    rows = db.scalars(select(BasketItem).where(BasketItem.user_id == user.id).order_by(BasketItem.created_at)).all()
    return {"plan_date": day.isoformat(), "items": [BasketOut.model_validate(r) for r in rows]}


@router.post("", response_model=BasketOut, status_code=201)
def add_item(body: BasketIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Add a stock to the list for the next trading session."""
    _check_sizing(body.sizing_mode, body.sizing_value)
    day = trading.plan_date(user.id)
    item = db.scalar(
        select(BasketItem).where(
            BasketItem.user_id == user.id,
            BasketItem.exchange_segment == body.exchange_segment,
            BasketItem.trading_symbol == body.trading_symbol,
        )
    )
    if item is not None and item.trade_date == day:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.symbol} is already in the list for this day")
    if item is None:
        item = BasketItem(user_id=user.id, trading_symbol=body.trading_symbol, exchange_segment=body.exchange_segment)
        db.add(item)
    # A stock from an earlier day is moved to this day with the new settings.
    item.symbol = body.symbol.upper()
    item.token = body.token or scrip_master.token_for(body.trading_symbol)
    item.name = body.name
    item.side = body.side
    item.sizing_mode = body.sizing_mode
    item.sizing_value = body.sizing_value
    item.account_ids = _check_accounts(db, user, body.account_ids)
    item.active = True
    item.trade_date = day
    db.commit()
    return item


@router.post("/reuse", response_model=list[BasketOut])
def reuse_items(body: BasketReuseIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Put stocks from an earlier day (all of them, or the given ids) on the list for the next session."""
    day = trading.plan_date(user.id)
    q = select(BasketItem).where(BasketItem.user_id == user.id, BasketItem.trade_date.is_distinct_from(day))
    if body.ids is not None:
        q = q.where(BasketItem.id.in_(body.ids))
    items = list(db.scalars(q))
    for it in items:
        it.trade_date = day
        it.active = True
    db.commit()
    return items


@router.patch("/{item_id}", response_model=BasketOut)
def update_item(item_id: int, body: BasketPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = _own(db, user, item_id)
    mode = body.sizing_mode or item.sizing_mode
    value = body.sizing_value if body.sizing_value is not None else item.sizing_value
    if body.sizing_mode and body.sizing_value is None and body.sizing_mode != item.sizing_mode:
        value = {"qty": 1, "amount": 10000, "percent": 10}[body.sizing_mode]
    _check_sizing(mode, value)
    item.sizing_mode, item.sizing_value = mode, value
    if body.side is not None:
        item.side = body.side
    if body.active is not None:
        item.active = body.active
    if body.clear_account_ids:
        item.account_ids = None
    elif body.account_ids is not None:
        item.account_ids = _check_accounts(db, user, body.account_ids)
    db.commit()
    return item


@router.delete("/{item_id}", status_code=204)
def delete_item(item_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = _own(db, user, item_id)
    db.delete(item)
    db.commit()
