from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..db import Session, get_db
from ..deps import current_user
from ..models import BasketItem, BrokerAccount, User
from ..schemas import BasketIn, BasketOut, BasketPatch
from ..services import etfs
from ..services.contracts import contract_master
from ..services.scrips import scrip_master
from ..services.sections import SECTIONS

router = APIRouter(prefix="/api/basket", tags=["basket"])

MAX_PERCENT = 500  # % of available funds (MIS leverage can exceed 100%)


def _check_sizing(mode: str, value: float, lots: bool = False) -> None:
    if lots and mode != "lots":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Contracts are sized in lots")
    if not lots and mode == "lots":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Stocks are sized in shares, ₹ or %")
    if mode in ("qty", "lots") and (value != int(value) or value < 1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Quantity must be a whole number of at least 1")
    if mode == "percent" and value > MAX_PERCENT:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Percentage can be at most {MAX_PERCENT}%")


def _check_accounts(db: Session, user: User, ids: list[int] | None) -> list[int] | None:
    if not ids:
        return None
    own = set(db.distinct(BrokerAccount, "id", {"user_id": user.id}))
    bad = [i for i in ids if i not in own]
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown account selected")
    return sorted(set(ids))


def _own(db: Session, user: User, item_id: int) -> BasketItem:
    item = db.get(BasketItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Stock not found in your list")
    return item


@router.get("", response_model=list[BasketOut])
def list_items(section: str = "intraday", user: User = Depends(current_user), db: Session = Depends(get_db)):
    if section not in SECTIONS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown section {section!r}")
    return db.find(BasketItem, {"user_id": user.id, "section": section}, sort=[("created_at", 1), ("id", 1)])


@router.post("", response_model=BasketOut, status_code=201)
def add_item(body: BasketIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    sp = SECTIONS[body.section]
    if body.exchange_segment not in sp.segments:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{body.symbol} can't be traded in {sp.label}")
    if body.side not in sp.sides:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{sp.label} only buys; tap Sell to sell what it bought")
    mode = "lots" if sp.lots and body.sizing_mode == "qty" else body.sizing_mode
    _check_sizing(mode, body.sizing_value, sp.lots)
    contract = etf = stock = None
    if sp.lots:
        # Contract details always come from the exchange list, never from the request.
        contract = contract_master.get(body.exchange_segment, body.trading_symbol)
        if contract is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{body.trading_symbol} isn't in today's contract list")
    elif body.section == "holding":
        # Any ETF from TradeLab's ETF list, or any stock in NSE's equity list (bought for delivery).
        etf = etfs.find(body.symbol) or etfs.find(body.trading_symbol)
        if etf is None:
            scrip_master.ensure_loaded()
            stock = scrip_master.find(body.trading_symbol)
            if stock is None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                    f"{body.symbol} isn't in NSE's stock or ETF list, so it can't be held")
    exists = db.count(BasketItem, {
        "user_id": user.id,
        "section": body.section,
        "exchange_segment": body.exchange_segment,
        "trading_symbol": etf.trading_symbol if etf else body.trading_symbol,
    })
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{body.symbol} is already in your {sp.label} list")
    item = BasketItem(
        user_id=user.id,
        section=body.section,
        symbol=body.symbol.upper(),
        trading_symbol=body.trading_symbol,
        token=(contract.token if contract else None) or body.token or scrip_master.token_for(body.trading_symbol),
        exchange_segment=body.exchange_segment,
        name=body.name,
        side=body.side,
        sizing_mode=mode,
        sizing_value=body.sizing_value,
        account_ids=_check_accounts(db, user, body.account_ids),
    )
    if etf is not None:
        item.symbol, item.trading_symbol, item.name = etf.symbol, etf.trading_symbol, etf.name[:160]
        item.token = etf.token or scrip_master.token_for(etf.trading_symbol)
    if stock is not None:
        item.symbol, item.name = stock.symbol[:40], (body.name or stock.name)[:160]
        item.token = stock.token or item.token
    if contract is not None:
        item.symbol = contract.display[:40]
        item.name = contract.description[:160]
        item.lot_size = contract.lot_size
        item.expiry = contract.expiry
        item.instrument_type = contract.instrument_type
        item.option_type = contract.option_type
        item.strike = contract.strike
    db.add(item)
    db.commit()
    return item


@router.patch("/{item_id}", response_model=BasketOut)
def update_item(item_id: int, body: BasketPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = _own(db, user, item_id)
    lots = SECTIONS[item.section].lots
    mode = body.sizing_mode or item.sizing_mode
    value = body.sizing_value if body.sizing_value is not None else item.sizing_value
    if body.sizing_mode and body.sizing_value is None and body.sizing_mode != item.sizing_mode:
        value = {"qty": 1, "amount": 10000, "percent": 10, "lots": 1}[body.sizing_mode]
    _check_sizing(mode, value, lots)
    item.sizing_mode, item.sizing_value = mode, value
    if body.side is not None:
        if body.side not in SECTIONS[item.section].sides:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{SECTIONS[item.section].label} only buys")
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
