"""Daily profit & loss reports (Excel), kept for two weeks.

Each day's workbook has a Summary sheet and one sheet per account listing every
stock traded that day: quantity, average buy and sell price, P/L, and the
account's net profit at the bottom. The workbooks are built on demand from the
small `pnl_records` table, so nothing is stored as a file; records older than
`RETENTION_DAYS` are deleted.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.hyperlink import Hyperlink
from sqlalchemy import delete, func, select

from ..broker.manager import broker_manager
from ..db import session_scope
from ..models import BrokerAccount, PnlRecord, User
from . import trading

log = logging.getLogger(__name__)

RETENTION_DAYS = 14  # a report is deleted once it is 14 days old
SUMMARY_SHEET = "Summary"
FIRST_ROW = 5  # first stock row on every sheet (rows 1-4: title, date, blank, headers)


class ReportGone(Exception):
    """The requested day is outside the two weeks of kept reports (or in the future)."""


@dataclass
class Row:
    account_id: int | None
    account: str
    symbol: str
    pnl: float
    buy_qty: int = 0
    sell_qty: int = 0
    buy_avg: float = 0.0
    sell_avg: float = 0.0
    pnl_complete: bool = True  # False: still open with no live price, P/L covers sold shares only


# ----------------------------------------------------------------------------- retention


def oldest_kept(today: date | None = None) -> date:
    """The oldest day whose report still exists (today and the 13 days before it)."""
    return (today or trading.now_ist().date()) - timedelta(days=RETENTION_DAYS - 1)


def purge_old(today: date | None = None) -> int:
    """Delete every user's P/L records older than two weeks. Returns how many rows went."""
    with session_scope() as db:
        gone = db.execute(delete(PnlRecord).where(PnlRecord.trade_date < oldest_kept(today))).rowcount
    if gone:
        log.info("Deleted %s P/L records older than %s days", gone, RETENTION_DAYS)
    return gone or 0


def _check_day(day: date) -> date:
    today = trading.now_ist().date()
    if day > today:
        raise ReportGone("There is no report for a day that hasn't happened yet.")
    if day < oldest_kept(today):
        raise ReportGone(f"Reports are kept for {RETENTION_DAYS} days; this one was deleted automatically.")
    return today


# ----------------------------------------------------------------------------- capture


def _account_rows(acc_id: int, acc_name: str) -> list[Row]:
    positions = trading.with_live_pnl(acc_id, broker_manager.call(acc_id, lambda b: b.positions()))
    rows = []
    for p in positions:
        if not (p.buy_qty or p.sell_qty):
            continue  # listed but not traded today
        complete = p.pnl is not None
        # Without a price for the open shares, count only what was already sold.
        pnl = p.pnl if complete else (p.sell_avg - p.buy_avg) * min(p.buy_qty, p.sell_qty)
        rows.append(Row(acc_id, acc_name, p.trading_symbol, round(pnl, 2),
                        p.buy_qty, p.sell_qty, p.buy_avg, p.sell_avg, complete))
    return rows


def capture(user_id: int, account_ids: list[int] | None = None) -> tuple[list[Row], list[str]]:
    """Read today's trades from the broker for the user's accounts and save them.

    Returns the rows and a message for each account that couldn't be read.
    """
    purge_old()
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
    now = datetime.now(timezone.utc)
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
                    rec = PnlRecord(user_id=user_id, trade_date=today, account_id=r.account_id, symbol=r.symbol)
                    db.add(rec)
                rec.account_name, rec.pnl, rec.pnl_complete = r.account, r.pnl, r.pnl_complete
                rec.buy_qty, rec.sell_qty, rec.buy_avg, rec.sell_avg = r.buy_qty, r.sell_qty, r.buy_avg, r.sell_avg
                rec.captured_at = now
            rows.extend(acc_rows)
    return rows, errors


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


# ----------------------------------------------------------------------------- reading


def saved_rows(user_id: int, day: date) -> list[Row]:
    with session_scope() as db:
        recs = db.scalars(
            select(PnlRecord)
            .where(PnlRecord.user_id == user_id, PnlRecord.trade_date == day)
            .order_by(PnlRecord.captured_at, PnlRecord.id)
        ).all()
        return [Row(r.account_id, r.account_name, r.symbol, r.pnl, r.buy_qty or 0, r.sell_qty or 0,
                    r.buy_avg or 0.0, r.sell_avg or 0.0, r.pnl_complete is not False) for r in recs]


def report_rows(user_id: int, day: date) -> tuple[list[Row], list[str]]:
    """Today: live from the broker (saved as it goes). Earlier days: what was saved."""
    if day == _check_day(day):
        _, errors = capture(user_id)
        return saved_rows(user_id, day), errors
    return saved_rows(user_id, day), []


def saved_days(user_id: int) -> list[dict]:
    """Days with a saved report (newest first), within the two weeks that are kept."""
    with session_scope() as db:
        rows = db.execute(
            select(PnlRecord.trade_date, func.sum(PnlRecord.pnl), func.count(),
                   func.count(func.distinct(PnlRecord.account_name)))
            .where(PnlRecord.user_id == user_id, PnlRecord.trade_date >= oldest_kept())
            .group_by(PnlRecord.trade_date)
            .order_by(PnlRecord.trade_date.desc())
        ).all()
    return [{"day": d.isoformat(), "pnl": round(total or 0.0, 2), "rows": n, "accounts": accts}
            for d, total, n, accts in rows]


# ----------------------------------------------------------------------------- workbook

_FONT = Font(name="Arial", size=11)
_BOLD = Font(name="Arial", size=11, bold=True)
_TITLE = Font(name="Arial", size=14, bold=True)
_MUTED = Font(name="Arial", size=10, color="5F6B78")
_NOTE = Font(name="Arial", size=9, italic=True, color="5F6B78")
_LINK = Font(name="Arial", size=11, color="0563C1", underline="single")
_HEAD_FILL = PatternFill("solid", fgColor="E8ECEF")
_THIN = Side(style="thin", color="B8C0C8")
_DOUBLE = Side(style="double", color="5F6B78")
MONEY = '#,##0.00;[Red]-#,##0.00;-'
PRICE = '#,##0.00##'  # 2 decimals, up to 4 when the average needs them
QTY = '#,##0'
FOOTNOTE = ("Buy and sell prices are average fill prices for the day. P/L as reported by Kotak Neo "
            "(paper accounts: simulated), before brokerage, taxes and other charges.")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _text(ws, row: int, col: int, value: str, font: Font = _FONT):
    """A text cell that is never read as a formula (an account called "=..." stays text)."""
    cell = ws.cell(row, col)
    cell.value = ILLEGAL_CHARACTERS_RE.sub("", str(value))
    cell.data_type = "s"
    cell.font = font
    return cell


def sheet_names(names: list[str]) -> list[str]:
    """Valid, unique Excel sheet names (31 chars max, no []:*?/\\) for the account names, in order."""
    used = {SUMMARY_SHEET.lower(), "history"}  # "History" is reserved by Excel
    out = []
    for name in names:
        base = ILLEGAL_CHARACTERS_RE.sub("", _BAD_SHEET_CHARS.sub("-", name)).strip().strip("'").strip()
        base = base[:31].rstrip().rstrip("'").rstrip() or "Account"
        candidate, n = base, 2
        while candidate.lower() in used:
            suffix = f" ({n})"
            candidate = base[: 31 - len(suffix)].rstrip().rstrip("'").rstrip() + suffix
            n += 1
        used.add(candidate.lower())
        out.append(candidate)
    return out


def _ref(sheet: str, cell: str) -> str:
    return "'" + sheet.replace("'", "''") + "'!" + cell


def _headers(ws, labels: list[str]) -> None:
    for col, label in enumerate(labels, start=1):
        c = _text(ws, FIRST_ROW - 1, col, label, _BOLD)
        c.fill = _HEAD_FILL
        c.border = Border(bottom=_THIN)
        c.alignment = Alignment(horizontal="left" if col == 1 or label == "Note" else "right")


def _total(ws, row: int, label_col: int, value_col: int, label: str, formula: str) -> None:
    _text(ws, row, label_col, label, _BOLD)
    cell = ws.cell(row, value_col, formula)
    cell.font = _BOLD
    cell.number_format = MONEY
    for col in range(label_col, value_col + 1):
        ws.cell(row, col).border = Border(top=_THIN, bottom=_DOUBLE)


def _finish(ws, widths: list[int]) -> None:
    for i, width in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = width
    ws.freeze_panes = f"A{FIRST_ROW}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToHeight = 0


def _row_note(r: Row, formula_ok: bool) -> str | None:
    open_qty = r.buy_qty - r.sell_qty
    if open_qty > 0:
        return (f"{open_qty} share{'s' if open_qty != 1 else ''} still open (not sold)"
                + ("; P/L uses the last price" if r.pnl_complete else "; P/L counts only the shares already sold"))
    if open_qty < 0:
        n = -open_qty
        return (f"{n} share{'s' if n != 1 else ''} sold short, not bought back"
                + ("; P/L uses the last price" if r.pnl_complete else "; P/L counts only the shares already bought back"))
    if not formula_ok:
        return "P/L as reported by Kotak Neo"
    return None


def _account_sheet(ws, name: str, rows: list[Row], day: date) -> int:
    """Fill one account's sheet; returns the row holding its net profit."""
    _text(ws, 1, 1, name, _TITLE)
    _text(ws, 2, 1, f"{day:%a, %d %b %Y}", _MUTED)
    notes = []
    for r in rows:
        qty = max(r.buy_qty, r.sell_qty)
        closed = r.buy_qty == r.sell_qty and qty > 0
        formula_ok = closed and abs((r.sell_avg - r.buy_avg) * qty - r.pnl) <= 0.05
        notes.append(_row_note(r, formula_ok))
    has_notes = any(notes)
    labels = ["Stock", "Quantity", "Buy price (₹)", "Sell price (₹)", "P/L (₹)"] + (["Note"] if has_notes else [])
    _headers(ws, labels)
    for i, (r, note) in enumerate(zip(rows, notes)):
        row = FIRST_ROW + i
        _text(ws, row, 1, r.symbol.removesuffix("-EQ"))
        ws.cell(row, 2, max(r.buy_qty, r.sell_qty)).number_format = QTY
        ws.cell(row, 3, r.buy_avg if r.buy_qty else None).number_format = PRICE
        ws.cell(row, 4, r.sell_avg if r.sell_qty else None).number_format = PRICE
        # Closed positions: P/L = (sell - buy) × quantity, as a formula. Anything else
        # (still open, or Kotak's figure differs) shows Kotak's number with a note.
        ws.cell(row, 5, f"=ROUND((D{row}-C{row})*B{row},2)" if note is None else r.pnl).number_format = MONEY
        if note:
            _text(ws, row, 6, note, _MUTED)
        for col in range(2, 6):
            ws.cell(row, col).font = _FONT
    last = FIRST_ROW + len(rows) - 1
    net_row = last + 2
    _total(ws, net_row, 4, 5, "Net profit", f"=SUM(E{FIRST_ROW}:E{last})")
    _text(ws, net_row + 2, 1, FOOTNOTE, _NOTE)
    _finish(ws, [18, 11, 15, 15, 15] + ([46] if has_notes else []))
    return net_row


def build_xlsx(rows: list[Row], day: date, is_today: bool, notes: list[str]) -> bytes:
    """Summary sheet, then one sheet per account: Stock | Quantity | Buy | Sell | P/L, net profit at the end."""
    # Group by account (deleted accounts by their saved name); the latest name wins.
    groups: dict[object, tuple[str, list[Row]]] = {}
    for r in rows:
        key = r.account_id if r.account_id is not None else ("deleted", r.account)
        _, acc_rows = groups.get(key, ("", []))
        groups[key] = (r.account, acc_rows + [r])
    accounts = sorted(groups.values(), key=lambda g: g[0].lower())
    names = sheet_names([name for name, _ in accounts])

    wb = Workbook()
    summary = wb.active
    summary.title = SUMMARY_SHEET
    _text(summary, 1, 1, "TradeLab P/L report", _TITLE)
    _text(summary, 2, 1, f"{day:%a, %d %b %Y} · {len(accounts)} account{'s' if len(accounts) != 1 else ''}", _MUTED)
    _headers(summary, ["Account", "Stocks", "Net profit (₹)"])

    for i, ((name, acc_rows), sheet) in enumerate(zip(accounts, names)):
        ws = wb.create_sheet(sheet)
        acc_rows = sorted(acc_rows, key=lambda r: r.symbol)
        net_row = _account_sheet(ws, name, acc_rows, day)
        row = FIRST_ROW + i
        # The tab's name, so two accounts with the same name can be told apart; it links to the tab.
        link = _text(summary, row, 1, sheet, _LINK)
        link.hyperlink = Hyperlink(ref=link.coordinate, location=_ref(sheet, "A1"), display=sheet)
        c = summary.cell(row, 2, f"=COUNTA({_ref(sheet, f'A{FIRST_ROW}:A{FIRST_ROW + len(acc_rows) - 1}')})")
        c.font, c.number_format = _FONT, QTY
        c = summary.cell(row, 3, f"={_ref(sheet, f'E{net_row}')}")
        c.font, c.number_format = _FONT, MONEY

    if accounts:
        last = FIRST_ROW + len(accounts) - 1
    else:
        _text(summary, FIRST_ROW, 1, "No trades on this day", _MUTED)
        summary.cell(FIRST_ROW, 3, 0).number_format = MONEY
        last = FIRST_ROW
    total_row = last + 2
    _total(summary, total_row, 1, 3, "Today's net profit" if is_today else "Total net profit",
           f"=SUM(C{FIRST_ROW}:C{last})")
    for i, note in enumerate([FOOTNOTE, *notes]):
        _text(summary, total_row + 2 + i, 1, note, _NOTE)
    _finish(summary, [30, 10, 18])

    wb.calculation.fullCalcOnLoad = True  # Excel/Sheets compute the formulas when the file opens
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def workbook_for(user_id: int, day: date) -> bytes:
    rows, errors = report_rows(user_id, day)
    return build_xlsx(rows, day, day == trading.now_ist().date(), errors)


def filename_for(day: date) -> str:
    return f"TradeLab-PnL-{day.isoformat()}.xlsx"


def build_zip(user_id: int) -> tuple[bytes, str]:
    """Every kept day's workbook (today included, read live) in one zip file."""
    today = trading.now_ist().date()
    days = sorted({date.fromisoformat(d["day"]) for d in saved_days(user_id)} | {today})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for day in days:
            zf.writestr(filename_for(day), workbook_for(user_id, day))
    name = (f"TradeLab-PnL-{days[0].isoformat()}-to-{days[-1].isoformat()}.zip" if len(days) > 1
            else f"TradeLab-PnL-{days[0].isoformat()}.zip")
    return buf.getvalue(), name
