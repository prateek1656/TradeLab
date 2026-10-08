from apscheduler.schedulers.background import BackgroundScheduler

from app.config import IST
from app.db import session_scope
from app.models import ExecutionRun
from app.services import scheduler, trading


def test_jobs_follow_user_settings(client, owner):
    sched = BackgroundScheduler(timezone=IST)
    sched.start(paused=True)
    scheduler._scheduler = sched
    try:
        scheduler.sync_jobs()
        me = client.get("/api/auth/me", headers=owner).json()
        entry = sched.get_job(f"entry:{me['id']}")
        exit_ = sched.get_job(f"exit:{me['id']}")
        # Entry job starts a minute early to log in and size orders, then fires on the dot.
        assert entry.next_run_time.astimezone(IST).strftime("%H:%M:%S") == "09:14:00"
        assert exit_.next_run_time.astimezone(IST).strftime("%H:%M:%S") == "14:30:00"
        assert entry.next_run_time.weekday() < 5 and exit_.next_run_time.weekday() < 5
        nxt = scheduler.next_run_times(me["id"])
        assert nxt["entry"].endswith("09:15:00+05:30")

        client.put("/api/automation/settings", json={"entry_time": "09:20", "exit_time": "15:00"}, headers=owner)
        assert sched.get_job(f"entry:{me['id']}").next_run_time.astimezone(IST).strftime("%H:%M") == "09:19"
        assert sched.get_job(f"exit:{me['id']}").next_run_time.astimezone(IST).strftime("%H:%M") == "15:00"

        # New people get their own jobs as soon as they sign up.
        tok = client.post(
            "/api/auth/signup", json={"name": "Zed", "email": "z@example.com", "password": "supersecret1"}
        ).json()["token"]
        uid = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()["id"]
        assert sched.get_job(f"entry:{uid}") is not None
        assert sched.get_job(f"exit:{uid}") is not None
    finally:
        sched.shutdown(wait=False)
        scheduler._scheduler = None


def test_manual_runs_cannot_overlap(client, owner):
    me = client.get("/api/auth/me", headers=owner).json()
    with session_scope() as db:
        db.add(ExecutionRun(user_id=me["id"], kind="exit", trigger="manual", trading_date=trading.now_ist().date()))
    r = client.post("/api/automation/square-off-all", headers=owner)
    assert r.status_code == 409
    assert client.post("/api/automation/run-entry", headers=owner).status_code == 404  # replaced by Let's Trade
