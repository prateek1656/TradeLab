import { motion } from "framer-motion";
import {
  AlarmClock, ArrowDownRight, ArrowUpRight, CandlestickChart, CheckCircle2, ChevronRight, CircleDashed,
  Flame, Gem, Landmark, Layers, LogOut as ExitIcon, PiggyBank, Power, Rocket, TrendingUp, Wallet, XCircle, Zap,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { PageHeader } from "../components/AppShell";
import { Candles } from "../components/Candles";
import { LetsTrade } from "../components/LetsTrade";
import { ReportButton } from "../components/ReportButton";
import { Confirm } from "../components/Modal";
import { Button, Pill, Skeleton, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { countdown, dateOf, hm, initials, money, pnlClass, signedMoney, timeOf } from "../lib/format";
import { useApi, useNow } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, AutomationStatus, LiveSnapshot, Run, SectionKey, SectionState } from "../lib/types";

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

const SECTION_ICON: Record<SectionKey, typeof Zap> = { intraday: Zap, holding: PiggyBank, fno: Layers, commodity: Gem };
const NOUNS: Record<SectionKey, [string, string]> = { intraday: ["stock", "stocks"], holding: ["stock or ETF", "stocks & ETFs"], fno: ["contract", "contracts"], commodity: ["contract", "contracts"] };

/** One line per section: what it holds in its list and whether it will trade. */
function sectionLine(st: SectionState): string {
  const [one, many] = NOUNS[st.section];
  const n = st.stocks.length;
  const items = n ? `${n} ${n === 1 ? one : many}` : `No ${many} yet`;
  if (st.settings.scheduled) {
    const sess = st.session;
    if (sess?.status === "armed") return `${items} · confirmed for ${dateOf(st.trade_date!)}`;
    if (sess && ["firing", "done"].includes(sess.status)) return `${items} · traded ${dateOf(st.trade_date!)}`;
    return n ? `${items} · press Let's Trade to ${st.section === "holding" ? "buy" : "trade"}` : items;
  }
  if (!n) return items;
  return st.buy_blocked ? `${items} · market closed for buying` : `${items} · market open — tap Buy`;
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
  const [confirmExit, setConfirmExit] = useState(false);
  const [busy, setBusy] = useState(false);

  const s = status.data;
  const lt = s?.lets_trade;
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

  // Only events that will really place orders: a confirmed buy, or a square-off after TradeLab bought today.
  const next = useMemo(() => {
    if (!s || !lt) return null;
    const today = s.server_time.slice(0, 10);
    const items = [
      lt.session?.status === "armed" && !lt.buys_now ? { kind: "entry" as const, at: new Date(lt.entry_at).getTime() } : null,
      s.settings.auto_exit && s.traded_today ? { kind: "exit" as const, at: new Date(`${today}T${s.settings.exit_time}+05:30`).getTime() } : null,
    ].filter((x) => x && x.at > Date.now()) as { kind: "entry" | "exit"; at: number }[];
    return items.sort((a, b) => a.at - b.at)[0] ?? null;
  }, [s, lt]);

  const reload = () => {
    status.reload();
    live.reload();
    accounts.reload();
  };

  const squareOffAll = async () => {
    setBusy(true);
    try {
      await api("/automation/square-off-all", { method: "POST" });
      toast("Squaring off every position…");
      setConfirmExit(false);
      setTimeout(reload, 2500);
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleExit = async (v: boolean) => {
    if (!s) return;
    status.setData({ ...s, settings: { ...s.settings, auto_exit: v } });
    try {
      await api("/automation/settings", { method: "PUT", body: { auto_exit: v } });
    } catch (e) {
      toast((e as Error).message, "error");
    }
    status.reload();
  };

  // The timeline is Intraday's day (its buy and square-off); other sections show in Activity.
  const runsOf = (kind: "entry" | "exit") => s?.today_runs.filter((r) => r.kind === kind && r.section === "intraday") ?? [];
  const firstName = user?.name.split(" ")[0] ?? "";

  const sideTiles = [
    { label: "Automated", icon: <Wallet size={16} />, value: accounts.data ? `${autoAccounts.length}/${accounts.data.length}` : null, hint: totals.errors ? `${totals.errors} need attention` : "accounts on autopilot", warn: totals.errors > 0 },
    { label: "In your lists", icon: <CandlestickChart size={16} />, value: s ? String(Object.values(s.sections).reduce((a, x) => a + x.stocks.length, 0)) : null, hint: "across 4 sections", warn: false },
  ].map((t, i) => (
    <motion.div key={t.label} className="card stat" {...stagger(i + 4)}>
      <div className="label"><span className="icon-bubble">{t.icon}</span>{t.label}</div>
      <div className="value num">{t.value ?? <Skeleton h={28} w="70%" />}</div>
      <div className="hint" style={t.warn ? { color: "var(--warn)" } : undefined}>{t.hint}</div>
    </motion.div>
  ));

  return (
    <>
      <PageHeader title={`${greeting()}, ${firstName}`} subtitle={new Date().toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long", timeZone: "Asia/Kolkata" })} action={s ? <PhasePill status={s} /> : null} />

      <div className="stack">
        {/* Money first: total funds and today's P&L */}
        <div className="stats-row two">
          <motion.div className="card stat" {...stagger(0)}>
            <div className="label"><span className="icon-bubble"><Landmark size={16} /></span>Total funds</div>
            <div className="value num">{live.data ? money(totals.funds, false) : <Skeleton h={28} w="70%" />}</div>
            <div className="hint">{live.data?.length ?? 0} account{live.data?.length === 1 ? "" : "s"}</div>
          </motion.div>
          <motion.div className="card stat" {...stagger(1)}>
            <div className="label"><span className="icon-bubble"><TrendingUp size={16} /></span>Today's P&amp;L</div>
            <div className={`value num ${pnlClass(totals.pnl)}`}>{live.data ? signedMoney(totals.pnl) : <Skeleton h={28} w="70%" />}</div>
            <div className="hint">{totals.open} open position{totals.open === 1 ? "" : "s"}</div>
            {s && <ReportButton day={s.server_time.slice(0, 10)} label="Today's report" className="stat-action" />}
          </motion.div>
        </div>

        <div className="grid grid-main">
          {/* Hero: Let's Trade + next event */}
          <motion.div className="card hero" {...stagger(2)}>
            <Candles className="candles-bg" count={22} seed={3} />
            <div className="row" style={{ gap: 8, position: "relative" }}>
              <span className="icon-bubble" style={{ width: 34, height: 34, borderRadius: 11 }}>
                {next?.kind === "exit" ? <ExitIcon size={17} /> : next ? <AlarmClock size={17} /> : <Rocket size={17} />}
              </span>
              <span className="event-label">
                {!s ? "Loading…" : next ? (next.kind === "entry" ? "Market buy fires in" : "Hard square-off in") : "Ready when you are"}
              </span>
            </div>
            {!s || !lt ? (
              <div style={{ margin: "16px 0" }}><Skeleton h={48} w="60%" /></div>
            ) : next ? (
              <div className="hero-count num">{countdown(next.at - now)}</div>
            ) : (
              <div className="hero-count" style={{ fontSize: 28, lineHeight: 1.2 }}>
                {lt.session?.status === "armed" && lt.buys_now ? "Confirmed" : `Nothing is bought until you say so`}
              </div>
            )}
            {s && lt && (
              <p className="muted" style={{ margin: "0 0 16px", maxWidth: 460, position: "relative" }}>
                {next?.kind === "exit"
                  ? `Closes everything TradeLab bought today, at ${hm(s.settings.exit_time)}.`
                  : next?.kind === "entry"
                    ? `${dateOf(lt.trade_date)} · ${lt.session!.stocks.length} stock${lt.session!.stocks.length === 1 ? "" : "s"} × ${autoAccounts.length} account${autoAccounts.length === 1 ? "" : "s"} at market price${lt.session!.sizing_mode === "equal" ? " · funds split equally" : ""}`
                    : "Pick your stocks, then press Let's Trade and confirm. No confirmation, no orders."}
              </p>
            )}
            {s && lt && (
              <div style={{ position: "relative" }}>
                <LetsTrade state={lt} settings={s.settings} accounts={accounts.data} onChanged={reload} />
              </div>
            )}
          </motion.div>

          {/* Square-off controls */}
          <motion.div className="card" {...stagger(3)}>
            <div className="card-title">
              Selling <Link to="/settings" style={{ fontSize: 13 }}>Change times</Link>
            </div>
            <div className="stack" style={{ gap: 12 }}>
              <div className="row between">
                <div className="row">
                  <span className="icon-bubble warn"><Power size={19} /></span>
                  <div>
                    <div style={{ fontWeight: 700 }}>Intraday square-off at {s ? hm(s.settings.exit_time) : "14:30"}</div>
                    <div className="muted" style={{ fontSize: 13 }}>Only intraday stocks, only in accounts TradeLab bought in that day</div>
                  </div>
                </div>
                <Switch checked={!!s?.settings.auto_exit} onChange={toggleExit} disabled={!s} label="Automatic square-off" />
              </div>
              <Button variant="warn" block onClick={() => setConfirmExit(true)} disabled={!accounts.data?.length}>
                <Flame size={18} /> Square off everything now
              </Button>
            </div>
          </motion.div>
        </div>

        {/* Every section at a glance */}
        <motion.div className="card" {...stagger(4)}>
          <div className="card-title">
            Sections <Link to="/trade/intraday" className="row" style={{ gap: 2, fontSize: 13 }}>Trade <ChevronRight size={16} /></Link>
          </div>
          {!s ? (
            <Skeleton h={120} />
          ) : (
            <div className="list">
              {(["intraday", "holding", "fno", "commodity"] as const).map((key) => {
                const st = s.sections[key];
                const Icon = SECTION_ICON[key];
                return (
                  <Link key={key} to={`/trade/${key}`} className="list-item" style={{ color: "inherit", textDecoration: "none" }}>
                    <span className="icon-bubble"><Icon size={18} /></span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <b>{st.label}</b>
                      <div className="muted truncate" style={{ fontSize: 13 }}>{sectionLine(st)}</div>
                    </div>
                    {st.traded_today && <Pill tone="bull">Traded today</Pill>}
                    <ChevronRight size={17} className="muted" />
                  </Link>
                );
              })}
            </div>
          )}
        </motion.div>

        {/* Automated / lists */}
        <div className="stats-row two">
          {sideTiles}
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
            {!s || !lt ? (
              <Skeleton h={90} />
            ) : (
              <div className="timeline">
                {!s.trading_day && (
                  <div className="banner" style={{ marginBottom: 14 }}>
                    <AlarmClock size={18} /> {s.phase === "holiday" ? `${s.holiday} — no trading today.` : "Weekend — markets are closed."}
                  </div>
                )}
                {(["entry", "exit"] as const).map((kind) => {
                  const latest = runsOf(kind)[0];
                  const today = s.server_time.slice(0, 10);
                  const confirmedToday = lt.trade_date === today && lt.session?.status === "armed";
                  return (
                    <div className="tl-item" key={kind}>
                      <RunDot run={latest} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className="row between">
                          <b>{kind === "entry" ? "Buy" : "Square-off"}</b>
                          <span className="muted num" style={{ fontSize: 13 }}>{hm(kind === "entry" ? s.settings.entry_time : s.settings.exit_time)}</span>
                        </div>
                        <div className="muted" style={{ fontSize: 13.5 }}>
                          {latest
                            ? `${latest.trigger === "manual" ? "Let's Trade" : latest.trigger === "catchup" ? "Catch-up" : "Scheduled"} · ${timeOf(latest.started_at)} · ${latest.status === "running" ? "running…" : latest.summary ?? latest.status}`
                            : kind === "entry"
                              ? confirmedToday ? "Confirmed — waiting" : "Not confirmed — won't run"
                              : !s.settings.auto_exit
                                ? "Switched off"
                                : s.traded_today ? "Waiting" : "Only runs if TradeLab buys today"}
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
        open={confirmExit}
        onClose={() => setConfirmExit(false)}
        onConfirm={squareOffAll}
        loading={busy}
        tone="warn"
        title="Square off everything?"
        confirmLabel="Square off now"
        body={
          <div className="stack" style={{ gap: 10 }}>
            <span>Right now, in all {accounts.data?.length ?? 0} accounts:</span>
            <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 4 }}>
              <li>cancels pending intraday orders and closes <b>every intraday (MIS) position</b> at market price;</li>
              <li>closes the carry-forward F&amp;O and Commodity positions <b>TradeLab opened</b> (sells what it bought, buys back what it sold first);</li>
              <li>cancels today's Let's Trade confirmations that haven't bought yet.</li>
            </ul>
            <span className="muted" style={{ fontSize: 13 }}>Delivery holdings (your Holding stocks and ETFs, and anyone's own shares) are never sold.</span>
          </div>
        }
      />
    </>
  );
}
