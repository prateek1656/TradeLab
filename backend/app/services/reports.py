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
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import xlsxwriter
from sqlalchemy import String, cast, delete, func, literal, select

from ..broker.base import Position
from ..broker.manager import broker_manager
from ..db import session_scope
from ..models import BrokerAccount, PnlRecord, User
from . import trading

log = logging.getLogger(__name__)

RETENTION_DAYS = 14  # a report is deleted once it is 14 days old
SUMMARY_SHEET = "Summary"
FIRST_ROW = 5  # first stock row on every sheet (rows 1-4: title, date, blank, headers)
# Today's positions are read from the broker only on a trading day from this time on,
# so last session's positions are never saved under a new date.
LIVE_FROM = dtime(9, 0)


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
    pnl_complete: bool = True  # False: still open with no live price, P/L covers closed shares only


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


def today_is_live() -> bool:
    """Whether today's report is read live from the broker right now."""
    now = trading.now_ist()
    return trading.is_trading_day(now.date()) and now.time() >= LIVE_FROM


# ----------------------------------------------------------------------------- capture


def rows_from_positions(acc_id: int, acc_name: str, positions: list[Position]) -> list[Row]:
    """One row per stock traded today. A stock held under two products (say intraday
    and delivery) is listed once, with the quantities, amounts and P/L added up."""
    parts_by_symbol: dict[str, list[Position]] = {}
    for p in positions:
        if p.buy_qty or p.sell_qty:  # listed but not traded today -> left out
            parts_by_symbol.setdefault(p.trading_symbol, []).append(p)
    rows = []
    for symbol, parts in parts_by_symbol.items():
        buy_qty = sum(p.buy_qty for p in parts)
        sell_qty = sum(p.sell_qty for p in parts)
        pnl = 0.0
        for p in parts:
            # Without a price for the open shares, count only the closed ones.
            pnl += p.pnl if p.pnl is not None else (
                (p.sell_avg - p.buy_avg) * min(p.buy_qty, p.sell_qty) * (p.multiplier or 1))
        rows.append(Row(
            acc_id, acc_name, symbol, round(pnl, 2), buy_qty, sell_qty,
            sum(p.buy_avg * p.buy_qty for p in parts) / buy_qty if buy_qty else 0.0,
            sum(p.sell_avg * p.sell_qty for p in parts) / sell_qty if sell_qty else 0.0,
            all(p.pnl is not None for p in parts),
        ))
    return rows


def _account_rows(acc_id: int, acc_name: str) -> list[Row]:
    positions = trading.with_live_pnl(acc_id, broker_manager.call(acc_id, lambda b: b.positions()))
    return rows_from_positions(acc_id, acc_name, positions)


_capture_locks: dict[int, threading.Lock] = {}
_capture_guard = threading.Lock()


def _capture_lock(user_id: int) -> threading.Lock:
    with _capture_guard:
        return _capture_locks.setdefault(user_id, threading.Lock())


def _save_account(user_id: int, today: date, acc_id: int, acc_rows: list[Row]) -> None:
    # Update or add, never delete: if Kotak clears the day's positions in the
    # evening, what was saved after the square-off stays.
    now = datetime.now(timezone.utc)
    with session_scope() as db:
        saved = {
            rec.symbol: rec
            for rec in db.scalars(select(PnlRecord).where(
                PnlRecord.user_id == user_id, PnlRecord.trade_date == today, PnlRecord.account_id == acc_id))
        }
        for r in acc_rows:
            rec = saved.get(r.symbol)
            if rec is None:
                rec = saved[r.symbol] = PnlRecord(user_id=user_id, trade_date=today, account_id=acc_id,
                                                  symbol=r.symbol)
                db.add(rec)
            rec.account_name, rec.pnl, rec.pnl_complete = r.account, r.pnl, r.pnl_complete
            rec.buy_qty, rec.sell_qty, rec.buy_avg, rec.sell_avg = r.buy_qty, r.sell_qty, r.buy_avg, r.sell_avg
            rec.captured_at = now


def capture(user_id: int, account_ids: list[int] | None = None) -> tuple[list[Row], list[str]]:
    """Read today's trades from the broker for the user's accounts and save them.

    Returns the rows and a message for each account that couldn't be read or saved.
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
            return acc, [], f"{acc[1]}: couldn't read positions from the broker ({exc})"

    with ThreadPoolExecutor(max_workers=min(16, len(accounts))) as ex:
        results = list(ex.map(fetch, accounts))

    rows: list[Row] = []
    errors: list[str] = []
    # One save at a time per user (square-off, 15:40 job and downloads can overlap),
    # and one transaction per account so a problem in one never loses the others.
    with _capture_lock(user_id):
        for (acc_id, acc_name), acc_rows, err in results:
            if err:
                errors.append(err)
                continue  # keep whatever was saved earlier for this account
            try:
                _save_account(user_id, today, acc_id, acc_rows)
            except Exception as exc:  # noqa: BLE001
                log.exception("Saving P/L failed for account %s", acc_id)
                errors.append(f"{acc_name}: couldn't save today's trades ({exc.__class__.__name__})")
                continue
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
    """Today (once the market day has begun): live from the broker, saved as it goes.
    Any other day or time: what was saved."""
    if day == _check_day(day) and today_is_live():
        _, errors = capture(user_id)
        return saved_rows(user_id, day), errors
    return saved_rows(user_id, day), []


def saved_days(user_id: int) -> list[dict]:
    """Days with a saved report (newest first), within the two weeks that are kept."""
    # Accounts are told apart the way the workbook does: by id, deleted ones by name.
    account_key = func.coalesce(cast(PnlRecord.account_id, String), literal("deleted:") + PnlRecord.account_name)
    with session_scope() as db:
        rows = db.execute(
            select(PnlRecord.trade_date, func.sum(PnlRecord.pnl), func.count(), func.count(func.distinct(account_key)))
            .where(PnlRecord.user_id == user_id, PnlRecord.trade_date >= oldest_kept())
            .group_by(PnlRecord.trade_date)
            .order_by(PnlRecord.trade_date.desc())
        ).all()
    return [{"day": d.isoformat(), "pnl": round(total or 0.0, 2), "rows": n, "accounts": accts}
            for d, total, n, accts in rows]


# ----------------------------------------------------------------------------- workbook

MONEY = '#,##0.00;[Red]-#,##0.00;-'
PRICE = '#,##0.00##'  # 2 decimals, up to 4 when the average needs them
QTY = '#,##0'
FOOTNOTE = ("Buy and sell prices are average fill prices for the day. P/L as reported by Kotak Neo "
            "(paper accounts: simulated), before brokerage, taxes and other charges.")
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _CONTROL_CHARS.sub("", str(text))


def _round2(x: float) -> float:
    """Round like Excel's ROUND (halves away from zero)."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _cut(text: str, limit: int) -> str:
    """At most `limit` characters as Excel counts them (UTF-16 units: an emoji counts as 2)."""
    out, used = [], 0
    for ch in text:
        size = 2 if ord(ch) > 0xFFFF else 1
        if used + size > limit:
            break
        out.append(ch)
        used += size
    return "".join(out)


def sheet_names(names: list[str]) -> list[str]:
    """Valid, unique Excel sheet names (31 chars max, no []:*?/\\) for the account names, in order."""
    used = {SUMMARY_SHEET.lower(), "history"}  # "History" is reserved by Excel
    out = []
    for name in names:
        base = _clean(_BAD_SHEET_CHARS.sub("-", name)).strip().strip("'").strip()
        base = _cut(base, 31).rstrip().rstrip("'").rstrip() or "Account"
        candidate, n = base, 2
        while candidate.lower() in used:
            suffix = f" ({n})"
            candidate = _cut(base, 31 - len(suffix)).rstrip().rstrip("'").rstrip() + suffix
            n += 1
        used.add(candidate.lower())
        out.append(candidate)
    return out


def _natural(text: str) -> list:
    """Sort key so "Account 2" comes before "Account 10"."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", text.lower()) if t]


def _ref(sheet: str, cell: str) -> str:
    return "'" + sheet.replace("'", "''") + "'!" + cell


class _Styles:
    def __init__(self, wb):
        def fmt(**kw):
            return wb.add_format({"font_name": "Arial", "font_size": 11, **kw})

        head = {"bold": True, "bg_color": "#E8ECEF", "bottom": 1, "bottom_color": "#B8C0C8"}
        total = {"bold": True, "top": 1, "top_color": "#B8C0C8", "bottom": 6, "bottom_color": "#5F6B78"}
        self.text = fmt()
        self.title = fmt(bold=True, font_size=14)
        self.muted = fmt(font_size=10, font_color="#5F6B78")
        self.note = fmt(font_size=9, italic=True, font_color="#5F6B78")
        self.warn = fmt(font_color="#B42318")
        self.link = fmt(font_color="#0563C1", underline=1)
        self.head_left = fmt(**head)
        self.head_right = fmt(**head, align="right")
        self.qty = fmt(num_format=QTY)
        self.price = fmt(num_format=PRICE)
        self.money = fmt(num_format=MONEY)
        self.total_label = fmt(**total)
        self.total_money = fmt(**total, num_format=MONEY)


def _headers(ws, st: _Styles, labels: list[str]) -> None:
    for col, label in enumerate(labels):
        left = col == 0 or label == "Note"
        ws.write_string(FIRST_ROW - 2, col, label, st.head_left if left else st.head_right)


def _finish(ws, widths: list[int]) -> None:
    for col, width in enumerate(widths):
        ws.set_column(col, col, width)
    ws.freeze_panes(FIRST_ROW - 1, 0)
    ws.fit_to_pages(1, 0)


def _row_note(r: Row, formula_ok: bool) -> str | None:
    if not r.buy_qty and not r.sell_qty:
        return "Saved before quantities and prices were recorded"
    open_qty = r.buy_qty - r.sell_qty
    if open_qty > 0:
        return (f"{open_qty} share{'s' if open_qty != 1 else ''} still open (not sold)"
                + ("; P/L uses the last price" if r.pnl_complete else "; P/L counts only the shares already sold"))
    if open_qty < 0:
        n = -open_qty
        return (f"{n} share{'s' if n != 1 else ''} sold and not bought back today"
                + ("; P/L uses the last price" if r.pnl_complete else "; P/L counts only the shares bought back"))
    if not formula_ok:
        return "P/L as reported by Kotak Neo"
    return None


def _account_sheet(ws, st: _Styles, name: str, rows: list[Row], day: date) -> tuple[int, float]:
    """Fill one account's sheet; returns (Excel row number of its net profit, net profit)."""
    ws.write_string(0, 0, _clean(name), st.title)
    ws.write_string(1, 0, f"{day:%a, %d %b %Y}", st.muted)
    plan = []
    for r in rows:
        qty = max(r.buy_qty, r.sell_qty)
        value = _round2((r.sell_avg - r.buy_avg) * qty)
        closed = r.buy_qty == r.sell_qty and qty > 0
        formula_ok = closed and abs(value - r.pnl) <= 0.011
        plan.append((r, qty, value if formula_ok else r.pnl, _row_note(r, formula_ok)))
    has_notes = any(note for *_, note in plan)
    _headers(ws, st, ["Stock", "Quantity", "Buy price (₹)", "Sell price (₹)", "P/L (₹)"] + (["Note"] if has_notes else []))
    for i, (r, qty, pnl, note) in enumerate(plan):
        row, n = FIRST_ROW - 1 + i, FIRST_ROW + i  # 0-based index, Excel row number
        ws.write_string(row, 0, _clean(r.symbol.removesuffix("-EQ")), st.text)
        if qty:
            ws.write_number(row, 1, qty, st.qty)
        else:
            ws.write_blank(row, 1, None, st.qty)
        if r.buy_qty:
            ws.write_number(row, 2, r.buy_avg, st.price)
        else:
            ws.write_blank(row, 2, None, st.price)
        if r.sell_qty:
            ws.write_number(row, 3, r.sell_avg, st.price)
        else:
            ws.write_blank(row, 3, None, st.price)
        # Closed positions: P/L = (sell - buy) × quantity as a live formula. Anything else
        # (still open, or Kotak's figure differs) shows Kotak's number with a note.
        # Formulas carry their result too, so previews and Protected View show the numbers.
        if note is None:
            ws.write_formula(row, 4, f"=ROUND((D{n}-C{n})*B{n},2)", st.money, pnl)
        else:
            ws.write_number(row, 4, pnl, st.money)
            ws.write_string(row, 5, note, st.muted)
    last = FIRST_ROW + len(rows) - 1
    net_n = last + 2
    net = _round2(sum(pnl for _, _, pnl, _ in plan))
    ws.write_string(net_n - 1, 3, "Net profit", st.total_label)
    ws.write_formula(net_n - 1, 4, f"=SUM(E{FIRST_ROW}:E{last})", st.total_money, net)
    ws.write_string(net_n + 1, 0, FOOTNOTE, st.note)
    _finish(ws, [18, 11, 15, 15, 15] + ([48] if has_notes else []))
    return net_n, net


def build_xlsx(rows: list[Row], day: date, problems: list[str] | None = None) -> bytes:
    """Summary sheet, then one sheet per account: Stock | Quantity | Buy | Sell | P/L, net profit at the end.

    `problems` are accounts that couldn't be read; they are listed in red on the Summary.
    """
    problems = problems or []
    # Group by account (deleted accounts by their saved name); the latest name wins.
    groups: dict[object, tuple[str, list[Row]]] = {}
    for r in rows:
        key = r.account_id if r.account_id is not None else ("deleted", r.account)
        _, acc_rows = groups.get(key, ("", []))
        groups[key] = (r.account, acc_rows + [r])
    accounts = sorted(groups.values(), key=lambda g: _natural(g[0]))
    names = sheet_names([name for name, _ in accounts])

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False})
    st = _Styles(wb)
    summary = wb.add_worksheet(SUMMARY_SHEET)
    summary.write_string(0, 0, "TradeLab P/L report", st.title)
    summary.write_string(1, 0, f"{day:%a, %d %b %Y} · {len(accounts)} account{'s' if len(accounts) != 1 else ''}",
                         st.muted)
    _headers(summary, st, ["Account", "Stocks", "Net profit (₹)"])

    total = 0.0
    for i, ((name, acc_rows), sheet) in enumerate(zip(accounts, names)):
        acc_rows = sorted(acc_rows, key=lambda r: r.symbol)
        net_n, net = _account_sheet(wb.add_worksheet(sheet), st, name, acc_rows, day)
        total += net
        row = FIRST_ROW - 1 + i
        # The tab's name, so two accounts with the same name can be told apart; it links to the tab.
        summary.write_url(row, 0, "internal:" + _ref(sheet, "A1"), st.link, string=sheet)
        stocks = _ref(sheet, f"A{FIRST_ROW}:A{FIRST_ROW + len(acc_rows) - 1}")
        summary.write_formula(row, 1, f"=COUNTA({stocks})", st.qty, len(acc_rows))
        summary.write_formula(row, 2, f"={_ref(sheet, f'E{net_n}')}", st.money, net)

    next_row = FIRST_ROW - 1 + len(accounts)
    if not accounts and not problems:
        summary.write_string(next_row, 0, "No trades on this day", st.muted)
        summary.write_number(next_row, 2, 0, st.money)
        next_row += 1
    for problem in problems:  # in the table, in red, so a missing account can't go unnoticed
        summary.write_string(next_row, 0, "Not included: " + _clean(problem), st.warn)
        next_row += 1
    last = max(FIRST_ROW, FIRST_ROW - 1 + len(accounts))
    total_row = next_row + 1
    summary.write_string(total_row, 0, "Total net profit", st.total_label)
    summary.write_blank(total_row, 1, None, st.total_label)
    summary.write_formula(total_row, 2, f"=SUM(C{FIRST_ROW}:C{last})", st.total_money, _round2(total))
    summary.write_string(total_row + 2, 0, FOOTNOTE, st.note)
    _finish(summary, [30, 10, 18])
    summary.activate()

    wb.close()  # XlsxWriter marks the file to recalculate when Excel opens it
    return buf.getvalue()


def workbook_for(user_id: int, day: date) -> tuple[bytes, list[str]]:
    """The day's workbook and any accounts that couldn't be read."""
    rows, problems = report_rows(user_id, day)
    return build_xlsx(rows, day, problems), problems


def filename_for(day: date) -> str:
    return f"TradeLab-PnL-{day.isoformat()}.xlsx"


def build_zip(user_id: int) -> tuple[bytes, str, list[str]]:
    """Every kept day's workbook in one zip; today's is included once the market day has begun."""
    today = trading.now_ist().date()
    days = {date.fromisoformat(d["day"]) for d in saved_days(user_id)}
    if today_is_live():
        days.add(today)
    if not days:
        raise ReportGone("There are no reports to download yet.")
    days_sorted = sorted(days)
    buf = io.BytesIO()
    problems: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for day in days_sorted:
            content, day_problems = workbook_for(user_id, day)
            problems += day_problems
            zf.writestr(filename_for(day), content)
    first, last = days_sorted[0].isoformat(), days_sorted[-1].isoformat()
    name = f"TradeLab-PnL-{first}-to-{last}.zip" if first != last else f"TradeLab-PnL-{first}.zip"
    return buf.getvalue(), name, problems
