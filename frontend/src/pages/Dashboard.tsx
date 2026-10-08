import { motion } from "framer-motion";
import {
  AlarmClock, ArrowDownRight, ArrowUpRight, CandlestickChart, CheckCircle2, ChevronRight, CircleDashed,
  Flame, Landmark, LogOut as ExitIcon, PlayCircle, Power, TrendingUp, Wallet, XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "../components/AppShell";
import { Candles } from "../components/Candles";
import { Confirm } from "../components/Modal";
import { Button, Pill, Skeleton, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { countdown, hm, initials, money, pnlClass, signedMoney, timeOf } from "../lib/format";
import { useApi, useNow } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, AutomationSettings, AutomationStatus, BasketItem, LiveSnapshot, Run } from "../lib/types";

function PhasePill({ status }: { status: AutomationStatus }) {
  switch (status.phase) {
    case "open":
      return <Pill tone="bull" dot pulse>Market open</Pill>;
    case "preopen":
      return <Pill tone="warn" dot pulse>Pre-open</Pill>;
    case "weekend":
      return <Pill dot>Weekend</Pill>;
    case "holiday":
      return <Pill tone="warn" dot>{status.holiday ?? "Holiday"}</Pill>;
    default:
      return <Pill dot>Market closed</Pill>;
  }
}

function greeting() {
  const h = Number(new Date().toLocaleString("en-IN", { hour: "numeric", hour12: false, timeZone: "Asia/Kolkata" }));
  return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

function RunDot({ run }: { run?: Run }) {
  if (!run) return <div className="tl-dot"><CircleDashed size={16} /></div>;
  if (run.status === "running") return <div className="tl-dot run"><span className="spinner" style={{ width: 15, height: 15 }} /></div>;
  if (run.status === "failed") return <div className="tl-dot fail"><XCircle size={16} /></div>;
  return <div className="tl-dot done"><CheckCircle2 size={16} /></div>;
}

export function DashboardPage() {
  const { user } = useAuth();
  const toast = useToast();
  const now = useNow(1000);
  const status = useApi<AutomationStatus>("/automation/status", { interval: 15000 });
  const accounts = useApi<Account[]>("/accounts");
  const live = useApi<LiveSnapshot[]>("/accounts/live", { interval: 30000 });
  const basket = useApi<BasketItem[]>("/basket");
  const [confirm, setConfirm] = useState<"entry" | "exit" | null>(null);
  const [busy, setBusy] = useState(false);

  const s = status.data;
  const activeStocks = basket.data?.filter((b) => b.active) ?? [];
  const autoAccounts = accounts.data?.filter((a) => a.automation_enabled) ?? [];
  const totals = useMemo(() => {
    const ok = live.data?.filter((x) => x.ok) ?? [];
    return {
      funds: ok.reduce((a, x) => a + (x.available ?? 0), 0),
      pnl: ok.reduce((a, x) => a + (x.pnl ?? 0), 0),
      open: ok.reduce((a, x) => a + (x.open_positions ?? 0), 0),
      errors: (live.data?.length ?? 0) - ok.length,
    };
  }, [live.data]);

  const next = useMemo(() => {
    if (!s) return null;
    const items = [
      s.settings.auto_entry && s.next.entry ? { kind: "entry" as const, at: new Date(s.next.entry).getTime() } : null,
      s.settings.auto_exit && s.next.exit ? { kind: "exit" as const, at: new Date(s.next.exit).getTime() } : null,
    ].filter(Boolean) as { kind: "entry" | "exit"; at: number }[];
    return items.sort((a, b) => a.at - b.at)[0] ?? null;
  }, [s]);

  const toggle = async (patch: Partial<AutomationSettings>) => {
    if (!s) return;
    status.setData({ ...s, settings: { ...s.settings, ...patch } });
    try {
      await api("/automation/settings", { method: "PUT", body: patch });
      status.reload();
    } catch (e) {
      toast((e as Error).message, "error");
      status.reload();
    }
  };

  const launch = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      await api(confirm === "entry" ? "/automation/run-entry" : "/automation/square-off-all", { method: "POST" });
      toast(confirm === "entry" ? "Placing market orders now…" : "Squaring off every position…");
      setConfirm(null);
      setTimeout(() => {
        status.reload();
        live.reload();
      }, 2500);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const runsOf = (kind: "entry" | "exit") => s?.today_runs.filter((r) => r.kind === kind) ?? [];
  const firstName = user?.name.split(" ")[0] ?? "";

  return (
    <>
      <PageHeader title={`${greeting()}, ${firstName}`} subtitle={new Date().toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", timeZone: "Asia/Kolkata" })} action={s ? <PhasePill status={s} /> : null} />

      <div className="stack">
        <div className="grid grid-main">
          {/* Hero: next automated event */}
          <motion.div className="card hero" {...stagger(0)}>
            <Candles className="candles-bg" count={22} seed={3} />
            <div className="row between">
              <span className="row" style={{ gap: 8 }}>
                <span className="icon-bubble" style={{ width: 34, height: 34, borderRadius: 11 }}>
                  {next?.kind === "exit" ? <ExitIcon size={17} /> : <AlarmClock size={17} />}
                </span>
                <span className="event-label">
                  {!s ? "Loading schedule…" : next ? (next.kind === "entry" ? "Market buy fires in" : "Hard square-off in") : "Automation is paused"}
                </span>
              </span>
            </div>
            {!s ? (
              <div style={{ margin: "16px 0" }}><Skeleton h={48} w="60%" /></div>
            ) : next ? (
              <div className="hero-count num">{countdown(next.at - now)}</div>
            ) : (
              <div className="hero-count" style={{ fontSize: 30 }}>Switch on auto entry or exit</div>
            )}
            {s && (
              <p className="muted" style={{ margin: 0, maxWidth: 420, position: "relative" }}>
                {next?.kind === "entry"
                  ? `${new Date(next.at).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short", timeZone: "Asia/Kolkata" })} · ${hm(s.settings.entry_time)} · ${activeStocks.length} stock${activeStocks.length === 1 ? "" : "s"} × ${autoAccounts.length} account${autoAccounts.length === 1 ? "" : "s"} at market price${s.settings.sizing_mode === "equal" ? " · funds split equally" : ""}`
                  : next?.kind === "exit"
                    ? `Every open position in ${autoAccounts.length} account${autoAccounts.length === 1 ? "" : "s"} is closed at ${hm(s.settings.exit_time)}`
                    : "Nothing will be bought or sold automatically."}
              </p>
            )}
            <div className="row wrap hero-actions" style={{ marginTop: 18, position: "relative" }}>
              <Button onClick={() => setConfirm("entry")} disabled={!activeStocks.length || !autoAccounts.length}>
                <PlayCircle size={18} /> Buy basket now
              </Button>
              <Button variant="warn" onClick={() => setConfirm("exit")} disabled={!accounts.data?.length}>
                <Flame size={18} /> Square off all
              </Button>
            </div>
          </motion.div>

          {/* Automation switches */}
          <motion.div className="card" {...stagger(2)}>
            <div className="card-title">
              Autopilot <Link to="/settings" style={{ fontSize: 13 }}>Change times</Link>
            </div>
            <div className="stack" style={{ gap: 4 }}>
              <div className="row between" style={{ padding: "6px 0" }}>
                <div className="row">
                  <span className="icon-bubble"><PlayCircle size={19} /></span>
                  <div>
                    <div style={{ fontWeight: 700 }}>Market buy at {s ? hm(s.settings.entry_time) : "09:15"}</div>
                    <div className="muted" style={{ fontSize: 13 }}>Mon–Fri, all selected stocks</div>
                  </div>
                </div>
                <Switch checked={!!s?.settings.auto_entry} onChange={(v) => toggle({ auto_entry: v })} disabled={!s} label="Automatic entry" />
              </div>
              <hr className="divider" style={{ margin: "6px 0" }} />
              <div className="row between" style={{ padding: "6px 0" }}>
                <div className="row">
                  <span className="icon-bubble warn"><Power size={19} /></span>
                  <div>
                    <div style={{ fontWeight: 700 }}>Hard square-off at {s ? hm(s.settings.exit_time) : "14:30"}</div>
                    <div className="muted" style={{ fontSize: 13 }}>Cancels pending orders, closes everything</div>
                  </div>
                </div>
                <Switch checked={!!s?.settings.auto_exit} onChange={(v) => toggle({ auto_exit: v })} disabled={!s} label="Automatic square-off" />
              </div>
            </div>
          </motion.div>

        </div>
          {/* Stats */}
          <div className="stats-row">
            {[
              { label: "Total funds", icon: <Landmark size={16} />, value: live.data ? money(totals.funds, false) : null, hint: `${live.data?.length ?? 0} account${live.data?.length === 1 ? "" : "s"}` },
              { label: "Today's P&L", icon: <TrendingUp size={16} />, value: live.data ? signedMoney(totals.pnl) : null, cls: pnlClass(totals.pnl), hint: `${totals.open} open position${totals.open === 1 ? "" : "s"}` },
              { label: "Automated", icon: <Wallet size={16} />, value: accounts.data ? `${autoAccounts.length}/${accounts.data.length}` : null, hint: totals.errors ? `${totals.errors} need attention` : "accounts on autopilot", warn: totals.errors > 0 },
              { label: "Stocks", icon: <CandlestickChart size={16} />, value: basket.data ? String(activeStocks.length) : null, hint: "in today's basket" },
            ].map((t, i) => (
              <motion.div key={t.label} className="card stat" {...stagger(i + 1)}>
                <div className="label"><span className="icon-bubble">{t.icon}</span>{t.label}</div>
                <div className={`value num ${t.cls ?? ""}`}>{t.value ?? <Skeleton h={28} w="70%" />}</div>
                <div className="hint" style={t.warn ? { color: "var(--warn)" } : undefined}>{t.hint}</div>
              </motion.div>
            ))}
          </div>

        <div className="grid grid-main">
          {/* Accounts snapshot */}
          <motion.div className="card" {...stagger(5)}>
            <div className="card-title">
              Accounts <Link to="/accounts" className="row" style={{ gap: 2, fontSize: 13 }}>Manage <ChevronRight size={16} /></Link>
            </div>
            {!accounts.data ? (
              <div className="stack"><Skeleton h={44} /><Skeleton h={44} /></div>
            ) : accounts.data.length === 0 ? (
              <div className="banner bull">
                <Wallet size={18} />
                <span>Add your first Kotak Neo account to start. You log in once — TradeLab handles every day after that. <Link to="/accounts">Add account →</Link></span>
              </div>
            ) : (
              <div className="list">
                {accounts.data.slice(0, 6).map((a) => {
                  const l = live.data?.find((x) => x.id === a.id);
                  return (
                    <div className="list-item" key={a.id}>
                      <div className={`avatar ${a.broker === "paper" ? "paper" : ""} ${a.status === "error" || (l && !l.ok) ? "err" : ""}`}>{initials(a.name)}</div>
                      <div className="truncate" style={{ flex: 1 }}>
                        <div className="truncate" style={{ fontWeight: 700 }}>{a.name}</div>
                        <div className="muted truncate" style={{ fontSize: 13 }}>
                          {l?.ok ? `${money(l.available)} available` : l ? "Couldn't load — check login" : a.client_name ?? "…"}
                        </div>
                      </div>
                      <div style={{ textAlign: "right" }}>
                        {l?.ok ? (
                          <div className={`num ${pnlClass(l.pnl)}`} style={{ fontWeight: 800 }}>
                            {(l.pnl ?? 0) > 0 ? <ArrowUpRight size={14} /> : (l.pnl ?? 0) < 0 ? <ArrowDownRight size={14} /> : null} {signedMoney(l.pnl)}
                          </div>
                        ) : l ? (
                          <Pill tone="bear">Error</Pill>
                        ) : (
                          <Skeleton h={16} w={70} />
                        )}
                        {!a.automation_enabled && <div className="muted" style={{ fontSize: 12 }}>autopilot off</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </motion.div>
          {/* Today's timeline */}
          <motion.div className="card" {...stagger(3)}>
            <div className="card-title">
              Today <Link to="/activity" className="row" style={{ gap: 2, fontSize: 13 }}>Activity <ChevronRight size={16} /></Link>
            </div>
            {!s ? (
              <Skeleton h={90} />
            ) : (
              <div className="timeline">
                {!s.trading_day && (
                  <div className="banner" style={{ marginBottom: 14 }}>
                    <AlarmClock size={18} /> {s.phase === "holiday" ? `${s.holiday} — no trading today.` : "Weekend — markets are closed."}
                  </div>
                )}
                {(["entry", "exit"] as const).map((kind) => {
                  const runs = runsOf(kind);
                  const latest = runs[0];
                  return (
                    <div className="tl-item" key={kind}>
                      <RunDot run={latest} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="row between">
                          <b>{kind === "entry" ? "Entry" : "Square-off"}</b>
                          <span className="muted num" style={{ fontSize: 13 }}>{hm(kind === "entry" ? s.settings.entry_time : s.settings.exit_time)}</span>
                        </div>
                        <div className="muted" style={{ fontSize: 13.5 }}>
                          {latest
                            ? `${latest.trigger === "manual" ? "Manual" : latest.trigger === "catchup" ? "Catch-up" : "Scheduled"} · ${timeOf(latest.started_at)} · ${latest.status === "running" ? "running…" : latest.summary ?? latest.status}`
                            : (kind === "entry" ? s.settings.auto_entry : s.settings.auto_exit)
                              ? "Waiting"
                              : "Switched off"}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </motion.div>
        </div>
      </div>

      <Confirm
        open={confirm === "entry"}
        onClose={() => setConfirm(null)}
        onConfirm={launch}
        loading={busy}
        title="Buy basket now?"
        confirmLabel="Place market orders"
        body={
          <>
            This immediately places <b>market</b> orders for <b>{activeStocks.length}</b> stock{activeStocks.length === 1 ? "" : "s"} in <b>{autoAccounts.length}</b> automated account
            {autoAccounts.length === 1 ? "" : "s"} — outside the {s ? hm(s.settings.entry_time) : "09:15"} schedule.
          </>
        }
      />
      <Confirm
        open={confirm === "exit"}
        onClose={() => setConfirm(null)}
        onConfirm={launch}
        loading={busy}
        tone="warn"
        title="Square off everything?"
        confirmLabel="Square off now"
        body={<>Cancels pending orders and closes <b>every open position</b> at market price in all {accounts.data?.length ?? 0} accounts, right now.</>}
      />
    </>
  );
}
