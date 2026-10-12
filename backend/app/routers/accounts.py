from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException, status

from ..broker.kotak import normalize_mobile, normalize_totp_secret
from ..broker.manager import broker_manager
from ..config import get_settings
from ..db import Session, get_db
from ..deps import current_user
from ..models import BasketItem, BrokerAccount, SectionSettings, TradeSession, User
from ..money import inr
from ..schemas import AccountCreate, AccountOut, AccountPatch, CredentialsUpdate
from ..security import decrypt_json, encrypt_json, mask
from ..services import trading

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

CRED_FIELDS = ("consumer_key", "mobile", "ucc", "mpin", "totp_secret")


def _own(db: Session, user: User, account_id: int) -> BrokerAccount:
    acc = db.get(BrokerAccount, account_id)
    if acc is None or acc.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return acc


def _clean_credentials(raw: dict[str, str | None]) -> dict[str, str]:
    creds = {k: (raw.get(k) or "").strip() for k in CRED_FIELDS}
    missing = [k for k, v in creds.items() if not v]
    if missing:
        labels = {"consumer_key": "consumer key", "mobile": "mobile number", "ucc": "UCC",
                  "mpin": "MPIN", "totp_secret": "TOTP secret"}
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing " + ", ".join(labels[m] for m in missing))
    try:
        creds["totp_secret"] = normalize_totp_secret(creds["totp_secret"])
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That TOTP secret is not valid") from exc
    creds["mobile"] = normalize_mobile(creds["mobile"])
    creds["ucc"] = creds["ucc"].upper()
    if not creds["mpin"].isdigit():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MPIN should contain digits only")
    return creds


def _duplicate_ucc(db: Session, user: User, ucc: str, exclude_id: int | None = None) -> bool:
    for acc in db.find(BrokerAccount, {"user_id": user.id, "broker": "kotak"}):
        if acc.id == exclude_id or not acc.credentials:
            continue
        try:
            if decrypt_json(acc.credentials).get("ucc", "").upper() == ucc:
                return True
        except ValueError:
            continue
    return False


@router.get("", response_model=list[AccountOut])
def list_accounts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.find(BrokerAccount, {"user_id": user.id}, sort=[("created_at", 1), ("id", 1)])


@router.post("", response_model=AccountOut, status_code=201)
def create_account(body: AccountCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.broker == "paper":
        if not get_settings().enable_paper_trading:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paper trading is disabled on this server")
        acc = BrokerAccount(user_id=user.id, name=body.name.strip(), broker="paper", paper_funds=body.paper_funds,
                            client_name="Paper trader", status="connected")
        db.add(acc)
        db.commit()
        return acc

    creds = _clean_credentials(body.model_dump())
    if _duplicate_ucc(db, user, creds["ucc"]):
        raise HTTPException(status.HTTP_409_CONFLICT, "This Kotak account (UCC) is already added")
    acc = BrokerAccount(
        user_id=user.id,
        name=body.name.strip(),
        broker="kotak",
        credentials=encrypt_json(creds),
        ucc_masked=mask(creds["ucc"], 3),
        status="connected",
    )
    db.add(acc)
    db.commit()
    # The one and only interactive login: verify the details right now.
    try:
        broker_manager.ensure_session(acc.id, force=True)
    except Exception as exc:  # noqa: BLE001
        broker_manager.forget(acc.id)
        db.delete(acc)
        db.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Kotak Neo login failed — {exc}") from exc
    db.refresh(acc)
    return acc


@router.patch("/{account_id}", response_model=AccountOut)
def update_account(account_id: int, body: AccountPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = _own(db, user, account_id)
    if body.name is not None:
        acc.name = body.name.strip()
    if body.automation_enabled is not None:
        acc.automation_enabled = body.automation_enabled
    if body.paper_funds is not None:
        if acc.broker != "paper":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Only paper accounts have editable funds")
        acc.paper_funds = body.paper_funds
    db.commit()
    return acc


@router.put("/{account_id}/credentials", response_model=AccountOut)
def update_credentials(account_id: int, body: CredentialsUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = _own(db, user, account_id)
    if acc.broker != "kotak":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paper accounts have no login")
    try:
        current = decrypt_json(acc.credentials) if acc.credentials else {}
    except ValueError:
        current = {}
    merged = {k: (getattr(body, k) or current.get(k)) for k in CRED_FIELDS}
    creds = _clean_credentials(merged)
    if _duplicate_ucc(db, user, creds["ucc"], exclude_id=acc.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This Kotak account (UCC) is already added")
    old_blob, old_mask = acc.credentials, acc.ucc_masked
    acc.credentials = encrypt_json(creds)
    acc.ucc_masked = mask(creds["ucc"], 3)
    db.commit()
    try:
        broker_manager.ensure_session(acc.id, force=True)
    except Exception as exc:  # noqa: BLE001
        acc.credentials, acc.ucc_masked = old_blob, old_mask
        db.commit()
        broker_manager.forget(acc.id)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Kotak Neo login failed — {exc}") from exc
    db.refresh(acc)
    return acc


@router.post("/{account_id}/reconnect", response_model=AccountOut)
def reconnect(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    acc = _own(db, user, account_id)
    try:
        broker_manager.ensure_session(acc.id, force=True)
    except Exception as exc:  # noqa: BLE001
        db.refresh(acc)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    db.refresh(acc)
    return acc


def _snapshot(account_id: int) -> dict:
    try:
        return {"id": account_id, "ok": True, **trading.broker_snapshot(account_id)}
    except Exception as exc:  # noqa: BLE001
        return {"id": account_id, "ok": False, "error": str(exc)}


@router.get("/live")
def live(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Funds, P&L and open position count for every account (fetched in parallel)."""
    ids = [a.id for a in db.find(BrokerAccount, {"user_id": user.id}, sort=[("id", 1)])]
    if not ids:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(ids))) as ex:
        return list(ex.map(_snapshot, ids))


@router.get("/{account_id}/live")
def live_one(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _own(db, user, account_id)
    return _snapshot(account_id)


@router.get("/{account_id}/positions")
def positions(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _own(db, user, account_id)
    try:
        positions = trading.with_live_pnl(account_id, broker_manager.call(account_id, lambda b: b.positions()))
        return [p.to_dict() for p in positions]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.get("/{account_id}/orders")
def orders(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _own(db, user, account_id)
    try:
        return [o.to_dict() for o in broker_manager.call(account_id, lambda b: b.orders())]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@router.post("/{account_id}/square-off")
def square_off(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _own(db, user, account_id)
    try:
        return {"run_id": trading.launch("exit", user.id, [account_id], section=trading.ALL)}
    except trading.RunInProgress as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/{account_id}", status_code=204)
def delete_account(account_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Accounts can only be removed once their balance is exactly ₹0 and nothing is open."""
    acc = _own(db, user, account_id)
    try:
        funds, positions = broker_manager.call(acc.id, lambda b: (b.funds(), b.positions()))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Couldn't verify this account's balance, so it can't be deleted yet ({exc}). "
            "Fix the login with “Update login details” and try again.",
        ) from exc
    if abs(funds.available) >= 0.01:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"“{acc.name}” still has a balance of {inr(funds.available)}. "
            "Withdraw everything so the balance is ₹0, then delete it.",
        )
    open_positions = [p for p in positions if p.net_qty != 0]
    if open_positions:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"“{acc.name}” has {len(open_positions)} open position(s). Square them off first.",
        )
    # Everything is loaded first and changed after, so it is all saved in the one commit below.
    items = db.find(BasketItem, {"user_id": user.id})
    sessions = db.find(TradeSession, {"user_id": user.id})
    setts = db.find(SectionSettings, {"user_id": user.id})
    # Stocks limited to this account must not follow its id to anything else.
    for item in items:
        if item.account_ids and account_id in item.account_ids:
            left = [i for i in item.account_ids if i != account_id]
            item.account_ids = left or None
            if not left:
                item.active = False  # it was only for this account
    for sess in sessions:
        if sess.account_ids and account_id in sess.account_ids:
            sess.account_ids = [i for i in sess.account_ids if i != account_id]
    for sett in setts:
        if sett.account_ids and account_id in sett.account_ids:
            sett.account_ids = [i for i in sett.account_ids if i != account_id]  # [] = no accounts, never "all"
    db.delete(acc)  # with its order history links and paper positions (models.BrokerAccount._on_delete)
    db.commit()
    broker_manager.forget(account_id)
