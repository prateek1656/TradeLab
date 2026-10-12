from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator

# Everything is stored in UTC; make sure the wire format says so even for naive values.
UTCDateTime = Annotated[datetime, AfterValidator(lambda v: v if v.tzinfo else v.replace(tzinfo=timezone.utc))]


class SignupIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: int
    name: str
    email: str
    created_at: UTCDateTime
    last_login_at: UTCDateTime | None = None
    is_owner: bool = False

    model_config = {"from_attributes": True}


class HolidayIn(BaseModel):
    day: date
    description: str = Field(default="Market holiday", max_length=120)


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker: Literal["kotak"] = "kotak"
    consumer_key: str | None = None
    mobile: str | None = None
    ucc: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None


class CredentialsUpdate(BaseModel):
    consumer_key: str | None = None
    mobile: str | None = None
    ucc: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    automation_enabled: bool | None = None


class AccountOut(BaseModel):
    id: int
    name: str
    broker: str
    ucc_masked: str | None
    client_name: str | None
    automation_enabled: bool
    status: str
    last_error: str | None
    last_login_at: UTCDateTime | None
    created_at: UTCDateTime

    model_config = {"from_attributes": True}


class BasketIn(BaseModel):
    section: Literal["intraday", "holding", "fno", "commodity"] = "intraday"
    symbol: str = Field(min_length=1, max_length=40)
    trading_symbol: str = Field(min_length=1, max_length=60)
    token: str | None = None
    exchange_segment: Literal["nse_cm", "bse_cm", "nse_fo", "bse_fo", "mcx_fo"] = "nse_cm"
    name: str | None = None
    side: Literal["B", "S"] = "B"
    sizing_mode: Literal["qty", "amount", "percent", "lots"] = "qty"
    sizing_value: float = Field(default=1, gt=0)
    account_ids: list[int] | None = None


class BasketPatch(BaseModel):
    side: Literal["B", "S"] | None = None
    sizing_mode: Literal["qty", "amount", "percent", "lots"] | None = None
    sizing_value: float | None = Field(default=None, gt=0)
    active: bool | None = None
    account_ids: list[int] | None = None
    clear_account_ids: bool = False


SectionKey = Literal["intraday", "holding", "fno", "commodity"]


class CancelIn(BaseModel):
    trade_date: date | None = None
    section: SectionKey = "intraday"


class SectionIn(BaseModel):
    entry_time: str | None = None
    exit_time: str | None = None
    auto_exit: bool | None = None
    product: Literal["MIS", "NRML", "CNC"] | None = None
    sizing_mode: Literal["equal", "custom"] | None = None
    # % of each account's free money kept aside when splitting equally.
    reserve_pct: float | None = Field(default=None, ge=0, le=90)
    max_lots: int | None = Field(default=None, ge=1, le=25)  # stays under NSE index freeze limits
    # Accounts this section trades in; clear_account_ids = every autopilot account.
    account_ids: list[int] | None = None
    clear_account_ids: bool = False

    @field_validator("entry_time", "exit_time")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _HMS.match(v):
            raise ValueError("Use HH:MM or HH:MM:SS (24 hour)")
        return v if len(v) == 8 else v + ":00"


class CommandIn(BaseModel):
    # The preview's fingerprint; refused if the list, accounts or settings changed since.
    fingerprint: str | None = Field(default=None, max_length=64)
    # The preview's single-use confirmation id: one run per "Yes", however often it is sent.
    confirm_id: str | None = Field(default=None, max_length=64)
    # Only these items (trading symbols); None = all of them.
    symbols: list[str] | None = Field(default=None, max_length=200)
    # Sell only: "B" closes only what TradeLab bought, "S" only what it sold first; None = both.
    opened: Literal["B", "S"] | None = None


class ArmIn(BaseModel):
    # The day shown in the "Are you sure?" dialog; refused if it no longer matches.
    trade_date: date
    # lets_trade.fingerprint from the status the dialog showed; refused if anything changed since.
    fingerprint: str | None = Field(default=None, max_length=64)
    section: SectionKey = "intraday"


class BasketOut(BaseModel):
    id: int
    section: str = "intraday"
    lot_size: int = 1
    expiry: date | None = None
    instrument_type: str | None = None
    option_type: str | None = None
    strike: float | None = None
    symbol: str
    trading_symbol: str
    token: str | None
    exchange_segment: str
    name: str | None
    side: str
    sizing_mode: str
    sizing_value: float
    active: bool
    account_ids: list[int] | None

    model_config = {"from_attributes": True}


_HMS = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


class AutomationIn(BaseModel):
    entry_time: str | None = None
    exit_time: str | None = None
    auto_exit: bool | None = None
    squareoff_scope: Literal["all", "intraday"] | None = None
    sizing_mode: Literal["equal", "custom"] | None = None
    reserve_pct: float | None = Field(default=None, ge=0, le=50)

    @field_validator("entry_time", "exit_time")
    @classmethod
    def _valid_time(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _HMS.match(v):
            raise ValueError("Use HH:MM or HH:MM:SS (24 hour)")
        return v if len(v) == 8 else v + ":00"


class AutomationOut(BaseModel):
    entry_time: str
    exit_time: str
    auto_exit: bool
    squareoff_scope: str
    sizing_mode: str
    reserve_pct: float

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: int
    kind: str
    section: str = "intraday"
    funds_short: list[str] | None = None
    trigger: str
    trading_date: date
    status: str
    summary: str | None
    started_at: UTCDateTime
    finished_at: UTCDateTime | None

    model_config = {"from_attributes": True}


class LogOut(BaseModel):
    id: int
    run_id: int | None
    section: str | None = None
    product: str | None = None
    account_id: int | None
    account_name: str
    action: str
    symbol: str | None
    quantity: int | None
    status: str
    order_id: str | None
    message: str | None
    created_at: UTCDateTime

    model_config = {"from_attributes": True}
