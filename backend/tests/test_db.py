"""The MongoDB storage layer itself (app/db.py)."""

import os
import tempfile
import threading
from datetime import date, datetime, timezone

os.environ.setdefault("TRADELAB_DATA_DIR", tempfile.mkdtemp(prefix="tradelab-test-"))
os.environ.setdefault("TRADELAB_MONGO_URL", os.environ.get("TRADELAB_TEST_MONGO_URL",
                                                           "mongodb://127.0.0.1:27017/?replicaSet=rs0"))
os.environ.setdefault("TRADELAB_MONGO_DB", f"tradelab_test_{os.getpid()}")

import pytest  # noqa: E402

from app.db import clear_all, init_db  # noqa: E402
from app.db import IntegrityError, Session, collection, database, session_scope
from app.models import (  # noqa: E402
    BasketItem, BrokerAccount, ExecutionRun, Holiday, OrderLog, PaperPosition, PnlRecord, SectionSettings,
    TradeSession, User,
)


@pytest.fixture(autouse=True)
def _empty_database():
    # Also runs on its own (`pytest --noconftest tests/test_db.py`), before the app is converted.
    init_db()
    clear_all()
    yield


def _user(db, email="a@example.com"):
    u = User(name="A", email=email, password_hash="x")
    db.add(u)
    db.commit()
    return u


def test_ids_come_from_a_counter_and_are_never_reused():
    with session_scope() as db:
        a = _user(db, "a@example.com")
        acc1 = BrokerAccount(user_id=a.id, name="One")
        db.add(acc1)
    assert a.id == 1 and acc1.id == 1
    with session_scope() as db:
        db.delete(db.get(BrokerAccount, 1))
    with session_scope() as db:
        acc2 = BrokerAccount(user_id=a.id, name="Two")
        db.add(acc2)
    assert acc2.id == 2


def test_dates_and_datetimes_round_trip_and_filter():
    with session_scope() as db:
        u = _user(db)
        db.add(TradeSession(user_id=u.id, trade_date=date(2026, 10, 14), items=[{"symbol": "SBIN"}], sizing_mode="equal"))
    assert collection(TradeSession).find_one()["trade_date"] == "2026-10-14"  # sortable text
    with session_scope() as db:
        s = db.find_one(TradeSession, {"trade_date": date(2026, 10, 14)})
        assert s.trade_date == date(2026, 10, 14) and s.items == [{"symbol": "SBIN"}]
        assert s.confirmed_at.tzinfo is not None
        assert db.count(TradeSession, {"trade_date": {"$lt": date(2026, 10, 15)}}) == 1
        assert db.get(Holiday, date(2026, 10, 20)) is None


def test_one_object_per_record_and_changes_are_saved():
    with session_scope() as db:
        u = _user(db)
        db.add(BasketItem(user_id=u.id, symbol="SBIN", trading_symbol="SBIN-EQ", account_ids=[1]))
    with session_scope() as db:
        a = db.get(BasketItem, 1)
        b = db.find_one(BasketItem, {"trading_symbol": "SBIN-EQ"})
        assert a is b
        a.account_ids.append(2)  # changing a list in place is noticed too
        a.active = False
    stored = collection(BasketItem).find_one({"_id": 1})
    assert stored["account_ids"] == [1, 2] and stored["active"] is False


def test_a_broken_unique_rule_saves_nothing():
    with session_scope() as db:
        u = _user(db)
        db.add(BasketItem(user_id=u.id, symbol="SBIN", trading_symbol="SBIN-EQ"))
    db = Session()
    db.add(BasketItem(user_id=1, symbol="TCS", trading_symbol="TCS-EQ"))
    db.add(BasketItem(user_id=1, symbol="SBIN", trading_symbol="SBIN-EQ"))  # duplicate
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()
    assert collection(BasketItem).count_documents({}) == 1  # TCS wasn't saved either


def test_queries_see_pending_changes():
    db = Session()
    u = User(name="A", email="a@example.com", password_hash="x")
    db.add(u)
    assert db.count(User, {"email": "a@example.com"}) == 1
    db.close()


def test_rollback_restores_and_drops():
    with session_scope() as db:
        _user(db)
    db = Session()
    u = db.get(User, 1)
    u.name = "Changed"
    db.add(BrokerAccount(user_id=1, name="Never saved"))
    db.rollback()
    assert u.name == "A"
    db.commit()
    db.close()
    assert collection(BrokerAccount).count_documents({}) == 0
    assert collection(User).find_one()["name"] == "A"


def test_session_scope_saves_nothing_when_the_block_fails():
    with pytest.raises(RuntimeError):
        with session_scope() as db:
            db.add(User(name="A", email="a@example.com", password_hash="x"))
            raise RuntimeError("boom")
    assert collection(User).count_documents({}) == 0


def test_update_one_claims_a_record_only_once():
    with session_scope() as db:
        u = _user(db)
        db.add(TradeSession(user_id=u.id, trade_date=date(2026, 10, 14), sizing_mode="equal"))
    wins = []
    barrier = threading.Barrier(8)

    def claim():
        db = Session()
        barrier.wait()
        wins.append(db.update_one(TradeSession, {"id": 1, "status": "armed"}, {"status": "firing"}))
        db.close()

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(wins) == [0] * 7 + [1]


def test_update_many_counts_and_refreshes_loaded_records():
    with session_scope() as db:
        u = _user(db)
        for i in range(3):
            db.add(ExecutionRun(user_id=u.id, kind="entry", trigger="scheduled", trading_date=date(2026, 10, 14)))
    db = Session()
    run = db.get(ExecutionRun, 1)
    n = db.update_many(ExecutionRun, {"status": "running"}, {"status": "failed", "summary": "Interrupted"})
    assert n == 3 and run.status == "failed" and run.summary == "Interrupted"
    assert db.update_many(ExecutionRun, {"status": "running"}, {"status": "x"}) == 0
    db.close()


def test_deleting_an_account_unlinks_its_history_and_removes_paper_positions():
    with session_scope() as db:
        u = _user(db)
        acc = BrokerAccount(user_id=u.id, name="Paper", broker="paper")
        db.add(acc)
        db.flush()
        db.add(OrderLog(user_id=u.id, account_id=acc.id, account_name="Paper", action="buy", status="success"))
        db.add(PnlRecord(user_id=u.id, trade_date=date(2026, 10, 14), account_id=acc.id, account_name="Paper",
                         symbol="SBIN-EQ", pnl=10.0))
        db.add(PaperPosition(account_id=acc.id, trading_date=date(2026, 10, 14), exchange_segment="nse_cm",
                             trading_symbol="SBIN-EQ", product="MIS"))
    with session_scope() as db:
        db.delete(db.get(BrokerAccount, 1))
    assert collection(OrderLog).find_one()["account_id"] is None
    assert collection(PnlRecord).find_one()["account_id"] is None
    assert collection(PaperPosition).count_documents({}) == 0


def test_pnl_rule_ignores_records_of_deleted_accounts():
    with session_scope() as db:
        u = _user(db)
        for _ in range(2):
            db.add(PnlRecord(user_id=u.id, trade_date=date(2026, 10, 14), account_id=None, account_name="Gone",
                             symbol="SBIN-EQ", pnl=1.0))
    assert collection(PnlRecord).count_documents({}) == 2


def test_compound_keys_and_missing_fields_get_defaults():
    with session_scope() as db:
        u = _user(db)
        db.add(SectionSettings(user_id=u.id, section="fno", product="NRML"))
    raw = collection(SectionSettings).find_one()
    assert raw["_id"] == "1|fno" and raw["section"] == "fno"
    # A record saved by an older version without some fields.
    collection(SectionSettings).update_one({"_id": "1|fno"}, {"$unset": {"max_lots": "", "reserve_pct": ""}})
    with session_scope() as db:
        s = db.get(SectionSettings, (1, "fno"))
        assert (s.max_lots, s.reserve_pct, s.account_ids) == (10, 0.0, None)


def test_record_loaded_elsewhere_can_be_saved_again():
    with session_scope() as db:
        u = _user(db)
    u.name = "Renamed"
    with session_scope() as db:
        db.add(u)
    assert collection(User).find_one()["name"] == "Renamed"
    assert collection(User).count_documents({}) == 1


def test_owner_is_the_first_user():
    with session_scope() as db:
        first = _user(db, "first@example.com")
        second = _user(db, "second@example.com")
    assert first.is_owner and not second.is_owner


def test_naive_datetimes_are_saved_as_utc():
    with session_scope() as db:
        u = _user(db)
        u.last_login_at = datetime(2026, 10, 14, 4, 0)
    assert database()["users"].find_one()["last_login_at"].replace(tzinfo=timezone.utc) == datetime(
        2026, 10, 14, 4, 0, tzinfo=timezone.utc)


def test_many_new_records_get_ids_with_one_counter_write():
    with session_scope() as db:
        u = _user(db)
        logs = [OrderLog(user_id=u.id, account_name="A", action="buy", status="pending") for _ in range(50)]
        db.add_all(logs)
        assert [o.id for o in logs] == list(range(1, 51))
    assert database()["counters"].find_one({"_id": "order_logs"})["seq"] == 50
    with session_scope() as db:
        db.add(OrderLog(user_id=1, account_name="A", action="buy", status="pending"))
    assert collection(OrderLog).count_documents({}) == 51


def test_read_only_finds_are_not_tracked():
    with session_scope() as db:
        _user(db)
    with session_scope() as db:
        (u,) = db.find(User, {}, track=False)
        u.name = "Not saved"
    assert collection(User).find_one()["name"] == "A"
