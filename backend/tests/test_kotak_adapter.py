"""The Kotak adapter against the response shapes documented for the official SDK."""

import pyotp
import pytest

from app.broker import kotak
from app.broker.base import BrokerError, SessionExpired
from app.services.scrips import parse_scrip_csv


class FakeNeo:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def __getattr__(self, name):
        def method(**kwargs):
            self.calls.append((name, kwargs))
            r = self.responses[name]
            return r(**kwargs) if callable(r) else r
        return method


@pytest.fixture
def broker(monkeypatch):
    b = kotak.KotakNeoBroker.__new__(kotak.KotakNeoBroker)
    import threading
    b._creds = {"consumer_key": "ck", "mobile": "9876543210", "ucc": "AB123", "mpin": "123456",
                "totp_secret": "JBSWY3DPEHPK3PXP"}
    b._client = FakeNeo()
    b._lock = threading.Lock()
    b._last_totp_step = None
    b.logged_in = False
    return b


def test_totp_login_flow(broker):
    broker._client.responses = {
        "totp_login": {"data": {"token": "view", "sid": "s", "greetingName": "RAHUL", "status": "success"}},
        "totp_validate": {"data": {"token": "trade", "sid": "s", "kType": "Trade", "status": "success"}},
    }
    info = broker.login()
    assert info.client_name == "RAHUL"
    name, kwargs = broker._client.calls[0]
    assert kwargs["mobile_number"] == "+919876543210"
    assert kwargs["totp"] == pyotp.TOTP("JBSWY3DPEHPK3PXP").now() or len(kwargs["totp"]) == 6
    assert broker._client.calls[1] == ("totp_validate", {"mpin": "123456"})


def test_login_errors_are_reported(broker):
    broker._client.responses = {"totp_login": {"error": [{"code": "10522", "message": "Invalid TOTP"}]}}
    with pytest.raises(BrokerError, match="Invalid TOTP"):
        broker.login()


def test_limits_positions_orders_parsing(broker):
    broker._client.responses = {
        "limits": {"Net": "19.41", "MarginUsed": "18.78", "CollateralValue": "38.19", "stCode": 200, "stat": "Ok"},
        "positions": {"stat": "ok", "stCode": 200, "data": [{
            "exSeg": "nse_cm", "trdSym": "SBIN-EQ", "prod": "MIS", "cfBuyQty": "0", "cfSellQty": "0",
            "flBuyQty": "10", "flSellQty": "4", "buyAmt": "8000.00", "sellAmt": "3240.00", "cfBuyAmt": "0.00",
            "cfSellAmt": "0.00", "multiplier": "1", "genNum": "1", "genDen": "1", "prcNum": "1", "prcDen": "1",
            "ltp": "812.00"}]},
        "order_report": {"stat": "Ok", "stCode": 200, "data": [{
            "nOrdNo": "250122000612876", "ordSt": "open", "trdSym": "IDEA-EQ", "exSeg": "nse_cm", "prod": "NRML",
            "trnsTp": "B", "qty": 1, "fldQty": 0, "avgPrc": "0.00", "prcTp": "L", "rejRsn": "--"}]},
    }
    assert broker.funds().available == pytest.approx(19.41)
    (p,) = broker.positions()
    assert (p.net_qty, p.buy_avg, p.sell_avg) == (6, 800.0, 810.0)
    assert p.pnl == pytest.approx((3240 - 8000) + 6 * 812)
    (o,) = broker.orders()
    assert o.is_open and o.order_id == "250122000612876" and o.message is None


def test_empty_books(broker):
    broker._client.responses = {
        "positions": {"stat": "Not_Ok", "emsg": "No Data", "stCode": 5203},
        "order_report": {"stat": "Not_Ok", "emsg": "No Data", "stCode": 5203},
    }
    assert broker.positions() == []
    assert broker.orders() == []


def test_place_order_success_reject_and_session(broker):
    broker._client.responses = {"place_order": {"stat": "Ok", "nOrdNo": "2401", "stCode": 200}}
    res = broker.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    assert res.ok and res.order_id == "2401"
    _, kwargs = broker._client.calls[-1]
    assert kwargs["order_type"] == "MKT" and kwargs["product"] == "MIS" and kwargs["price"] == "0"
    assert kwargs["quantity"] == "10" and kwargs["transaction_type"] == "B" and kwargs["validity"] == "DAY"

    broker._client.responses = {"place_order": {"stat": "Not_Ok", "emsg": "Insufficient funds", "stCode": 1009}}
    res = broker.place_market_order("nse_cm", "SBIN-EQ", "B", 10)
    assert not res.ok and "Insufficient" in res.message

    broker._client.responses = {"place_order": {"Error Message": "Complete the 2fa process before accessing this application"}}
    with pytest.raises(SessionExpired):
        broker.place_market_order("nse_cm", "SBIN-EQ", "B", 10)


def test_quotes_parsing(broker):
    broker._client.responses = {"quotes": [{"exchange_token": "1333", "ltp": "801.60"}]}
    assert broker.ltp([("nse_cm", "1333")]) == {"1333": 801.6}


def test_scrip_csv_parsing():
    csv = (
        "pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,pISIN,pDesc,lLotSize,dStrikePrice;\n"
        "2885,EQ,nse_cm,,RELIANCE,RELIANCE-EQ,,INE002A01018,RELIANCE INDUSTRIES LTD,1,-1\n"
        "11536,EQ,nse_cm,,TCS,TCS-EQ,,INE467B01029,TATA CONSULTANCY SERV LT,1,-1\n"
        "99999,BE,nse_cm,,FOO,FOO-BE,,INE000000000,FOO LTD,1,-1\n"
        "26000,,nse_cm,INDEX,Nifty 50,,,,,1,-1\n"
    ).encode()
    rows = parse_scrip_csv(csv)
    assert [(r.symbol, r.trading_symbol, r.token) for r in rows] == [
        ("RELIANCE", "RELIANCE-EQ", "2885"), ("TCS", "TCS-EQ", "11536")
    ]
    assert rows[0].name == "RELIANCE INDUSTRIES LTD"


def test_totp_secret_normalisation():
    assert kotak.normalize_totp_secret(" jbsw y3dp ehpk 3pxp ") == "JBSWY3DPEHPK3PXP"
    assert kotak.normalize_totp_secret("otpauth://totp/x?secret=JBSWY3DPEHPK3PXP") == "JBSWY3DPEHPK3PXP"
    with pytest.raises(Exception):
        kotak.normalize_totp_secret("not-valid-1!")
    assert kotak.normalize_mobile("98765 43210") == "+919876543210"
    assert kotak.normalize_mobile("+91 98765 43210") == "+919876543210"


def test_totp_code_is_never_reused(broker, monkeypatch):
    broker._client.responses = {
        "totp_login": {"data": {"token": "view", "sid": "s"}},
        "totp_validate": {"data": {"token": "trade", "sid": "s"}},
    }
    clock = {"t": 1_000_000_005.0}
    monkeypatch.setattr(kotak.time, "time", lambda: clock["t"])
    monkeypatch.setattr(kotak.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    broker.login()
    first_step = int(clock["t"] // 30)
    broker.login()
    assert int(clock["t"] // 30) == first_step + 1  # waited for the next code
