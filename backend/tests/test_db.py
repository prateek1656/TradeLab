"""The MongoDB storage layer itself (app/db.py)."""

import os
import tempfile
import threading
from datetime import date, datetime, timezone

os.environ["TRADELAB_DATA_DIR"] = tempfile.mkdtemp(prefix="tradelab-test-")
os.environ["TRADELAB_MONGO_URL"] = os.environ.get("TRADELAB_TEST_MONGO_URL", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
os.environ["TRADELAB_MONGO_DB"] = f"tradelab_test_{os.getpid()}"  # never a real database

import pytest  # noqa: E402

from app.db import clear_all, init_db  # noqa: E402
from app.db import IntegrityError, Session, collection, database, session_scope
from app.models import (  # noqa: E402
    BasketItem, BrokerAccount, ExecutionRun, Holiday, OrderLog, PnlRecord, SectionSettings,
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


def test_deleting_an_account_unlinks_its_history():
    with session_scope() as db:
        u = _user(db)
        acc = BrokerAccount(user_id=u.id, name="Main")
        db.add(acc)
        db.flush()
        db.add(OrderLog(user_id=u.id, account_id=acc.id, account_name="Main", action="buy", status="success"))
        db.add(PnlRecord(user_id=u.id, trade_date=date(2026, 10, 14), account_id=acc.id, account_name="Main",
                         symbol="SBIN-EQ", pnl=10.0))
    with session_scope() as db:
        db.delete(db.get(BrokerAccount, 1))
    assert collection(OrderLog).find_one()["account_id"] is None
    assert collection(PnlRecord).find_one()["account_id"] is None


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


# ── one transaction per session, until commit (like the SQLAlchemy sessions this replaced)

def test_a_failed_block_saves_nothing_even_after_a_query():
    with pytest.raises(RuntimeError):
        with session_scope() as db:
            db.add(User(name="A", email="a@example.com", password_hash="x"))
            assert db.count(User, {}) == 1  # the query sees it (autoflush)...
            raise RuntimeError("boom")
    assert collection(User).count_documents({}) == 0  # ...but nothing was saved


def test_changes_are_not_saved_without_commit():
    with session_scope() as db:
        _user(db)
    db = Session()
    u = db.get(User, 1)
    u.name = "Changed"
    db.count(BrokerAccount, {})  # a query writes the change into the open transaction
    assert collection(User).find_one()["name"] == "A"  # others don't see it
    db.close()  # no commit
    assert collection(User).find_one()["name"] == "A"


def test_rollback_after_a_write_puts_everything_back():
    with session_scope() as db:
        _user(db)
        db.add(BrokerAccount(user_id=1, name="Keep"))
    db = Session()
    u = db.get(User, 1)
    keep = db.get(BrokerAccount, 1)
    u.name = "Changed"
    new = BrokerAccount(user_id=1, name="New")
    db.add(new)
    db.delete(keep)
    db.flush()
    db.rollback()
    assert u.name == "A" and db.get(BrokerAccount, 1) is keep and db.get(BrokerAccount, new.id) is None
    db.commit()
    db.close()
    assert collection(User).find_one()["name"] == "A"
    assert [d["name"] for d in collection(BrokerAccount).find()] == ["Keep"]


def test_a_unique_clash_later_in_the_session_undoes_the_whole_transaction():
    with session_scope() as db:
        _user(db)
        db.add(BasketItem(user_id=1, symbol="SBIN", trading_symbol="SBIN-EQ"))
    db = Session()
    db.add(BrokerAccount(user_id=1, name="Written first"))
    db.flush()
    db.add(BasketItem(user_id=1, symbol="SBIN", trading_symbol="SBIN-EQ"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.close()
    assert collection(BrokerAccount).count_documents({}) == 0


def test_two_sessions_changing_the_same_record_both_succeed_in_turn():
    with session_scope() as db:
        _user(db)
    a = Session()
    a.get(User, 1).name = "From A"
    a.flush()  # A's transaction now holds the record
    done = []

    def other():
        with session_scope() as b:
            b.get(User, 1).token_version = 7
        done.append(True)

    t = threading.Thread(target=other)
    t.start()
    t.join(0.5)
    assert not done  # B waits (retries) while A's transaction is open
    a.commit()
    a.close()
    t.join(10)
    assert done
    stored = collection(User).find_one()
    assert stored["token_version"] == 7  # B's change landed after A's


def test_a_transaction_mongodb_aborts_for_running_too_long_is_written_again():
    admin = database().client.admin
    old = admin.command("getParameter", 1, transactionLifetimeLimitSeconds=1)["transactionLifetimeLimitSeconds"]
    admin.command("setParameter", 1, transactionLifetimeLimitSeconds=1)
    try:
        with session_scope() as db:
            db.add(User(name="Slow", email="slow@example.com", password_hash="x"))
            db.flush()
            import time
            time.sleep(2.5)  # e.g. waiting on the broker; MongoDB aborts the transaction meanwhile
            db.add(BrokerAccount(user_id=1, name="Later"))
    finally:
        admin.command("setParameter", 1, transactionLifetimeLimitSeconds=old)
    assert collection(User).count_documents({}) == 1 and collection(BrokerAccount).count_documents({}) == 1


def test_direct_writes_save_the_session_first():
    with session_scope() as db:
        _user(db)
        db.add(TradeSession(user_id=1, trade_date=date(2026, 10, 14), sizing_mode="equal"))
    db = Session()
    db.get(User, 1).name = "Saved by the claim"
    assert db.update_one(TradeSession, {"id": 1, "status": "armed"}, {"status": "firing"}) == 1
    db.close()
    assert collection(User).find_one()["name"] == "Saved by the claim"


def test_counters_catch_up_with_ids_already_used():
    """After restoring an older backup, new ids must not reuse ones that exist."""
    with session_scope() as db:
        _user(db)
    collection(User).insert_one({"_id": 50, "name": "Restored", "email": "r@example.com", "password_hash": "x"})
    init_db()
    with session_scope() as db:
        u = User(name="Next", email="n@example.com", password_hash="x")
        db.add(u)
    assert u.id == 51


def test_after_a_failed_save_the_session_must_be_rolled_back():
    from app.db import SessionFailed

    with session_scope() as db:
        _user(db)
        db.add(BasketItem(user_id=1, symbol="SBIN", trading_symbol="SBIN-EQ"))
    db = Session()
    db.get(User, 1).name = "Written earlier"
    db.flush()
    db.add(BasketItem(user_id=1, symbol="SBIN", trading_symbol="SBIN-EQ"))
    with pytest.raises(IntegrityError):
        db.flush()
    with pytest.raises(SessionFailed):  # carrying on would save only part of the work
        db.commit()
    db.rollback()
    db.add(BrokerAccount(user_id=1, name="After rollback"))
    db.commit()
    db.close()
    assert collection(User).find_one()["name"] == "A"
    assert collection(BrokerAccount).count_documents({}) == 1


def test_a_bad_value_mid_save_undoes_the_whole_save():
    from decimal import Decimal

    from app.db import SessionFailed

    with session_scope() as db:
        _user(db)
    db = Session()
    db.add(BrokerAccount(user_id=1, name="Good"))
    db.flush()
    db.add(OrderLog(user_id=1, account_name="A", action="buy", status="success"))
    db.add(PnlRecord(user_id=1, trade_date=date(2026, 10, 14), account_name="A", symbol="X", pnl=Decimal("1.5")))
    with pytest.raises(Exception):
        db.flush()
    with pytest.raises(SessionFailed):
        db.commit()
    db.close()
    assert collection(BrokerAccount).count_documents({}) == 0 and collection(OrderLog).count_documents({}) == 0


def test_re_added_record_keeps_its_values_through_an_undo():
    with session_scope() as db:
        u = _user(db)
    db = Session()
    db.add(u)
    u.name = "Renamed"
    db.flush()
    db.rollback()
    assert (u.name, u.email) == ("A", "a@example.com")  # back to what's stored, not to defaults
    db.commit()
    db.close()
    assert collection(User).find_one()["email"] == "a@example.com"


def test_record_added_back_after_its_delete_was_written_is_saved_again():
    with session_scope() as db:
        _user(db)
        acc = BrokerAccount(user_id=1, name="Back")
        db.add(acc)
    with session_scope() as db:
        acc = db.get(BrokerAccount, 1)
        db.delete(acc)
        db.flush()
        db.add(acc)
    assert collection(BrokerAccount).count_documents({}) == 1


def test_settings_created_at_the_same_moment_by_two_requests():
    from app.services import trading

    with session_scope() as db:
        _user(db)
    results, errors = [], []
    barrier = threading.Barrier(10)

    def create():
        try:
            barrier.wait()
            with session_scope() as db:
                results.append(trading.get_section(db, 1, "holding").product)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and results == ["CNC"] * 10
    assert collection(SectionSettings).count_documents({}) == 1


def test_reads_recover_after_mongodb_aborts_a_long_transaction():
    admin = database().client.admin
    old = admin.command("getParameter", 1, transactionLifetimeLimitSeconds=1)["transactionLifetimeLimitSeconds"]
    admin.command("setParameter", 1, transactionLifetimeLimitSeconds=1)
    try:
        with session_scope() as db:
            db.add(User(name="Slow", email="slow@example.com", password_hash="x"))
            db.flush()
            import time
            time.sleep(2.5)
            assert db.count(User, {}) == 1  # read in a new transaction, with the user written again
            assert db.find_one(User, {"email": "slow@example.com"}).name == "Slow"
    finally:
        admin.command("setParameter", 1, transactionLifetimeLimitSeconds=old)
    assert collection(User).count_documents({}) == 1
