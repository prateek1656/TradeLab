from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, EmailStr, Field, field_validator

# SQLite returns naive datetimes; everything is stored in UTC, so say so on the wire.
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

    model_config = {"from_attributes": True}


class HolidayIn(BaseModel):
    day: date
    description: str = Field(default="Market holiday", max_length=120)


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    broker: Literal["kotak", "paper"] = "kotak"
    consumer_key: str | None = None
    mobile: str | None = None
    ucc: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None
    paper_funds: float = Field(default=100000, ge=0)


class CredentialsUpdate(BaseModel):
    consumer_key: str | None = None
    mobile: str | None = None
    ucc: str | None = None
    mpin: str | None = None
    totp_secret: str | None = None


class AccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    automation_enabled: bool | None = None
    paper_funds: float | None = Field(default=None, ge=0)


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
    symbol: str = Field(min_length=1, max_length=40)
    trading_symbol: str = Field(min_length=1, max_length=60)
    token: str | None = None
    exchange_segment: Literal["nse_cm", "bse_cm"] = "nse_cm"
    name: str | None = None
    side: Literal["B", "S"] = "B"
    sizing_mode: Literal["qty", "amount", "percent"] = "qty"
    sizing_value: float = Field(default=1, gt=0)
    account_ids: list[int] | None = None


class BasketPatch(BaseModel):
    side: Literal["B", "S"] | None = None
    sizing_mode: Literal["qty", "amount", "percent"] | None = None
    sizing_value: float | None = Field(default=None, gt=0)
    active: bool | None = None
    account_ids: list[int] | None = None
    clear_account_ids: bool = False


class BasketReuseIn(BaseModel):
    ids: list[int] | None = None  # None = every stock from earlier days


class BasketOut(BaseModel):
    id: int
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
    trade_date: date | None

    model_config = {"from_attributes": True}


_HMS = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


class AutomationIn(BaseModel):
    entry_time: str | None = None
    exit_time: str | None = None
    auto_entry: bool | None = None
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
    auto_entry: bool
    auto_exit: bool
    squareoff_scope: str
    sizing_mode: str
    reserve_pct: float

    model_config = {"from_attributes": True}


class RunOut(BaseModel):
    id: int
    kind: str
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
