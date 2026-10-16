"""Kotak Neo Trade API adapter built on the official `kotakneoapi` SDK (neo_api_client).

Login uses the TOTP flow: the stored TOTP secret generates the 6 digit code, so
after the account is added once the app can log in by itself every day.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from typing import Any

import pyotp

from .base import (
    BrokerError,
    Funds,
    Holding,
    LoginInfo,
    Order,
    OrderOutcomeUnknown,
    OrderResult,
    Position,
    SessionExpired,
)

# Kotak says "Invalid session" (stCode 1003 or HTTP 403) when the session has ended. Its refusal for an
# order from an IP address that isn't whitelisted is "unauthorized" (stCode 100008): logging in again
# doesn't help with that one, so it is not a session problem.
_SESSION_HINTS = ("session", "re-login", "relogin", "2fa", "token expired", "invalid token")
_SESSION_CODES = ("403", "1003")
_IP_REFUSED_CODE = "100008"
IP_REFUSED = ("Kotak refused the request: this server's IP address isn't whitelisted for this account "
              "(Kotak Neo app → More → Trade API → static IP)")
_NO_DATA_HINTS = ("no data", "no record", "not found")


MOBILE_HELP = "Enter the mobile number registered with this Kotak account (10 digits, e.g. 98765 43210)"


def normalize_mobile(mobile: str) -> str:
    """The mobile number as Kotak expects it: +91 and the 10 digits for an Indian number.

    Takes spaces, dashes, a +91 or 91 prefix and a leading 0 (098765 43210); a foreign number needs its
    + and country code. Raises ValueError for anything else, rather than sending Kotak a number that
    can't match the account (Kotak then answers "User not found for given ucc")."""
    # Any script's digits count as digits (a regional keyboard's ४३२१० is 43210); everything else goes.
    digits = "".join(str(unicodedata.decimal(ch)) for ch in mobile if ch.isdecimal())
    plus = mobile.strip().startswith("+")
    if not plus and len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif plus and not digits.startswith(("91", "0")) and 8 <= len(digits) <= 15:
        return "+" + digits  # a foreign number registered with the account
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    raise ValueError(MOBILE_HELP)


UCC_HELP = ("The UCC is the 'Unique Client Code' in Neo app → Profile → Account details: letters and digits, "
            "like AB123. Not the numeric client ID, demat number, PAN or mobile")


def canonical_ucc(value: str) -> str:
    """The UCC as Kotak knows it: letters and digits, in capitals.

    Drops what copy-paste brings along and no UCC contains (spaces, zero-width and direction marks, soft
    hyphens) and turns full-width letters into plain ones. Raises ValueError for anything that can't be a
    UCC, such as a PAN or a mobile or demat number, rather than letting Kotak answer "User not found"."""
    text = unicodedata.normalize("NFKC", value)
    text = "".join(ch for ch in text if not ch.isspace() and unicodedata.category(ch) != "Cf").upper()
    if not re.fullmatch(r"[A-Z0-9]+", text):
        raise ValueError(UCC_HELP)
    if re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", text):
        raise ValueError("That looks like a PAN. " + UCC_HELP)
    if text.isdigit() and len(text) >= 10:
        raise ValueError("That looks like a mobile or demat number. " + UCC_HELP)
    return text


def clean_consumer_key(value: str) -> str:
    """The consumer key without what copy-paste adds around or inside it (spaces, quotes, hidden marks)."""
    text = "".join(ch for ch in value if not ch.isspace() and unicodedata.category(ch) != "Cf")
    return text.strip("\"'“”‘’")


# Kotak's answer when it finds no Trade API user for the UCC and mobile it was sent with this consumer key.
_USER_NOT_FOUND_HINT = (
    ". Check, signed in to this account in the Neo app: the UCC is the 'Unique Client Code' in Profile → "
    "Account details (letters and digits, like AB123); the mobile is the one registered on it; the consumer "
    "key was created in its More → Trade API; and Trade API → TOTP Registration was completed for it. If all "
    "of that matches, ask Kotak (support@kotakneo.com) to confirm Trade API login is enabled for this UCC."
)


def _masked_mobile(mobile: str) -> str:
    return f"{mobile[:3]}••••••{mobile[-4:]}" if len(mobile) > 7 else mobile


def _login_message(msg: str, ucc: str, mobile: str, code: str = "") -> str:
    """Kotak's login error, and for "User not found" also what TradeLab sent and what to check."""
    if "user not found" not in msg.lower():
        return msg
    said = f"{msg} (Kotak code {code})" if code else msg
    numeric = (f" — {ucc} is all digits, like a client ID; the UCC has letters too, like AB123"
               if ucc.isdigit() else "")
    return f"{said}. TradeLab sent UCC {ucc} and mobile {_masked_mobile(mobile)}{numeric}{_USER_NOT_FOUND_HINT}"


def _error_code(resp: Any) -> str:
    """Kotak's error code from a reply like {"error": [{"code": "10522", "message": ...}]}, or ""."""
    err = resp.get("error") if isinstance(resp, dict) else None
    if isinstance(err, list) and err and isinstance(err[0], dict):
        return str(err[0].get("code") or "")
    return str(resp.get("stCode") or "") if isinstance(resp, dict) and isinstance(err, str) else ""


def normalize_totp_secret(value: str) -> str:
    """Accept a raw base32 secret or a full otpauth:// URI (from the QR code)."""
    value = value.strip()
    if value.lower().startswith("otpauth://"):
        parsed = pyotp.parse_uri(value)
        return parsed.secret
    secret = re.sub(r"[\s-]", "", value).upper()
    pyotp.TOTP(secret).now()  # raises on an invalid base32 secret
    return secret


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any) -> int:
    return int(round(_f(value)))


# Fields of Kotak's check-margin response that hold the margin for the order, in order of preference.
# Only this order's margin. Not mrgnUsd (margin already in use) and not reqdMrgn (0, or the shortfall).
MARGIN_KEYS = ("ordMrgn", "orderMargin")


def _first(row: dict, *keys: str) -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return None


def _error_message(resp: Any) -> str | None:
    """Return a human readable error if `resp` is an error payload, else None."""
    if resp is None:
        return "Empty response from Kotak Neo"
    if isinstance(resp, str):
        return None
    if isinstance(resp, list):
        return None
    if not isinstance(resp, dict):
        return f"Unexpected response from Kotak Neo: {type(resp).__name__}"
    for key in ("Error Message", "Error", "error", "errMsg", "emsg"):
        if key in resp and resp[key]:
            err = resp[key]
            if isinstance(err, list) and err:
                first = err[0]
                return str(first.get("message", first)) if isinstance(first, dict) else str(first)
            return str(err)
    stat = str(resp.get("stat", "")).lower()
    if stat and stat not in ("ok", "success"):
        return str(resp.get("message") or resp.get("desc") or f"Kotak Neo returned status '{resp.get('stat')}'")
    code = resp.get("stCode")
    if code not in (None, 200, "200") and "data" not in resp and "nOrdNo" not in resp:
        return str(resp.get("message") or f"Kotak Neo returned code {code}")
    return None


def _code(resp: Any) -> str:
    return str(resp.get("stCode", "")) if isinstance(resp, dict) else ""


def _raise_for(resp: Any, context: str) -> None:
    msg = _error_message(resp)
    if msg is None:
        return
    code = _code(resp)
    if code == _IP_REFUSED_CODE:
        raise BrokerError(f"{context}: {IP_REFUSED}")
    if code in _SESSION_CODES or any(h in msg.lower() for h in _SESSION_HINTS):
        raise SessionExpired(f"{context}: {msg}")
    raise BrokerError(f"{context}: {msg}")


def _unclear_reply(resp: Any) -> str | None:
    """Why an order reply leaves it unknown whether Kotak has the order (None if the reply is clear).

    The SDK never raises from place_order: a timeout, a dropped connection or an error page that isn't
    JSON comes back as {"error": ApiException} or {"Error": exception}. Those requests may well have
    reached Kotak. Failures before anything was sent (no connection, the SDK's own checks, its rate
    limiter) and Kotak's own refusals are clear: the order was not placed.
    """
    if resp is None:
        return "Empty reply from Kotak Neo"
    if not isinstance(resp, dict):
        return None
    err = resp.get("error") if "error" in resp else resp.get("Error")
    if not isinstance(err, BaseException):
        return None
    status = getattr(err, "status", None)
    if status is not None:  # the SDK's ApiException
        reason = str(getattr(err, "reason", "") or err)
        if status == 429 or "unable to connect" in reason.lower():
            return None  # refused by the SDK's rate limiter, or never connected: not sent
        if status == 0 or (isinstance(status, int) and status >= 500):
            return reason.strip() or "no reply"
        return None
    if isinstance(err, json.JSONDecodeError):
        return "Kotak Neo's reply couldn't be read"
    if isinstance(err, (ValueError, TypeError, KeyError)):
        return None  # the SDK's own checks refused the order before sending it
    return str(err) or err.__class__.__name__


def _is_no_data(resp: Any) -> bool:
    msg = (_error_message(resp) or "").lower()
    return any(h in msg for h in _NO_DATA_HINTS)


class KotakNeoBroker:
    def __init__(self, credentials: dict[str, str]):
        from neo_api_client import NeoAPI  # imported lazily: heavy dependency

        self._creds = credentials
        self._client = NeoAPI(consumer_key=clean_consumer_key(credentials["consumer_key"]), environment="prod")
        self._lock = threading.Lock()
        self._last_totp_step: int | None = None
        self.logged_in = False

    # -- auth -----------------------------------------------------------------
    def login(self) -> LoginInfo:
        with self._lock:
            totp = pyotp.TOTP(self._creds["totp_secret"])
            # Don't submit a code that is about to roll over, or one this
            # account already used (one-time codes can't be replayed).
            remaining = totp.interval - (time.time() % totp.interval)
            if remaining < 3 or int(time.time() // totp.interval) == self._last_totp_step:
                time.sleep(remaining + 0.2)
            self._last_totp_step = int(time.time() // totp.interval)
            try:  # details saved by an older version are cleaned the same way as new ones
                mobile, ucc = normalize_mobile(self._creds["mobile"]), canonical_ucc(self._creds["ucc"])
            except ValueError as exc:
                raise BrokerError(f"Login failed: {exc}") from exc
            resp = self._client.totp_login(mobile_number=mobile, ucc=ucc, totp=totp.now())
            msg = _error_message(resp)
            data = resp.get("data") if isinstance(resp, dict) else None
            if msg or not isinstance(data, dict) or not data.get("token"):
                detail = _login_message(msg, ucc, mobile, _error_code(resp)) if msg else "no session token returned"
                raise BrokerError(f"Login failed: {detail}")
            name = data.get("greetingName")
            resp = self._client.totp_validate(mpin=self._creds["mpin"])
            msg = _error_message(resp)
            data = resp.get("data") if isinstance(resp, dict) else None
            if msg or not isinstance(data, dict) or not data.get("token"):
                raise BrokerError(f"MPIN validation failed: {msg or 'no trade token returned'}")
            self.logged_in = True
            return LoginInfo(client_name=data.get("greetingName") or name)

    # -- account data ---------------------------------------------------------
    def funds(self) -> Funds:
        resp = self._client.limits()
        _raise_for(resp, "Fetching funds")
        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        if isinstance(data, list):
            data = data[0] if data else {}
        # Net = cash ("CollateralValue", despite its name) + margin from pledged shares
        # ("Collateral") - margin in use, across every segment.
        return Funds(
            available=_f(data.get("Net")),
            margin_used=_f(data.get("MarginUsed")),
            collateral=_f(data.get("Collateral")),
        )

    def positions(self) -> list[Position]:
        resp = self._client.positions()
        if _is_no_data(resp):
            return []
        _raise_for(resp, "Fetching positions")
        rows = (resp.get("data") or []) if isinstance(resp, dict) else []
        out: list[Position] = []
        for r in rows:
            buy_qty = _i(r.get("cfBuyQty")) + _i(r.get("flBuyQty"))
            sell_qty = _i(r.get("cfSellQty")) + _i(r.get("flSellQty"))
            net = buy_qty - sell_qty
            nf = (
                _f(r.get("multiplier"), 1)
                * (_f(r.get("genNum"), 1) / (_f(r.get("genDen"), 1) or 1))
                * (_f(r.get("prcNum"), 1) / (_f(r.get("prcDen"), 1) or 1))
            ) or 1
            buy_amt = _f(r.get("buyAmt")) + _f(r.get("cfBuyAmt"))
            sell_amt = _f(r.get("sellAmt")) + _f(r.get("cfSellAmt"))
            ltp = _f(r.get("ltp"), default=float("nan"))
            ltp_val = None if ltp != ltp else ltp  # NaN check
            pnl = r.get("mtmPnl")
            if pnl is None and net == 0:
                pnl = sell_amt - buy_amt  # closed: the P/L is fully realised
            elif pnl is None and ltp_val is not None:
                pnl = (sell_amt - buy_amt) + net * ltp_val * nf
            out.append(
                Position(
                    exchange_segment=str(r.get("exSeg", "nse_cm")),
                    trading_symbol=str(r.get("trdSym", "")),
                    product=str(r.get("prod", "")),
                    net_qty=net,
                    buy_qty=buy_qty,
                    sell_qty=sell_qty,
                    # Unrounded, so quantity × average price adds up to the exact amounts.
                    buy_avg=buy_amt / (buy_qty * nf) if buy_qty else 0.0,
                    sell_avg=sell_amt / (sell_qty * nf) if sell_qty else 0.0,
                    ltp=ltp_val,
                    pnl=round(_f(pnl), 2) if pnl is not None else None,
                    token=str(r["tok"]) if r.get("tok") not in (None, "") else None,
                    multiplier=nf,
                )
            )
        return out

    def holdings(self) -> list[Holding]:
        """Demat holdings (delivery). Field names follow Kotak's portfolio/v1/holdings response."""
        resp = self._client.holdings()
        if _is_no_data(resp):
            return []
        _raise_for(resp, "Fetching holdings")
        rows = resp.get("data") if isinstance(resp, dict) else resp
        out: list[Holding] = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            seg = str(_first(r, "exchangeSegment", "exSeg") or "nse_cm").lower()
            symbol = str(_first(r, "symbol", "displaySymbol", "sym") or "").strip()
            trading_symbol = str(_first(r, "tradingSymbol", "trdSym") or "").strip()
            if not trading_symbol and symbol:
                # Holdings carry no order symbol; on NSE it is "<symbol>-<series>" (ITBEES-EQ, and
                # BAJAJ-AUTO-EQ for a symbol that has a hyphen of its own).
                series = str(_first(r, "series") or "EQ").strip().upper()
                if seg == "nse_cm" and not symbol.upper().endswith(f"-{series}"):
                    trading_symbol = f"{symbol}-{series}"
                else:
                    trading_symbol = symbol
            qty = _i(_first(r, "quantity", "qty", "holdingQty"))
            if not trading_symbol or qty <= 0:
                continue
            sellable = _first(r, "sellableQuantity", "sellableQty", "freeQty")
            price = _first(r, "closingPrice", "ltp", "lastPrice", "lastTradedPrice")
            out.append(Holding(
                trading_symbol=trading_symbol, symbol=symbol or trading_symbol.rsplit("-", 1)[0],
                exchange_segment=seg, quantity=qty, sellable_qty=_i(sellable) if sellable is not None else qty,
                avg_price=_f(_first(r, "averagePrice", "avgPrice", "avgPrc")),
                ltp=_f(price) if price not in (None, "") else None,
                # exchangeIdentifier is the exchange token; instrumentToken is a different number.
                token=str(_first(r, "exchangeIdentifier", "tok") or "") or None,
            ))
        return out

    def margin_per_lot(self, exchange_segment: str, token: str, trading_symbol: str, side: str, product: str,
                       quantity: int, price: float | None = None) -> float:
        """Margin Kotak needs for `quantity` units (one lot) of a contract, priced at `price` (the LTP).

        The SDK wraps the reply as {"data": {...}}; "ordMrgn" is this order's margin ("reqdMrgn"
        reads 0 when the account has enough money, so it is never used).
        """
        if not token:
            raise BrokerError(f"No instrument token for {trading_symbol}")
        if not price or price <= 0:
            raise BrokerError(f"No price for {trading_symbol} to check its margin")
        resp = self._client.margin_required(
            exchange_segment=exchange_segment, price=f"{price:.2f}", order_type="L", product=product,
            quantity=str(int(quantity)), instrument_token=str(token), transaction_type=side,
        )
        _raise_for(resp, "Checking margin")
        data = resp.get("data", resp) if isinstance(resp, dict) else {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            raise BrokerError(f"Margin response for {trading_symbol} was not understood")
        msg = _error_message(data)
        if msg or str(data.get("stat", "Ok")).lower() not in ("ok", ""):
            raise BrokerError(f"Margin check for {trading_symbol} failed: {msg or data.get('stat')}")
        for key in MARGIN_KEYS:
            value = _f(data.get(key), default=-1)
            if value > 0:
                return value
        raise BrokerError(f"Margin response for {trading_symbol} had no margin figure")

    def orders(self) -> list[Order]:
        resp = self._client.order_report()
        if _is_no_data(resp):
            return []
        _raise_for(resp, "Fetching orders")
        rows = (resp.get("data") or []) if isinstance(resp, dict) else []
        return [
            Order(
                order_id=str(r.get("nOrdNo", "")),
                status=str(r.get("ordSt") or r.get("stat") or ""),
                trading_symbol=str(r.get("trdSym", "")),
                exchange_segment=str(r.get("exSeg", "")),
                product=str(r.get("prod", "")),
                side=str(r.get("trnsTp", "")),
                quantity=_i(r.get("qty")),
                filled_qty=_i(r.get("fldQty")),
                avg_price=_f(r.get("avgPrc")),
                order_type=str(r.get("prcTp", "")),
                placed_at=r.get("ordDtTm"),
                message=None if str(r.get("rejRsn", "--")) in ("--", "", "NA") else str(r.get("rejRsn")),
                tag=str(r["GuiOrdId"]) if r.get("GuiOrdId") not in (None, "", "--", "NA") else None,
            )
            for r in rows
        ]

    # -- trading --------------------------------------------------------------
    def place_market_order(
        self, exchange_segment: str, trading_symbol: str, side: str, quantity: int, product: str = "MIS",
        tag: str | None = None,
    ) -> OrderResult:
        resp = self._client.place_order(
            exchange_segment=exchange_segment,
            product=product,
            price="0",
            order_type="MKT",
            quantity=str(int(quantity)),
            validity="DAY",
            trading_symbol=trading_symbol,
            transaction_type=side,
            amo="NO",
            tag=tag,  # echoed back as GuiOrdId in the order book, to find an order whose reply was lost
        )
        if isinstance(resp, dict) and resp.get("nOrdNo"):
            return OrderResult(ok=True, order_id=str(resp["nOrdNo"]), message="Order placed", raw=resp)
        unclear = _unclear_reply(resp)
        if unclear:
            raise OrderOutcomeUnknown(f"Placing order: {unclear}")
        msg = _error_message(resp)
        if _code(resp) == _IP_REFUSED_CODE:
            return OrderResult(ok=False, message=IP_REFUSED, raw=resp)
        _raise_session_only(resp, "Placing order")
        return OrderResult(ok=False, message=msg or "Order not accepted", raw=resp if isinstance(resp, dict) else {})

    def cancel_order(self, order_id: str) -> OrderResult:
        resp = self._client.cancel_order(order_id=order_id)
        msg = _error_message(resp)
        if msg is None:
            return OrderResult(ok=True, order_id=order_id, message="Cancelled", raw=resp)
        _raise_session_only(resp, "Cancelling order")
        return OrderResult(ok=False, order_id=order_id, message=msg)

    # -- market data ----------------------------------------------------------
    def ltp(self, instruments: list[tuple[str, str]]) -> dict[str, float]:
        """Latest prices by token. Keeps whatever batches succeed; raises only if every batch fails."""
        out: dict[str, float] = {}
        last_error: Exception | None = None
        for i in range(0, len(instruments), 50):
            batch = [{"instrument_token": tok, "exchange_segment": seg} for seg, tok in instruments[i : i + 50]]
            try:
                resp = self._client.quotes(instrument_tokens=batch, quote_type="ltp")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            rows = resp if isinstance(resp, list) else (resp.get("data") if isinstance(resp, dict) else None)
            if rows is None:
                last_error = BrokerError(f"Quotes failed: {_error_message(resp) or resp}")
            for r in rows or []:
                if isinstance(r, dict) and r.get("exchange_token") and r.get("ltp") is not None:
                    price = _f(r.get("ltp"))
                    if price > 0:
                        out[str(r["exchange_token"])] = price
        if not out and last_error is not None:
            raise last_error
        return out

    def quote_times(self, instruments: list[tuple[str, str]]) -> dict[str, float]:
        """Last-update time (epoch seconds) of each instrument's quote, by token."""
        batch = [{"instrument_token": tok, "exchange_segment": seg} for seg, tok in instruments[:50]]
        resp = self._client.quotes(instrument_tokens=batch, quote_type="all")
        rows = resp if isinstance(resp, list) else (resp.get("data") if isinstance(resp, dict) else None)
        if rows is None:
            raise BrokerError(f"Quotes failed: {_error_message(resp) or resp}")
        out: dict[str, float] = {}
        for r in rows:
            if not isinstance(r, dict) or not r.get("exchange_token"):
                continue
            ts = _f(r.get("lstup_time"), default=0.0)
            if ts > 1e12:  # milliseconds
                ts /= 1000
            if ts > 0:
                out[str(r["exchange_token"])] = ts
        return out

    def scrip_master_url(self, exchange_segment: str = "nse_cm") -> str:
        resp = self._client.scrip_master(exchange_segment=exchange_segment)
        if isinstance(resp, str) and resp.startswith("http"):
            return resp
        raise BrokerError(f"Could not get scrip master: {_error_message(resp) or resp}")


def _raise_session_only(resp: Any, context: str) -> None:
    """Raise SessionExpired for session problems; other errors are returned as results."""
    try:
        _raise_for(resp, context)
    except SessionExpired:
        raise
    except BrokerError:
        return
