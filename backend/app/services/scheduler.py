"""Schedules the daily jobs: session warm-up, Intraday's buy (09:15) and square-off (14:30), in IST.

Intraday is the only section with timed jobs. Holding, F&O and Commodity have none: they buy
and sell only when the user taps Buy or Sell."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from pymongo.errors import PyMongoError

from ..broker.manager import broker_manager
from ..config import IST, get_settings
from ..db import session_scope
from ..models import BrokerAccount, TradeSession, User
from . import calendar, reports, trading
from .scrips import scrip_master
from .sections import SCHEDULED, SECTIONS

log = logging.getLogger(__name__)

ENTRY_LEAD = trading.ENTRY_LEAD
WARMUP_TIME = "09:00:00"

_scheduler: BackgroundScheduler | None = None
_sync_lock = threading.Lock()


def _cron(hms: str) -> CronTrigger:
    t = trading.parse_hms(hms)
    return CronTrigger(day_of_week="mon-fri", hour=t.hour, minute=t.minute, second=t.second, timezone=IST)


def _in_thread(fn, *args, **kwargs) -> None:  # noqa: ANN001
    """Run a timed job in its own thread, so the scheduler's small worker pool is never held: an entry
    waits about a minute for its moment, and every confirmed user's entry fires at the same time."""
    name = f"{getattr(fn, '__name__', 'job')}-{args[0] if args else ''}"

    def guarded() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - never die silently: the log says which job failed and why
            log.exception("Timed job %s failed", name)

    threading.Thread(target=guarded, daemon=True, name=name).start()


def _with_db_retry(job, deadline: datetime, what: str) -> None:  # noqa: ANN001
    """Run `job`; if the database doesn't answer, try again (1 s, 2 s, 4 s, then every 5 s) until
    `deadline`. A retry never doubles an order: a confirmation already claimed isn't bought again, and a
    square-off never closes more than is open."""
    delay = 1.0
    while True:
        try:
            job()
            return
        except PyMongoError as exc:
            if trading.now_ist() >= deadline:
                log.error("%s gave up: the database didn't answer until %s (%s)", what, f"{deadline:%H:%M:%S}", exc)
                return
            log.warning("%s: the database didn't answer (%s), trying again in %.0f s", what, exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 5.0)


def _start_entry(user_id: int, section: str = "intraday", trigger: str = "scheduled") -> None:
    """Buys only if the user confirmed today with "Let's Trade" (checked inside run_entry)."""
    # The job starts a minute early; past the buy's own late limit there's nothing left to retry.
    deadline = trading.now_ist() + trading.ENTRY_LEAD + trading.ENTRY_LATE_TOLERANCE

    def go() -> None:
        with session_scope() as db:
            entry_time = trading.get_section(db, user_id, section).entry_time
        now = trading.now_ist()
        fire_at = datetime.combine(now.date(), trading.parse_hms(entry_time), tzinfo=IST)
        trading.run_entry(user_id, trigger, fire_at=fire_at, section=section)

    _with_db_retry(go, deadline, f"The {section} buy for user {user_id}")


def _start_exit(user_id: int, section: str = "intraday", trigger: str = "scheduled") -> None:
    now = trading.now_ist()
    deadline = datetime.combine(now.date(), trading.spec(section).sell_until, tzinfo=IST)

    def go() -> None:
        with session_scope() as db:
            enabled = trading.get_section(db, user_id, section).auto_exit
        if enabled:
            trading.run_exit(user_id, trigger, section=section)

    _with_db_retry(go, deadline, f"The {section} square-off for user {user_id}")


# Monotonic time of the scheduler's last tick: proof that timed jobs can still fire.
_last_beat: float | None = None


def _beat() -> None:
    global _last_beat
    _last_beat = time.monotonic()


def heartbeat_age() -> float | None:
    """Seconds since the scheduler last ticked, or None when it isn't running (e.g. in tests)."""
    if _scheduler is None or _last_beat is None:
        return None
    return time.monotonic() - _last_beat


def _entry_job(user_id: int, section: str = "intraday") -> None:
    _in_thread(_start_entry, user_id, section)


def _exit_job(user_id: int, section: str = "intraday") -> None:
    _in_thread(_start_exit, user_id, section)


def _warmup_job() -> None:
    """Log every automation account in before the bell, and refresh the stock list."""
    try:
        scrip_master.ensure_loaded()
    except Exception:  # noqa: BLE001
        log.exception("Scrip master refresh failed")
    today = trading.now_ist().date()
    if not trading.is_trading_day(today):
        return
    with session_scope() as db:
        # Only users who confirmed today with "Let's Trade" need their accounts logged in.
        confirmed = db.distinct(
            TradeSession, "user_id", {"trade_date": today, "status": "armed", "user_id": {"$ne": None}}
        )
        ids = [
            a.id
            for a in db.find(
                BrokerAccount, {"automation_enabled": True, "user_id": {"$in": confirmed}}, sort=[("id", 1)]
            )
        ]
    for account_id in ids:
        try:
            broker_manager.ensure_session(account_id, force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Warm-up login failed for account %s: %s", account_id, exc)


def _user_settings(user_id: int | None = None) -> list[tuple[int, str, str, str | None]]:
    """(user, section, entry time, exit time) for every scheduled section of every user (or one user)."""
    with session_scope() as db:
        users = db.find(User, {} if user_id is None else {"id": user_id}, sort=[("id", 1)])
        out = []
        for u in users:
            for key in SCHEDULED:
                s = trading.get_section(db, u.id, key)
                out.append((u.id, key, s.entry_time, s.exit_time if SECTIONS[key].has_exit else None))
        return out


def sync_jobs() -> None:
    """(Re)create the per-user, per-section jobs after settings change or users are added."""
    if _scheduler is None:
        return
    with _sync_lock:
        wanted = {}
        for user_id, section, entry_time, exit_time in _user_settings():
            entry_dt = datetime.combine(datetime.now(IST).date(), trading.parse_hms(entry_time)) - ENTRY_LEAD
            wanted[f"entry:{user_id}:{section}"] = (_entry_job, _cron(entry_dt.strftime("%H:%M:%S")),
                                                    [user_id, section], 50)
            if exit_time:
                # A late job may still run until the section's last moment to sell, never after
                # (run_exit refuses then too): a stalled process waking at 15:04 must not square off.
                today = datetime.now(IST).date()
                left = datetime.combine(today, SECTIONS[section].sell_until) - datetime.combine(
                    today, trading.parse_hms(exit_time))
                wanted[f"exit:{user_id}:{section}"] = (_exit_job, _cron(exit_time), [user_id, section],
                                                       max(60, int(left.total_seconds())))
        for job in _scheduler.get_jobs():
            if (job.id.startswith("entry:") or job.id.startswith("exit:")) and job.id not in wanted:
                job.remove()
        for job_id, (fn, trig, args, grace) in wanted.items():
            _scheduler.add_job(
                fn, trig, args=args, id=job_id, replace_existing=True,
                misfire_grace_time=grace, coalesce=True, max_instances=1,
            )


def catch_up_square_off(user_id: int | None = None) -> None:
    """If the square-off time has passed today without a square-off (the server was down, the time was
    moved to a moment already past, or automatic square-off was switched back on after it), square off
    now. run_exit itself only acts on accounts where TradeLab bought today, and not twice."""
    now = trading.now_ist()
    if now.weekday() >= 5:
        return
    for uid, section, _, exit_time in _user_settings(user_id):
        sp = SECTIONS[section]
        if not exit_time:
            continue
        with session_scope() as db:
            if not trading.get_section(db, uid, section).auto_exit:
                continue
        at = now.time().replace(tzinfo=None)
        if trading.parse_hms(exit_time) <= at < sp.sell_until:
            _in_thread(trading.run_exit, uid, "catchup", section=section)
        elif at >= sp.sell_until:  # too late to sell: the owner is told once, if the section traded today
            _in_thread(trading._record_missed_exit, uid, section, "catchup", now.date())


def catch_up_entries(user_id: int | None = None) -> None:
    """A confirmed buy whose job time has passed but whose moment hasn't (the server came back a few
    seconds before it, or the buy time was moved to less than a minute ahead) still buys on time.
    run_entry refuses to buy late, and claims the confirmation so it can't buy twice."""
    now = trading.now_ist()
    if now.weekday() >= 5:
        return
    with session_scope() as db:
        flt: dict = {"trade_date": now.date(), "status": "armed", "section": {"$in": list(SCHEDULED)}}
        if user_id is not None:
            flt["user_id"] = user_id
        armed = [(s.user_id, s.section) for s in db.find(TradeSession, flt)]
    for uid, section in armed:
        with session_scope() as db:
            entry_time = trading.get_section(db, uid, section).entry_time
        fire_at = datetime.combine(now.date(), trading.parse_hms(entry_time), tzinfo=IST)
        if fire_at - ENTRY_LEAD < now <= fire_at + trading.ENTRY_LATE_TOLERANCE and not trading._entry_busy(uid, section):
            _in_thread(_start_entry, uid, section, "catchup")


def settings_changed(user_id: int) -> None:
    """After a user changes times or the square-off switch: reschedule, and catch up anything the change
    left behind for today."""
    sync_jobs()
    if _scheduler is None:
        return
    catch_up_entries(user_id)
    catch_up_square_off(user_id)


def catch_up_reports() -> None:
    """If the server was down at 15:40 on a trading day, save the day's P/L as soon as it is back."""
    now = trading.now_ist()
    if trading.is_trading_day(now.date()) and now.time().replace(tzinfo=None) >= dtime(15, 40):
        _in_thread(_close_of_day)


def _close_of_day() -> None:
    """After the NSE close: settle the day's orders, true up what TradeLab holds, save the P/L."""
    for step in (trading.reconcile_today, trading.true_up_all, reports.capture_traded_accounts):
        try:
            step()
        except Exception:  # noqa: BLE001
            log.exception("Close-of-day step %s failed", getattr(step, "__name__", step))


# The 23:58 job may run late, but never after midnight: from then on the order books and TradeLab's "today" are the
# next day's, and the evening's orders would not be looked at.
AFTER_MCX_GRACE = 90


def _after_mcx_close() -> None:
    """After the MCX close: settle the evening's commodity orders and true up what TradeLab holds."""
    for step in (trading.reconcile_today, trading.true_up_all):
        try:
            step()
        except Exception:  # noqa: BLE001
            log.exception("After-MCX step %s failed", getattr(step, "__name__", step))


def next_run_times(user_id: int) -> dict:
    """Next automatic buy / square-off: Intraday's at the top level (older app versions), every
    scheduled section's under "sections"."""
    out: dict = {"entry": None, "exit": None, "sections": {}}
    if _scheduler is None:
        return out
    for key in SCHEDULED:
        times: dict[str, str | None] = {"entry": None, "exit": None}
        for kind in ("entry", "exit"):
            job = _scheduler.get_job(f"{kind}:{user_id}:{key}")
            if job and job.next_run_time:
                t = job.next_run_time
                if kind == "entry":
                    t = t + ENTRY_LEAD
                times[kind] = t.astimezone(IST).isoformat()
        out["sections"][key] = times
    out["entry"], out["exit"] = out["sections"]["intraday"]["entry"], out["sections"]["intraday"]["exit"]
    return out


def start() -> None:
    global _scheduler
    if get_settings().disable_scheduler or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone=IST, job_defaults={"max_instances": 1})
    _scheduler.add_job(_warmup_job, _cron(WARMUP_TIME), id="warmup", replace_existing=True, misfire_grace_time=1800)
    _scheduler.add_job(
        calendar.sync_nse_holidays,
        CronTrigger(hour=8, minute=30, timezone=IST),
        id="holidays",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
    )
    _scheduler.add_job(
        calendar.sync_clearing_holidays,
        CronTrigger(hour=8, minute=35, timezone=IST),
        id="clearing_holidays",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
    )
    # After the NSE close: settle the day's orders (Kotak's order books are cleared overnight), true up
    # what TradeLab holds, and keep the day's P/L for the Excel report, even if no one opens the app.
    _scheduler.add_job(
        _close_of_day,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone=IST),
        id="pnl-report",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
    )
    # After the MCX close (23:30, or 23:55 from November to March): the evening's commodity orders.
    _scheduler.add_job(
        _after_mcx_close,
        CronTrigger(day_of_week="mon-fri", hour=23, minute=58, timezone=IST),
        id="after-mcx",
        replace_existing=True,
        misfire_grace_time=AFTER_MCX_GRACE,
    )
    trading.recover_after_restart()
    # Reports are kept for two weeks; delete older ones every night.
    _scheduler.add_job(
        reports.purge_old,
        CronTrigger(hour=0, minute=10, timezone=IST),
        id="pnl-retention",
        replace_existing=True,
        misfire_grace_time=12 * 3600,
    )
    _beat()
    _scheduler.add_job(_beat, "interval", seconds=15, id="heartbeat", replace_existing=True, misfire_grace_time=60)
    _scheduler.start()
    sync_jobs()
    catch_up_entries()
    catch_up_square_off()
    catch_up_reports()
    threading.Thread(target=scrip_master.ensure_loaded, daemon=True).start()
    threading.Thread(target=calendar.sync_nse_holidays, daemon=True).start()
    threading.Thread(target=calendar.sync_clearing_holidays, daemon=True).start()
    threading.Thread(target=reports.purge_old, daemon=True).start()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
