from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..deps import current_user
from ..models import User
from ..services import reports, trading

router = APIRouter(prefix="/api/reports", tags=["reports"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("")
def list_reports(user: User = Depends(current_user)):
    """Days with a saved P/L report, newest first."""
    return {"today": trading.now_ist().date().isoformat(), "days": reports.saved_days(user.id)}


@router.get("/{day}.xlsx")
async def download(day: date, user: User = Depends(current_user)):
    """Excel report for one day: Date | Account | Stock | P/L, with the day's total at the end."""
    is_today = day == trading.now_ist().date()
    rows, errors = await run_in_threadpool(reports.report_rows, user.id, day)
    content = reports.build_xlsx(rows, day, is_today, errors)
    return Response(
        content,
        media_type=XLSX,
        headers={"Content-Disposition": f'attachment; filename="TradeLab-PnL-{day.isoformat()}.xlsx"'},
    )
