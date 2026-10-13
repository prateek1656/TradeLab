import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CalendarCheck2, CheckCircle2, Rocket, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { dateOf, hm, timeOf } from "../lib/format";
import { useToast } from "../lib/toast";
import type { Account, LetsTradeState, SectionKey } from "../lib/types";
import { Sheet } from "./Modal";
import { Button, Pill } from "./ui";

/** Times a scheduled section trades at (Intraday's legacy settings or a section's own). */
export interface Timing {
  entry_time: string | null;
  exit_time: string | null;
  auto_exit: boolean;
}

const NOUN: Record<string, [string, string]> = { intraday: ["stock", "stocks"], holding: ["ETF", "ETFs"] };

/** The big "Let's Trade" button. Nothing is ever bought without it. */
export function LetsTrade({
  state,
  settings,
  accounts,
  onChanged,
}: {
  state: LetsTradeState;
  settings: Timing;
  accounts: Account[] | null;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const section: SectionKey = state.section ?? "intraday";
  const holding = section === "holding";
  const [one, many] = NOUN[section] ?? ["item", "items"];
  const count = (n: number) => `${n} ${n === 1 ? one : many}`;
  const entry = hm(settings.entry_time ?? "09:15");
  const sess = state.session;
  const day = dateOf(state.trade_date);
  // Exactly the accounts the server will confirm (part of the fingerprint sent with "Yes").
  const autoAccounts = (accounts ?? []).filter((a) => state.account_ids.includes(a.id));
  const shorts = new Set(state.shorts);
  const blocked = !state.stocks.length
    ? `Add at least one ${one} first`
    : !state.autopilot_accounts
      ? holding ? "Choose at least one autopilot account for Holding first" : "Switch on autopilot for an account first"
      : state.closing_soon
        ? holding
          ? "Too late to buy today — confirm again for the next trading day"
          : `Too close to the ${hm(settings.exit_time ?? "14:30")} square-off — confirm the next day after it`
        : null;

  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ run_id: number | null }>("/automation/lets-trade", {
        body: { trade_date: state.trade_date, fingerprint: state.fingerprint, section },
      });
      toast(r.run_id ? "Placing market orders now…" : `Confirmed — TradeLab ${holding ? "buys" : "trades"} at ${entry} on ${day}`);
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
      await api("/automation/lets-trade/cancel", { body: { trade_date: date ?? state.trade_date, section } });
      toast(`Cancelled — nothing will be bought on ${dateOf(date ?? state.trade_date)}`);
      onChanged();
    } catch (e) {
      toast((e as Error).message, "error");
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  const ask = () => {
    setError(null);
    setAsking(true);
    onChanged(); // show the very latest list, accounts and timing in the dialog
  };

  const armed = sess?.status === "armed";
  const preparing = sess?.status === "running";
  const finished = sess && ["firing", "done"].includes(sess.status);
  const didntTrade = sess && ["skipped", "missed"].includes(sess.status); // can be confirmed again

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
            <div className="muted" style={{ fontSize: 13.5 }}>Orders go out at {entry}. You can still cancel.</div>
          </div>
          <Button size="sm" variant="ghost" onClick={() => cancel()} loading={busy}><X size={15} /> Cancel</Button>
        </div>
      ) : finished ? (
        <div className="lt-status">
          <CheckCircle2 size={20} className="up" />
          <div>
            <b>{sess!.status === "firing" ? "Sending orders…" : `${day}: traded`}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>{sess!.note ?? "See Activity for every order."}</div>
          </div>
        </div>
      ) : armed && !sess!.changed ? (
        <div className="lt-status">
          <CalendarCheck2 size={20} className="up" />
          <div style={{ flex: "1 1 180px", minWidth: 0 }}>
            <b>Confirmed for {day}</b>
            <div className="muted" style={{ fontSize: 13.5 }}>
              {count(sess!.stocks.length)} · buys at {entry} · confirmed {timeOf(sess!.confirmed_at)}
            </div>
          </div>
          <Button size="sm" variant="ghost" onClick={() => cancel()} loading={busy}><X size={15} /> Cancel</Button>
        </div>
      ) : (
        <>
          {armed && sess!.changed && (
            <div className="banner warn" style={{ marginBottom: 12, fontSize: 13.5, flexWrap: "wrap" }}>
              <AlertTriangle size={17} />
              <span style={{ flex: "1 1 200px" }}>You changed {many}, accounts or money settings after confirming. The confirmed {count(sess!.stocks.length)} will still trade on {day} unless you confirm again or cancel.</span>
              <Button size="sm" variant="ghost" onClick={() => cancel()} loading={busy}><X size={15} /> Cancel it</Button>
            </div>
          )}
          {didntTrade && (
            <div className="lt-status" style={{ marginBottom: 12 }}>
              <AlertTriangle size={20} style={{ color: "var(--warn)" }} />
              <div style={{ flex: "1 1 180px", minWidth: 0 }}>
                <b>No trade on {day} yet</b>
                <div className="muted" style={{ fontSize: 13.5 }}>{sess!.note ?? "Nothing was bought."} You can confirm again below.</div>
              </div>
            </div>
          )}
          <motion.button
            className="lt-button"
            whileTap={{ scale: 0.98 }}
            disabled={!!blocked}
            onClick={ask}
          >
            <Rocket size={24} />
            <span>
              <span className="lt-title">{armed ? "Let's Trade with the new list" : "Let's Trade"}</span>
              <span className="lt-sub">
                {blocked ?? (state.buys_now ? `${holding ? "Buys" : "Trades"} ${count(state.stocks.length)} now — market is open` : `${day} · ${count(state.stocks.length)} · ${holding ? "buys" : "trades"} at ${entry}`)}
              </span>
            </span>
          </motion.button>
          {blocked && !state.closing_soon && (
            <div className="muted" style={{ fontSize: 13, marginTop: 8, textAlign: "center" }}>
              {!state.stocks.length ? <Link to={`/trade/${section}`}>Add {many} →</Link> : <Link to="/accounts">Open accounts →</Link>}
            </div>
          )}
        </>
      )}

      <Sheet open={asking} onClose={() => setAsking(false)} title="Are you sure?" subtitle={state.buys_now ? "The market is open: orders go out the moment you confirm." : `Nothing is bought until you say yes.`}>
        <div className="stack">
          <div className="lt-confirm-day">
            {state.buys_now ? "Place orders now at market price" : <>{holding ? "Buy" : "Trade"} on <b>{day}</b> at {entry}</>}
          </div>
          <div className="lt-row">
            <span className="muted">{holding ? "ETFs" : "Stocks"}</span>
            <div className="chip-select">
              {state.stocks.map((s) => (shorts.has(s) ? <Pill key={s} tone="bear">{s} · short sell</Pill> : <Pill key={s}>{s} · buy</Pill>))}
            </div>
          </div>
          <div className="lt-row">
            <span className="muted">Accounts</span>
            <div className="chip-select">{autoAccounts.map((a) => <Pill key={a.id}>{a.name}</Pill>)}</div>
          </div>
          <div className="lt-row">
            <span className="muted">Money</span>
            <span>
              {state.sizing_mode === "equal"
                ? `The money free in each account split equally between its ${many}${state.reserve_pct ? `, keeping ${state.reserve_pct}% aside` : ""}${holding ? " (cash only, for delivery)" : ""}`
                : `Each ${one}'s own size`}
            </span>
          </div>
          <div className="lt-row">
            <span className="muted">Sell</span>
            <span>
              {holding
                ? "Kept for delivery. Nothing is sold until you tap Sell."
                : settings.auto_exit
                  ? `Squared off at ${hm(settings.exit_time ?? "14:30")}: every intraday (MIS) stock position in the accounts TradeLab bought in, and their pending intraday orders`
                  : "Automatic square-off is OFF — Kotak squares intraday positions off itself at 15:05 (with its charges)"}
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
