import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CalendarCheck2, CheckCircle2, Rocket, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { dateOf, hm, timeOf } from "../lib/format";
import { useToast } from "../lib/toast";
import type { Account, AutomationSettings, LetsTradeState } from "../lib/types";
import { Sheet } from "./Modal";
import { Button, Pill } from "./ui";

/** The big "Let's Trade" button. Nothing is ever bought without it. */
export function LetsTrade({
  state,
  settings,
  accounts,
  onChanged,
}: {
  state: LetsTradeState;
  settings: AutomationSettings;
  accounts: Account[] | null;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sess = state.session;
  const day = dateOf(state.trade_date);
  const autoAccounts = (accounts ?? []).filter((a) => a.automation_enabled);
  const blocked = !state.stocks.length
    ? "Add at least one stock first"
    : !state.autopilot_accounts
      ? "Switch on autopilot for an account first"
      : state.closing_soon
        ? `Too close to the ${hm(settings.exit_time)} square-off — confirm the next day after it`
        : null;

  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ run_id: number | null }>("/automation/lets-trade", { body: { trade_date: state.trade_date } });
      toast(r.run_id ? "Placing market orders now…" : `Confirmed — TradeLab buys at ${hm(settings.entry_time)} on ${day}`);
      setAsking(false);
      onChanged();
      if (r.run_id) setTimeout(onChanged, 3000);
    } catch (e) {
      setError((e as Error).message);
      onChanged(); // the day or state may have moved on; show the latest
    } finally {
      setBusy(false);
    }
  };

  const cancel = async (date?: string) => {
    setBusy(true);
    try {
      await api("/automation/lets-trade/cancel", { body: { trade_date: date ?? state.trade_date } });
      toast(`Cancelled — nothing will be bought on ${dateOf(date ?? state.trade_date)}`);
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };

  const armed = sess?.status === "armed";
  const preparing = sess?.status === "running";
  const finished = sess && ["firing", "done", "skipped"].includes(sess.status);

  return (
    <div className="lets-trade">
      {state.other_confirmed.map((d) => (
        <div key={d} className="lt-status" style={{ marginBottom: 10 }}>
          <CalendarCheck2 size={20} className="up" />
          <div style={{ flex: "1 1 180px", minWidth: 0 }}>
            <b>Also confirmed for {dateOf(d)}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>Buys on that day unless you cancel it.</div>
          </div>
          <Button size="sm" variant="ghost" onClick={() => cancel(d)} loading={busy}><X size={15} /> Cancel</Button>
        </div>
      ))}
      {preparing ? (
        <div className="lt-status">
          <span className="spinner" style={{ width: 18, height: 18, color: "var(--bull)" }} />
          <div style={{ flex: "1 1 180px", minWidth: 0 }}>
            <b>Getting ready for {day}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>Orders go out at {hm(settings.entry_time)}. You can still cancel.</div>
          </div>
          <Button size="sm" variant="ghost" onClick={() => cancel()} loading={busy}><X size={15} /> Cancel</Button>
        </div>
      ) : finished ? (
        <div className="lt-status">
          <CheckCircle2 size={20} className="up" />
          <div>
            <b>{sess!.status === "skipped" ? `No trade on ${day}` : sess!.status === "firing" ? "Sending orders…" : `${day}: traded`}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>{sess!.note ?? "See Activity for every order."}</div>
          </div>
        </div>
      ) : armed && !sess!.changed ? (
        <div className="lt-status">
          <CalendarCheck2 size={20} className="up" />
          <div style={{ flex: "1 1 180px", minWidth: 0 }}>
            <b>Confirmed for {day}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>
              {sess!.stocks.length} stock{sess!.stocks.length === 1 ? "" : "s"} · buys at {hm(settings.entry_time)} · confirmed {timeOf(sess!.confirmed_at)}
            </div>
          </div>
          <Button size="sm" variant="ghost" onClick={() => cancel()} loading={busy}><X size={15} /> Cancel</Button>
        </div>
      ) : (
        <>
          {armed && sess!.changed && (
            <div className="banner warn" style={{ marginBottom: 12, fontSize: 13.5 }}>
              <AlertTriangle size={17} />
              <span>You changed your stocks or money settings after confirming. The confirmed {sess!.stocks.length} stock{sess!.stocks.length === 1 ? "" : "s"} will trade unless you confirm again.</span>
            </div>
          )}
          <motion.button
            className="lt-button"
            whileTap={{ scale: 0.98 }}
            disabled={!!blocked}
            onClick={() => { setError(null); setAsking(true); }}
          >
            <Rocket size={24} />
            <span>
              <span className="lt-title">{armed ? "Let's Trade with the new list" : "Let's Trade"}</span>
              <span className="lt-sub">
                {blocked ?? (state.buys_now ? `Buys ${state.stocks.length} stock${state.stocks.length === 1 ? "" : "s"} now — market is open` : `${day} · ${state.stocks.length} stock${state.stocks.length === 1 ? "" : "s"} · buys at ${hm(settings.entry_time)}`)}
              </span>
            </span>
          </motion.button>
          {blocked && !state.closing_soon && (
            <div className="muted" style={{ fontSize: 13, marginTop: 8, textAlign: "center" }}>
              {!state.stocks.length ? <Link to="/stocks">Add stocks →</Link> : <Link to="/accounts">Open accounts →</Link>}
            </div>
          )}
        </>
      )}

      <Sheet open={asking} onClose={() => setAsking(false)} title="Are you sure?" subtitle={state.buys_now ? "The market is open: orders go out the moment you confirm." : `Nothing is bought until you say yes.`}>
        <div className="stack">
          <div className="lt-confirm-day">
            {state.buys_now ? "Buy now at market price" : <>Trade on <b>{day}</b> at {hm(settings.entry_time)}</>}
          </div>
          <div className="lt-row">
            <span className="muted">Stocks</span>
            <div className="chip-select">{state.stocks.map((s) => <Pill key={s}>{s}</Pill>)}</div>
          </div>
          <div className="lt-row">
            <span className="muted">Accounts</span>
            <div className="chip-select">{autoAccounts.map((a) => <Pill key={a.id}>{a.name}</Pill>)}</div>
          </div>
          <div className="lt-row">
            <span className="muted">Money</span>
            <span>
              {state.sizing_mode === "equal"
                ? `Each account's money split equally between its stocks${state.reserve_pct ? `, keeping ${state.reserve_pct}% aside` : ""}`
                : "Each stock's own size"}
            </span>
          </div>
          <div className="lt-row">
            <span className="muted">Sell</span>
            <span>
              {settings.auto_exit ? `Everything TradeLab bought is squared off at ${hm(settings.exit_time)}` : "Automatic square-off is OFF — positions stay open"}
            </span>
          </div>
          <AnimatePresence>
            {error && (
              <motion.div className="banner error" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                <AlertTriangle size={17} /> {error}
              </motion.div>
            )}
          </AnimatePresence>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={() => setAsking(false)}>Not now</Button>
            <Button onClick={go} loading={busy}><Rocket size={17} /> Yes, let's trade</Button>
          </div>
        </div>
      </Sheet>
    </div>
  );
}
