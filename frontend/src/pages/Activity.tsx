import { AnimatePresence, motion } from "framer-motion";
import { Activity as ActivityIcon, AlertTriangle, Ban, CheckCircle2, ChevronDown, LogOut, PlayCircle, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { PageHeader } from "../components/AppShell";
import { Empty, Pill, Segmented, Skeleton, stagger } from "../components/ui";
import { dateOf, timeOf } from "../lib/format";
import { useApi } from "../lib/hooks";
import type { LogEntry, Run } from "../lib/types";

const statusTone = { done: "bull", partial: "warn", failed: "bear", running: "info" } as const;
const statusText = { done: "Done", partial: "Partly failed", failed: "Failed", running: "Running" } as const;

export function ActivityPage() {
  const [filter, setFilter] = useState<"all" | "entry" | "exit">("all");
  const runs = useApi<Run[]>("/automation/runs?limit=60", { interval: 10000 });
  const [openId, setOpenId] = useState<number | null>(null);

  const groups = useMemo(() => {
    const list = (runs.data ?? []).filter((r) => filter === "all" || r.kind === filter);
    const map = new Map<string, Run[]>();
    for (const r of list) map.set(r.trading_date, [...(map.get(r.trading_date) ?? []), r]);
    return [...map.entries()];
  }, [runs.data, filter]);

  return (
    <>
      <PageHeader title="Activity" subtitle="Every automated and manual run, order by order" />
      <div style={{ marginBottom: 16 }}>
        <Segmented value={filter} onChange={setFilter} options={[{ value: "all", label: "All" }, { value: "entry", label: "Entries" }, { value: "exit", label: "Square-offs" }]} />
      </div>
      {!runs.data ? (
        <div className="stack">{[0, 1, 2].map((i) => <Skeleton key={i} h={72} r={20} />)}</div>
      ) : groups.length === 0 ? (
        <div className="card"><Empty icon={<ActivityIcon size={24} />} title="Nothing yet">Runs appear here after the first 09:15 entry or 14:30 square-off.</Empty></div>
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
          <div className="row" style={{ gap: 8 }}>
            <b>{run.kind === "entry" ? "Entry" : "Square-off"}</b>
            <span className="muted" style={{ fontSize: 13 }}>{timeOf(run.started_at)} · {run.trigger === "manual" ? "manual" : run.trigger === "catchup" ? "catch-up" : "scheduled"}</span>
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
