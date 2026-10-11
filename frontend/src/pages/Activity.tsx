import { AnimatePresence, motion } from "framer-motion";
import { Activity as ActivityIcon, AlertTriangle, Ban, CheckCircle2, ChevronDown, Download, FileSpreadsheet, LogOut, PlayCircle, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { PageHeader } from "../components/AppShell";
import { ReportButton } from "../components/ReportButton";
import { Button, Empty, Pill, Segmented, Skeleton, stagger } from "../components/ui";
import { download, reportToast } from "../lib/api";
import { useToast } from "../lib/toast";
import { dateOf, pnlClass, signedMoney, timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import { SECTION_LABEL } from "../components/SectionKit";
import type { LogEntry, ReportList, Run, SectionKey } from "../lib/types";

const statusTone = { done: "bull", partial: "warn", failed: "bear", running: "info" } as const;
const statusText = { done: "Done", partial: "Partly failed", failed: "Failed", running: "Running" } as const;

/** What a run did, in words: "Buy", "Sell", "Square-off" or "Square off everything". */
export function runTitle(run: Run): string {
  if (run.section === "all") return "Square off everything";
  // F&O and Commodity can buy or sell first, and their Exit sells or buys back.
  const twoWay = run.section === "fno" || run.section === "commodity";
  if (run.kind === "entry") return run.section === "intraday" ? "Entry" : twoWay ? "Trade" : "Buy";
  return run.section === "intraday" ? "Square-off" : twoWay ? "Exit" : "Sell";
}

export function ActivityPage() {
  const [filter, setFilter] = useState<"all" | SectionKey>("all");
  const runs = useApi<Run[]>("/automation/runs?limit=80", { interval: 10000 });
  const [openId, setOpenId] = useState<number | null>(null);

  const groups = useMemo(() => {
    const list = (runs.data ?? []).filter((r) => filter === "all" || r.section === filter);
    const map = new Map<string, Run[]>();
    for (const r of list) map.set(r.trading_date, [...(map.get(r.trading_date) ?? []), r]);
    return [...map.entries()];
  }, [runs.data, filter]);

  return (
    <>
      <PageHeader title="Activity" subtitle="Every automated and manual run, order by order" />
      <ReportsCard />
      <div style={{ marginBottom: 16 }}>
        <div className="section-tabs" style={{ margin: 0 }}>
          <Segmented value={filter} onChange={setFilter} options={[
            { value: "all", label: "All" }, { value: "intraday", label: "Intraday" }, { value: "holding", label: "Holding" },
            { value: "fno", label: "F&O" }, { value: "commodity", label: "Commodity" },
          ]} />
        </div>
      </div>
      {!runs.data ? (
        <div className="stack">{[0, 1, 2].map((i) => <Skeleton key={i} h={72} r={20} />)}</div>
      ) : groups.length === 0 ? (
        <div className="card"><Empty icon={<ActivityIcon size={24} />} title="Nothing yet">{filter === "all" ? "Every buy, sell and square-off appears here." : `${SECTION_LABEL[filter]} hasn't traded yet.`}</Empty></div>
      ) : (
        <div className="stack">
          {groups.map(([day, list], gi) => (
            <motion.div key={day} {...stagger(gi)}>
              <div className="muted" style={{ fontWeight: 700, fontSize: 13, margin: "6px 4px 8px", textTransform: "uppercase", letterSpacing: ".06em" }}>{dateOf(day)}</div>
              <div className="card" style={{ padding: 6 }}>
                {list.map((r) => (
                  <RunRow key={r.id} run={r} open={openId === r.id} onToggle={() => setOpenId(openId === r.id ? null : r.id)} />
                ))}
              </div>
            </motion.div>
          ))}
        </div>
      )}
    </>
  );
}

function RunRow({ run, open, onToggle }: { run: Run; open: boolean; onToggle: () => void }) {
  return (
    <div className="run-row">
      <button onClick={onToggle} className="row" style={{ width: "100%", background: "none", border: 0, padding: "12px 10px", cursor: "pointer", textAlign: "left" }}>
        <span className={`icon-bubble ${run.kind === "exit" ? "warn" : ""}`}>{run.kind === "entry" ? <PlayCircle size={19} /> : <LogOut size={19} />}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="row wrap" style={{ gap: 8, rowGap: 2 }}>
            <b>{runTitle(run)}</b>
            {run.section !== "all" && <Pill>{SECTION_LABEL[run.section] ?? run.section}</Pill>}
            <span className="muted" style={{ fontSize: 13 }}>{timeOf(run.started_at)} · {run.trigger === "manual" ? "manual" : run.trigger === "catchup" ? "catch-up" : run.trigger === "command" ? "on your command" : "scheduled"}</span>
          </div>
          <div className="muted truncate" style={{ fontSize: 13.5 }}>{run.summary ?? "In progress…"}</div>
        </div>
        <Pill tone={statusTone[run.status]} dot pulse={run.status === "running"}>{statusText[run.status]}</Pill>
        <ChevronDown size={18} className="muted" style={{ transform: open ? "rotate(180deg)" : undefined, transition: "transform .2s" }} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} style={{ overflow: "hidden" }}>
            <RunLogs runId={run.id} running={run.status === "running"} />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function RunLogs({ runId, running }: { runId: number; running: boolean }) {
  const logs = useApi<LogEntry[]>(`/automation/logs?run_id=${runId}&limit=500`, { interval: running ? 4000 : undefined });
  if (!logs.data) return <div style={{ padding: 12 }}><Skeleton h={50} /></div>;
  if (!logs.data.length) return <div className="muted" style={{ padding: "4px 16px 16px", fontSize: 14 }}>{running ? "Working…" : "No orders were needed."}</div>;
  return (
    <div className="list" style={{ padding: "0 10px 8px 58px" }}>
      {logs.data.map((l) => (
        <div key={l.id} className="list-item" style={{ alignItems: "flex-start", gap: 10, padding: "10px 0" }}>
          <span style={{ marginTop: 2 }}>
            {l.status === "success" ? <CheckCircle2 size={17} className="up" />
              : l.status === "failed" ? <XCircle size={17} className="down" />
              : l.status === "unknown" || l.status === "pending" ? <AlertTriangle size={17} style={{ color: "var(--warn)" }} />
              : <Ban size={17} className="muted" />}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="row wrap" style={{ gap: 6 }}>
              <b style={{ fontSize: 14 }}>{l.account_name}</b>
              {l.symbol && (
                <span className="num" style={{ fontSize: 14 }}>
                  <span className={l.action === "buy" ? "up" : l.action === "sell" ? "down" : "muted"} style={{ fontWeight: 800, textTransform: "uppercase", fontSize: 12 }}>{l.action}</span> {l.symbol}
                  {l.quantity ? <span className="muted"> × {l.quantity}</span> : null}
                </span>
              )}
            </div>
            {l.message && <div className="muted" style={{ fontSize: 13, wordBreak: "break-word" }}>{l.message}</div>}
            {l.order_id && <div className="muted num" style={{ fontSize: 12 }}>Order #{l.order_id}</div>}
          </div>
          <span className="muted num" style={{ fontSize: 12 }}>{timeOf(l.created_at)}</span>
        </div>
      ))}
    </div>
  );
}

function ReportsCard() {
  const toast = useToast();
  // Refreshed every minute and when the app comes back, so days that pass 2 weeks drop off.
  const reports = useApi<ReportList>("/reports", { interval: 60_000 });
  const [all, setAll] = useState(false);
  const [zipping, setZipping] = useState(false);
  const data = reports.data;
  // Today is offered on trading days (read live from Kotak); earlier days are what was saved after each square-off.
  const days = data
    ? data.days.some((d) => d.day === data.today) || !data.today_live
      ? data.days
      : [{ day: data.today, pnl: null as number | null, rows: 0, accounts: 0 }, ...data.days]
    : [];
  const shown = all ? days : days.slice(0, 5);

  const downloadAll = async () => {
    setZipping(true);
    try {
      const headers = await download("/reports/all.zip", `TradeLab-PnL-${data?.today ?? "reports"}.zip`);
      toast(...reportToast(headers, `Downloaded ${days.length} report${days.length === 1 ? "" : "s"} (zip)`));
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setZipping(false);
      reports.reload();
    }
  };

  return (
    <motion.div className="card" style={{ marginBottom: 16 }} {...stagger(0)}>
      <div className="card-title">
        <span className="row" style={{ gap: 8 }}><FileSpreadsheet size={18} /> Daily P&amp;L reports</span>
      </div>
      <p className="muted" style={{ margin: "-4px 0 10px", fontSize: 13.5 }}>
        One Excel file per day, with a sheet for every account: stocks, quantity, buy and sell price, P/L and net profit.
      </p>
      {!data ? (
        <Skeleton h={48} />
      ) : days.length === 0 ? (
        <div className="muted" style={{ fontSize: 13.5 }}>
          No reports yet. Each trading day's report appears here once TradeLab trades.
          <div style={{ fontSize: 12, marginTop: 8 }}>Excel reports are deleted automatically after 2 weeks.</div>
        </div>
      ) : (
        <div className="list">
          {shown.map((d) => (
            <div className="list-item" key={d.day}>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontWeight: 700 }}>{d.day === data.today ? "Today" : dateOf(d.day)}</div>
                <div className="muted" style={{ fontSize: 13 }}>
                  {d.pnl === null
                    ? "Live from Kotak Neo"
                    : `${d.accounts} account${d.accounts === 1 ? "" : "s"} · ${d.rows} stock${d.rows === 1 ? "" : "s"}${d.day === data.today && data.today_live ? " · updates when you download" : ""}`}
                </div>
              </div>
              {d.pnl !== null && <b className={`num ${pnlClass(d.pnl)}`}>{signedMoney(d.pnl)}</b>}
              <ReportButton day={d.day} label="Excel" onDone={reports.reload} />
            </div>
          ))}
          {days.length > 5 && (
            <button className="btn btn-ghost sm" style={{ alignSelf: "center", marginTop: 6 }} onClick={() => setAll(!all)}>
              {all ? "Show fewer" : `Show all ${days.length} days`}
            </button>
          )}
          <Button block onClick={downloadAll} loading={zipping} style={{ marginTop: 14 }}>
            {!zipping && <Download size={17} />} Download all{days.length > 1 ? ` (${days.length} days)` : ""}
          </Button>
          <div className="muted" style={{ fontSize: 12, textAlign: "center", marginTop: 8 }}>
            Excel reports are deleted automatically after 2 weeks.
          </div>
        </div>
      )}
    </motion.div>
  );
}
