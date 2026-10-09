from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..deps import current_user
from ..models import User
from ..services import reports, trading

router = APIRouter(prefix="/api/reports", tags=["reports"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _attachment(content: bytes, media_type: str, filename: str) -> Response:
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("")
def list_reports(user: User = Depends(current_user)):
    """Days with a saved P/L report (newest first). Reports older than two weeks are deleted."""
    today = trading.now_ist().date()
    return {
        "today": today.isoformat(),
        "keep_days": reports.RETENTION_DAYS,
        "oldest": reports.oldest_kept(today).isoformat(),
        "days": reports.saved_days(user.id),
    }


@router.get("/all.zip")
async def download_all(user: User = Depends(current_user)):
    """Every kept day's Excel report in one zip (today's is read live from the broker)."""
    content, filename = await run_in_threadpool(reports.build_zip, user.id)
    return _attachment(content, "application/zip", filename)


@router.get("/{day}.xlsx")
async def download(day: date, user: User = Depends(current_user)):
    """One day's report: a Summary sheet, then a sheet per account with each stock and the net profit."""
    try:
        content = await run_in_threadpool(reports.workbook_for, user.id, day)
    except reports.ReportGone as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _attachment(content, XLSX, reports.filename_for(day))
