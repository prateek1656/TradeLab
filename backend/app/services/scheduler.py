"""Schedules the daily jobs: session warm-up, each scheduled section's buy (Intraday 09:15,
Holding 09:20 by default) and Intraday's square-off (14:30), all in IST."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

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


def _entry_job(user_id: int, section: str = "intraday") -> None:
    """Buys only if the user confirmed today with "Let's Trade" (checked inside run_entry)."""
    with session_scope() as db:
        entry_time = trading.get_section(db, user_id, section).entry_time
    now = trading.now_ist()
    fire_at = datetime.combine(now.date(), trading.parse_hms(entry_time), tzinfo=IST)
    trading.run_entry(user_id, "scheduled", fire_at=fire_at, section=section)


def _exit_job(user_id: int, section: str = "intraday") -> None:
    with session_scope() as db:
        enabled = trading.get_section(db, user_id, section).auto_exit
    if enabled:
        trading.run_exit(user_id, "scheduled", section=section)


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
        confirmed = select(TradeSession.user_id).where(
            TradeSession.trade_date == today, TradeSession.status == "armed"
        )
        ids = list(
            db.scalars(
                select(BrokerAccount.id).where(
                    BrokerAccount.automation_enabled.is_(True), BrokerAccount.user_id.in_(confirmed)
                )
            )
        )
    for account_id in ids:
        try:
            broker_manager.ensure_session(account_id, force=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Warm-up login failed for account %s: %s", account_id, exc)


def _user_settings() -> list[tuple[int, str, str, str | None]]:
    """(user, section, entry time, exit time) for every scheduled section of every user."""
    with session_scope() as db:
        users = db.scalars(select(User)).all()
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
                                                    [user_id, section], 20)
            if exit_time:
                wanted[f"exit:{user_id}:{section}"] = (_exit_job, _cron(exit_time), [user_id, section], 3600)
        for job in _scheduler.get_jobs():
            if (job.id.startswith("entry:") or job.id.startswith("exit:")) and job.id not in wanted:
                job.remove()
        for job_id, (fn, trig, args, grace) in wanted.items():
            _scheduler.add_job(
                fn, trig, args=args, id=job_id, replace_existing=True,
                misfire_grace_time=grace, coalesce=True, max_instances=1,
            )


def catch_up_square_off() -> None:
    """If the server was down at square-off time, square off as soon as it is back.

    run_exit itself only acts on accounts where TradeLab bought today.
    """
    now = trading.now_ist()
    if now.weekday() >= 5:
        return
    for user_id, section, _, exit_time in _user_settings():
        sp = SECTIONS[section]
        if not exit_time:
            continue
        with session_scope() as db:
            if not trading.get_section(db, user_id, section).auto_exit:
                continue
        if trading.parse_hms(exit_time) <= now.time().replace(tzinfo=None) < sp.sell_until:
            threading.Thread(target=trading.run_exit, args=(user_id, "catchup"), kwargs={"section": section},
                             daemon=True).start()


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
    # Keep each trading day's P/L for the Excel report, even if no one opens the app.
    _scheduler.add_job(
        reports.capture_traded_accounts,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=40, timezone=IST),
        id="pnl-report",
        replace_existing=True,
        misfire_grace_time=6 * 3600,
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
    _scheduler.start()
    sync_jobs()
    catch_up_square_off()
    threading.Thread(target=scrip_master.ensure_loaded, daemon=True).start()
    threading.Thread(target=calendar.sync_nse_holidays, daemon=True).start()
    threading.Thread(target=reports.purge_old, daemon=True).start()


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
