"""Daily profit & loss reports (Excel)."""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from sqlalchemy import func, select

from ..broker.manager import broker_manager
from ..db import session_scope
from ..models import BrokerAccount, PnlRecord, User
from . import trading

log = logging.getLogger(__name__)


@dataclass
class Row:
    account_id: int | None
    account: str
    symbol: str
    pnl: float


def _account_rows(acc_id: int, acc_name: str) -> list[Row]:
    positions = broker_manager.call(acc_id, lambda b: b.positions())
    return [
        Row(acc_id, acc_name, p.trading_symbol, round(p.pnl or 0.0, 2))
        for p in positions
        if (p.buy_qty or p.sell_qty) and p.pnl is not None
    ]


def capture(user_id: int, account_ids: list[int] | None = None) -> tuple[list[Row], list[str]]:
    """Read today's P/L from the broker for the user's accounts and save it.

    Returns the rows and a message for each account that couldn't be read.
    """
    today = trading.now_ist().date()
    with session_scope() as db:
        q = select(BrokerAccount).where(BrokerAccount.user_id == user_id)
        if account_ids is not None:
            q = q.where(BrokerAccount.id.in_(account_ids))
        accounts = [(a.id, a.name) for a in db.scalars(q.order_by(BrokerAccount.created_at))]
    if not accounts:
        return [], []

    def fetch(acc):
        try:
            return acc, _account_rows(*acc), None
        except Exception as exc:  # noqa: BLE001
            return acc, [], f"{acc[1]}: couldn't read positions ({exc})"

    with ThreadPoolExecutor(max_workers=min(16, len(accounts))) as ex:
        results = list(ex.map(fetch, accounts))

    rows: list[Row] = []
    errors: list[str] = []
    with session_scope() as db:
        for (acc_id, _), acc_rows, err in results:
            if err:
                errors.append(err)
                continue  # keep whatever was saved earlier for this account
            # Update or add, never delete: if Kotak clears the day's positions in
            # the evening, what was saved after the square-off stays.
            saved = {
                rec.symbol: rec
                for rec in db.scalars(select(PnlRecord).where(
                    PnlRecord.user_id == user_id, PnlRecord.trade_date == today, PnlRecord.account_id == acc_id))
            }
            for r in acc_rows:
                rec = saved.get(r.symbol)
                if rec is None:
                    db.add(PnlRecord(user_id=user_id, trade_date=today, account_id=r.account_id,
                                     account_name=r.account, symbol=r.symbol, pnl=r.pnl))
                else:
                    rec.pnl, rec.account_name, rec.captured_at = r.pnl, r.account, datetime.now(timezone.utc)
            rows.extend(acc_rows)
    return rows, errors


def saved_rows(user_id: int, day: date) -> list[Row]:
    with session_scope() as db:
        recs = db.scalars(
            select(PnlRecord)
            .where(PnlRecord.user_id == user_id, PnlRecord.trade_date == day)
            .order_by(PnlRecord.account_name, PnlRecord.symbol)
        ).all()
        return [Row(r.account_id, r.account_name, r.symbol, r.pnl) for r in recs]


def report_rows(user_id: int, day: date) -> tuple[list[Row], list[str]]:
    """Today: live from the broker (saved as it goes). Earlier days: what was saved."""
    if day == trading.now_ist().date():
        _, errors = capture(user_id)
        return saved_rows(user_id, day), errors
    return saved_rows(user_id, day), []


def saved_days(user_id: int, limit: int = 90) -> list[dict]:
    with session_scope() as db:
        rows = db.execute(
            select(PnlRecord.trade_date, func.sum(PnlRecord.pnl), func.count())
            .where(PnlRecord.user_id == user_id)
            .group_by(PnlRecord.trade_date)
            .order_by(PnlRecord.trade_date.desc())
            .limit(limit)
        ).all()
    return [{"day": d.isoformat(), "pnl": round(total or 0.0, 2), "rows": n} for d, total, n in rows]


def build_xlsx(rows: list[Row], day: date, is_today: bool, notes: list[str]) -> bytes:
    """Date | Account | Stock | P/L (₹), then the day's total."""
    wb = Workbook()
    ws = wb.active
    ws.title = "P&L"
    base = Font(name="Arial", size=11)
    bold = Font(name="Arial", size=11, bold=True)
    head_fill = PatternFill("solid", fgColor="E8ECEF")
    thin = Side(style="thin", color="B8C0C8")
    money = '#,##0.00;[Red]-#,##0.00;-'

    headers = ["Date", "Account", "Stock", "P/L (₹)"]
    ws.append(headers)
    for c in ws[1]:
        c.font = bold
        c.fill = head_fill
        c.border = Border(bottom=thin)
        c.alignment = Alignment(horizontal="center" if c.column == 1 else "left")

    rows = sorted(rows, key=lambda r: (r.account.lower(), r.symbol))
    for r in rows:
        ws.append([day, r.account, r.symbol.removesuffix("-EQ"), r.pnl])
    first, last = 2, 1 + len(rows)
    for row in ws.iter_rows(min_row=first, max_row=max(last, first - 1)):
        for c in row:
            c.font = base
        row[0].number_format = "DD-MMM-YYYY"
        row[0].alignment = Alignment(horizontal="center")
        row[3].number_format = money
    if not rows:
        ws.append([day, "No trades", "", 0])
        last = 2
        for c in ws[2]:
            c.font = base
        ws["A2"].number_format = "DD-MMM-YYYY"
        ws["D2"].number_format = money

    total_row = last + 2
    label = "Today's P/L" if is_today else f"P/L for {day:%d %b %Y}"
    ws.cell(total_row, 3, label).font = bold
    total = ws.cell(total_row, 4, f"=SUM(D{first}:D{last})")
    total.font = bold
    total.number_format = money
    for col in (3, 4):
        ws.cell(total_row, col).border = Border(top=thin, bottom=Side(style="double", color="5F6B78"))

    note_row = total_row + 2
    notes = [
        "P/L as reported by Kotak Neo for the day's positions (paper accounts: simulated), "
        "before brokerage, taxes and other charges.",
        *notes,
    ]
    for i, n in enumerate(notes):
        cell = ws.cell(note_row + i, 1, n)
        cell.font = Font(name="Arial", size=9, italic=True, color="5F6B78")

    for col, width in zip("ABCD", (14, 24, 16, 16)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    wb.calculation.fullCalcOnLoad = True  # Excel/Sheets compute the total when the file opens
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def capture_traded_accounts() -> None:
    """After the market: save today's P/L for accounts TradeLab traded in."""
    today = trading.now_ist().date()
    with session_scope() as db:
        users = list(db.scalars(select(User.id)))
    for user_id in users:
        ids = sorted(trading.accounts_traded_today(user_id, today))
        if ids:
            try:
                capture(user_id, ids)
            except Exception:  # noqa: BLE001
                log.exception("Saving P/L failed for user %s", user_id)
