import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, ArrowLeft, Check, ChevronDown, Coins, Info, Layers, Plus, Search, ShoppingCart, Trash2, TrendingDown, Users, Wallet } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { dateOf, money, pnlClass, signedMoney } from "../lib/format";
import { useApi, useDebounced } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, BasketItem, CommandPreview, LiveSnapshot, Run, SectionInfo, SectionKey, SectionPositionRow, SectionPositions } from "../lib/types";
import { Sheet } from "./Modal";
import { Button, Empty, Input, Pill, Segmented, Skeleton, Spinner, Switch, stagger } from "./ui";

export const SECTION_LABEL: Record<string, string> = { intraday: "Intraday", holding: "Holding", fno: "F&O", commodity: "Commodity", all: "Everything" };

/** One section's settings, saved straight away when changed. */
export function useSectionInfo(section: SectionKey) {
  const toast = useToast();
  const res = useApi<{ sections: SectionInfo[] }>("/sections");
  const info = res.data?.sections.find((s) => s.section === section) ?? null;
  const save = async (patch: Record<string, unknown>) => {
    if (!res.data || !info) return null;
    const before = res.data;
    try {
      const updated = await api<SectionInfo>(`/sections/${section}`, { method: "PUT", body: patch });
      res.setData({ sections: before.sections.map((s) => (s.section === section ? updated : s)) });
      return updated;
    } catch (e) {
      toast((e as Error).message, "error");
      res.setData(before);
      return null;
    }
  };
  return { info, save, reload: res.reload };
}

/** Follows a background run and says how it went once it has finished. */
export function useRunWatcher(onFinished?: () => void) {
  const toast = useToast();
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  return (runId: number, what: string) => {
    let tries = 0;
    const poll = async () => {
      tries += 1;
      try {
        const run = await api<Run>(`/automation/runs/${runId}`);
        if (run.status !== "running") {
          const tone = run.status === "done" ? "success" : "error";
          toast(`${what}: ${run.summary ?? run.status}`, tone);
          onFinished?.();
          return;
        }
      } catch {
        /* keep trying for a while */
      }
      if (tries < 60) timers.current.push(window.setTimeout(poll, 1500));
    };
    timers.current.push(window.setTimeout(poll, 1200));
  };
}

// ───────────────────────────────────────────── accounts

export function SectionAccountsCard({ info, accounts, onSave }: { info: SectionInfo | null; accounts: Account[] | null; onSave: (patch: Record<string, unknown>) => void }) {
  // Quick taps build on the latest local choice (not a reply still on its way).
  const [local, setLocal] = useState<number[] | null | undefined>(undefined);
  useEffect(() => setLocal(undefined), [info?.account_ids]);
  if (!info || !accounts) return <Skeleton h={110} r={20} />;
  const current = local === undefined ? info.account_ids : local;
  const all = current === null;
  const picked = current ?? [];
  const toggle = (id: number) => {
    const next = picked.includes(id) ? picked.filter((x) => x !== id) : [...picked, id];
    setLocal(next);
    onSave({ account_ids: next });
  };
  const trading = accounts.filter((a) => a.automation_enabled && (all || picked.includes(a.id)));
  return (
    <div className="card">
      <div className="card-title">
        <span className="row"><span className="icon-bubble"><Users size={18} /></span> Accounts for {info.label}</span>
        <Pill tone={trading.length ? "bull" : "warn"}>{trading.length} trading</Pill>
      </div>
      <div className="row between" style={{ gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <b style={{ fontSize: 14 }}>Every autopilot account</b>
          <div className="muted" style={{ fontSize: 12.5 }}>Or pick the accounts {info.label} should trade in</div>
        </div>
        <Switch checked={all} onChange={(v) => {
          const ids = accounts.filter((a) => a.automation_enabled).map((a) => a.id);
          setLocal(v ? null : ids);
          onSave(v ? { clear_account_ids: true } : { account_ids: ids });
        }} label={`All accounts for ${info.label}`} />
      </div>
      {!all && (
        <div className="chip-select" style={{ marginTop: 12 }}>
          {accounts.map((a) => {
            const on = picked.includes(a.id);
            return (
              <button key={a.id} type="button" className={`chip ${on ? "on" : ""}`} onClick={() => toggle(a.id)} title={a.automation_enabled ? undefined : "Autopilot is off for this account"} style={a.automation_enabled ? undefined : { opacity: 0.6 }}>
                {on && <Check size={14} />} {a.name}{!a.automation_enabled && " · autopilot off"}
              </button>
            );
          })}
        </div>
      )}
      {!all && picked.length === 0 && (
        <div className="banner warn" style={{ marginTop: 12, fontSize: 13 }}><AlertTriangle size={16} /> No accounts chosen — {info.label} is paused.</div>
      )}
    </div>
  );
}

// ───────────────────────────────────────────── money

export function MoneyCard({ info, accounts, live, items, onSave, onPreview }: {
  info: SectionInfo | null;
  accounts: Account[] | null;
  live: LiveSnapshot[] | null;
  items: BasketItem[] | null;
  onSave: (patch: Record<string, unknown>) => void;
  onPreview?: () => void;
}) {
  const [reserve, setReserve] = useState("");
  const [maxLots, setMaxLots] = useState("");
  useEffect(() => {
    if (info) {
      setReserve(String(info.reserve_pct));
      setMaxLots(String(info.max_lots));
    }
  }, [info?.reserve_pct, info?.max_lots]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!info) return <Skeleton h={140} r={20} />;
  const equal = info.sizing_mode === "equal";
  const lots = info.lots;
  const noun = lots ? "contract" : "ETF";
  const chosen = (accounts ?? []).filter((a) => a.automation_enabled && (info.account_ids === null || info.account_ids.includes(a.id)));
  const active = (items ?? []).filter((b) => b.active);
  const commitReserve = () => {
    const v = Math.min(90, Math.max(0, Number(reserve) || 0));
    setReserve(String(v));
    if (v !== info.reserve_pct) onSave({ reserve_pct: v });
  };
  const commitLots = () => {
    const v = Math.min(25, Math.max(1, Math.round(Number(maxLots) || 1)));
    setMaxLots(String(v));
    if (v !== info.max_lots) onSave({ max_lots: v });
  };

  return (
    <motion.div className="card" style={{ marginTop: 16 }} {...stagger(1)}>
      <div className="card-title">
        <span className="row"><span className="icon-bubble"><Coins size={18} /></span> Money per {noun}</span>
        {onPreview && <Button size="sm" variant="ghost" onClick={onPreview} disabled={!active.length || !chosen.length}><ShoppingCart size={15} /> Preview</Button>}
      </div>
      <Segmented block tone="bull" value={info.sizing_mode} onChange={(v) => onSave({ sizing_mode: v })}
        options={[{ value: "equal", label: "Split money equally" }, { value: "custom", label: lots ? "Fixed lots" : "Set per ETF" }]} />
      <div className="stack" style={{ gap: 12, marginTop: 14 }}>
        <div className="muted" style={{ fontSize: 13.5 }}>
          {equal
            ? lots
              ? "When you tap Buy, the money free in each account is divided equally between the switched-on contracts, and TradeLab buys as many lots as that part covers (using Kotak's margin for one lot)."
              : "At the buy time, the cash free in each account is divided equally between the switched-on ETFs, and TradeLab buys as many units as fit. Pledged-share margin isn't used for delivery."
            : lots
              ? "Each contract buys its own number of lots — never more than the money free covers."
              : "Each ETF uses its own size — units, a ₹ amount or a % of the cash free. Tap Edit on an ETF to set it."}
        </div>
        {equal && chosen.length > 0 && active.length > 0 && (
          <div className="list">
            {chosen.map((a) => {
              const l = live?.find((x) => x.id === a.id);
              const n = active.filter((b) => !b.account_ids?.length || b.account_ids.includes(a.id)).length;
              // Delivery buys (Holding) use cash only; contracts can use all available margin.
              const base = info.product === "CNC" ? (l?.cash_free ?? l?.available ?? 0) : (l?.available ?? 0);
              const usable = l?.ok ? Math.max(0, base) * (1 - info.reserve_pct / 100) : null;
              return (
                <div key={a.id} className="list-item" style={{ padding: "9px 2px" }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b className="truncate">{a.name}</b>
                    <div className="muted num" style={{ fontSize: 12.5 }}>
                      {l ? (l.ok ? `${money(usable, false)} free ÷ ${n} ${noun}${n === 1 ? "" : "s"}` : "Couldn't read funds") : "Checking funds…"}
                    </div>
                  </div>
                  <div className="num" style={{ fontWeight: 800, textAlign: "right" }}>
                    {usable !== null && n > 0 ? money(usable / n, false) : "—"}
                    <div className="muted" style={{ fontSize: 11.5, fontWeight: 600 }}>per {noun}</div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {lots && (
          <div className="field">
            <span className="label-text">Order type</span>
            <Segmented block value={info.product} onChange={(v) => onSave({ product: v })}
              options={[{ value: "NRML", label: "Carry forward (NRML)" }, { value: "MIS", label: "Intraday (MIS)" }]} />
            <span className="help">
              {info.product === "NRML" ? "Positions stay open until you tap Sell (or the contract expires)." : `Kotak closes intraday positions itself near the close (${info.section === "commodity" ? "about 11:00 PM" : "about 3:20 PM"}).`}
            </span>
          </div>
        )}
        <div className="row between" style={{ gap: 12 }}>
          <div style={{ minWidth: 0 }}>
            <b style={{ fontSize: 14 }}>Keep aside</b>
            <div className="muted" style={{ fontSize: 12.5 }}>Part of the free money left untraded</div>
          </div>
          <div style={{ width: 110, flexShrink: 0 }}>
            <Input className="sm" type="number" inputMode="decimal" min={0} max={90} value={reserve} onChange={(e) => setReserve(e.target.value)} onBlur={commitReserve}
              onKeyDown={(e) => e.key === "Enter" && (e.currentTarget as HTMLInputElement).blur()}
              suffix={<span className="muted" style={{ paddingRight: 10, fontWeight: 700 }}>%</span>} style={{ paddingRight: 30 }} />
          </div>
        </div>
        {lots && (
          <div className="row between" style={{ gap: 12 }}>
            <div style={{ minWidth: 0 }}>
              <b style={{ fontSize: 14 }}>Most lots per order</b>
              <div className="muted" style={{ fontSize: 12.5 }}>A safety cap for every account</div>
            </div>
            <div style={{ width: 110, flexShrink: 0 }}>
              <Input className="sm" type="number" inputMode="numeric" min={1} max={25} value={maxLots} onChange={(e) => setMaxLots(e.target.value)} onBlur={commitLots}
                onKeyDown={(e) => e.key === "Enter" && (e.currentTarget as HTMLInputElement).blur()}
                suffix={<span className="muted" style={{ paddingRight: 10, fontWeight: 700 }}>lots</span>} style={{ paddingRight: 44 }} />
            </div>
          </div>
        )}
      </div>
    </motion.div>
  );
}

// ───────────────────────────────────────────── list items

export function Ticker({ symbol }: { symbol: string }) {
  const label = symbol.replace(/[^A-Z0-9&]/gi, "").slice(0, 4);
  return <div className="ticker-badge" style={{ fontSize: label.length >= 4 ? 11 : 13 }}>{label}</div>;
}

export function ItemCard({ item, info, today, index, onToggle, onEdit, onRemove }: {
  item: BasketItem;
  info: SectionInfo | null;
  today: string;
  index: number;
  onToggle: (v: boolean) => void;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const expired = !!item.expiry && item.expiry < today;
  const lots = info?.lots;
  const equal = info?.sizing_mode !== "custom";
  return (
    <motion.div className="card" layout {...stagger(index)} exit={{ opacity: 0, scale: 0.95 }} style={{ opacity: item.active ? 1 : 0.62 }}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <Ticker symbol={item.symbol} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <b style={{ fontSize: 16 }}>{item.symbol}</b>
          <div className="muted truncate" style={{ fontSize: 13 }}>{item.name ?? item.trading_symbol}</div>
        </div>
        <Switch checked={item.active} onChange={onToggle} label={`Trade ${item.symbol}`} />
      </div>
      <div className="row wrap" style={{ marginTop: 14, gap: 8 }}>
        {lots && <Pill><Layers size={13} /> Lot {item.lot_size}</Pill>}
        {item.expiry && <Pill tone={expired ? "bear" : undefined}>{expired ? "Expired" : `Expires ${dateOf(item.expiry)}`}</Pill>}
        <Pill>{equal ? "Equal share" : lots ? `${item.sizing_value} lot${item.sizing_value === 1 ? "" : "s"}` : item.sizing_mode === "qty" ? `${item.sizing_value} units` : item.sizing_mode === "amount" ? `${money(item.sizing_value, false)} each` : `${item.sizing_value}% of cash`}</Pill>
        <Pill><Users size={13} /> {item.account_ids?.length ? `${item.account_ids.length} account${item.account_ids.length === 1 ? "" : "s"}` : "All accounts"}</Pill>
      </div>
      <div className="row" style={{ marginTop: 14 }}>
        <Button size="sm" variant="ghost" style={{ flex: 1 }} onClick={onEdit}>Edit</Button>
        <button className="icon-btn danger" onClick={onRemove} aria-label={`Remove ${item.symbol}`}><Trash2 size={18} /></button>
      </div>
    </motion.div>
  );
}

export function ItemEditSheet({ item, info, accounts, onClose, onSave }: {
  item: BasketItem;
  info: SectionInfo;
  accounts: Account[];
  onClose: () => void;
  onSave: (b: Record<string, unknown>) => Promise<void>;
}) {
  const lots = info.lots;
  const equal = info.sizing_mode === "equal";
  const [mode, setMode] = useState(item.sizing_mode === "lots" ? "qty" : item.sizing_mode);
  const [value, setValue] = useState(String(item.sizing_value));
  const [all, setAll] = useState(!item.account_ids?.length);
  const [picked, setPicked] = useState<number[]>(item.account_ids ?? []);
  const [busy, setBusy] = useState(false);
  const defaults: Record<string, string> = { qty: "1", amount: "10000", percent: "10" };
  const num = Number(value);
  const invalid = !equal && (!(num > 0) || ((lots || mode === "qty") && !Number.isInteger(num)) || (!lots && mode === "percent" && num > 100));
  const save = async () => {
    setBusy(true);
    await onSave({
      ...(equal ? {} : lots ? { sizing_mode: "lots", sizing_value: num } : { sizing_mode: mode, sizing_value: num }),
      ...(all || picked.length === 0 ? { clear_account_ids: true } : { account_ids: picked }),
    });
    setBusy(false);
  };
  return (
    <Sheet open onClose={onClose} title={item.symbol} subtitle={item.name ?? item.trading_symbol}>
      <div className="stack">
        {equal ? (
          <div className="banner bull" style={{ fontSize: 13.5 }}>
            <Coins size={17} />
            <span>Size: an <b>equal share</b> of the money free in each account{lots ? ", in whole lots" : ", as many units as fit"}. Choose “{lots ? "Fixed lots" : "Set per ETF"}” in Money to set a size here.</span>
          </div>
        ) : lots ? (
          <div className="field">
            <span className="label-text">Lots per account</span>
            <Input type="number" inputMode="numeric" min={1} step={1} value={value} onChange={(e) => setValue(e.target.value)}
              suffix={<span className="muted" style={{ paddingRight: 12, fontWeight: 700 }}>lots</span>} />
            <span className="help">1 lot = {item.lot_size} units. Capped at {info.max_lots} lots and by the money free in each account.</span>
          </div>
        ) : (
          <div className="field">
            <span className="label-text">Order size per account</span>
            <Segmented block value={mode} onChange={(m) => { setMode(m); setValue(m === item.sizing_mode ? String(item.sizing_value) : defaults[m]); }}
              options={[{ value: "qty", label: "Units" }, { value: "amount", label: "₹ Amount" }, { value: "percent", label: "% of cash" }]} />
            <Input type="number" inputMode="decimal" min={mode === "qty" ? 1 : 0} step={mode === "qty" ? 1 : "any"} value={value} onChange={(e) => setValue(e.target.value)}
              suffix={<span className="muted" style={{ paddingRight: 12, fontWeight: 700 }}>{mode === "qty" ? "units" : mode === "amount" ? "₹" : "%"}</span>} />
          </div>
        )}
        <div className="field">
          <div className="row between">
            <span className="label-text">Accounts</span>
            <label className="row" style={{ gap: 8, fontSize: 13, fontWeight: 600 }}>All of {info.label}'s accounts <Switch checked={all} onChange={setAll} label="All accounts" /></label>
          </div>
          {!all && (
            <div className="chip-select">
              {accounts.map((a) => {
                const on = picked.includes(a.id);
                return (
                  <button key={a.id} type="button" className={`chip ${on ? "on" : ""}`} onClick={() => setPicked(on ? picked.filter((x) => x !== a.id) : [...picked, a.id])}>
                    {on && <Check size={14} />} {a.name}
                  </button>
                );
              })}
            </div>
          )}
        </div>
        <Button block size="lg" onClick={save} loading={busy} disabled={invalid}>Save</Button>
      </div>
    </Sheet>
  );
}

// ───────────────────────────────────────────── Buy / Sell confirmation

export function CommandSheet({ section, action, symbols, label, onClose, onDone }: {
  section: SectionKey;
  action: "buy" | "sell";
  symbols?: string[];
  label?: string;
  onClose: () => void;
  onDone: (runId: number) => void;
}) {
  const query = `/sections/${section}/preview?action=${action}${(symbols ?? []).map((s) => `&symbols=${encodeURIComponent(s)}`).join("")}`;
  const preview = useApi<CommandPreview>(query);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const data = preview.data;
  const groups = useMemo(() => {
    const map = new Map<number, CommandPreview["orders"]>();
    for (const o of data?.orders ?? []) map.set(o.account_id, [...(map.get(o.account_id) ?? []), o]);
    return [...map.values()];
  }, [data]);
  const placeable = (data?.orders ?? []).filter((o) => !o.skip && o.quantity > 0);
  const accountsTouched = new Set(placeable.map((o) => o.account_id)).size;
  const buying = action === "buy";
  const title = buying ? "Buy now?" : label ? `Sell ${label}?` : `Sell everything in ${SECTION_LABEL[section]}?`;

  const go = async () => {
    if (!data) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api<{ run_id: number }>(`/sections/${section}/${action}`, { body: { fingerprint: data.fingerprint, symbols: symbols?.length ? symbols : null } });
      onDone(r.run_id);
    } catch (e) {
      setError((e as Error).message);
      preview.reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Sheet open onClose={onClose} wide title={title}
      subtitle={buying ? "Orders go out the moment you say yes, in every account at once, at market price." : "Sells only what TradeLab bought in this section — never anyone's own shares or contracts."}>
      {preview.error ? (
        <div className="banner error"><AlertTriangle size={17} /> {preview.error}</div>
      ) : !data ? (
        <div className="stack" style={{ alignItems: "center", padding: 24 }}>
          <Spinner size={26} />
          <span className="muted">{buying ? "Checking money, prices and margins…" : "Checking what each account holds…"}</span>
        </div>
      ) : (
        <div className="stack">
          {data.blocked && <div className="banner warn"><AlertTriangle size={17} /> {data.blocked}</div>}
          {data.expired.length > 0 && <div className="banner error"><AlertTriangle size={17} /> Expired: {data.expired.join(", ")}. Remove them first.</div>}
          {groups.length === 0 ? (
            <Empty icon={buying ? <ShoppingCart size={24} /> : <TrendingDown size={24} />} title={buying ? "Nothing to buy" : "Nothing to sell"}>
              {buying ? "Switch on at least one item and choose at least one autopilot account." : "TradeLab holds nothing in this section right now."}
            </Empty>
          ) : (
            groups.map((list) => {
              const ok = list.filter((o) => !o.skip);
              return (
                <div key={list[0].account_id}>
                  <div className="row between" style={{ marginBottom: 4 }}>
                    <b>{list[0].account_name}</b>
                    {ok.length > 0 && ok.every((o) => o.price) && buying && (
                      <span className="muted num" style={{ fontSize: 13 }}>{ok.length} order{ok.length === 1 ? "" : "s"}</span>
                    )}
                  </div>
                  <div className="list">
                    {list.map((o) => (
                      <div key={`${o.account_id}-${o.trading_symbol}`} className="list-item" style={{ padding: "9px 2px", alignItems: "flex-start" }}>
                        <Pill tone={o.skip ? undefined : o.side === "B" ? "bull" : "bear"}>{o.side === "B" ? "BUY" : "SELL"}</Pill>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <b>{o.symbol}</b>
                          <div className={o.funds_short ? "down" : "muted"} style={{ fontSize: 12.5, wordBreak: "break-word" }}>{o.skip ?? o.note ?? (o.price ? `at ≈${money(o.price)}` : "")}</div>
                        </div>
                        <div className="num" style={{ fontWeight: 800, textAlign: "right", whiteSpace: "nowrap" }}>
                          {o.skip ? <span className="muted" style={{ fontWeight: 600 }}>skipped</span> : o.lots ? `${o.lots} lot${o.lots === 1 ? "" : "s"}` : `× ${o.quantity}`}
                          {!o.skip && o.lots ? <div className="muted" style={{ fontSize: 11.5, fontWeight: 600 }}>{o.quantity} units</div> : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })
          )}
          {buying && placeable.length > 0 && (
            <div className="banner" style={{ fontSize: 13 }}>
              <Info size={17} />
              <span>Lots and quantities are worked out again from the money free at the moment you confirm, so they can differ slightly.</span>
            </div>
          )}
          <AnimatePresence>
            {error && (
              <motion.div className="banner error" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                <AlertTriangle size={17} /> {error}
              </motion.div>
            )}
          </AnimatePresence>
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <Button variant="ghost" onClick={onClose}>Not now</Button>
            <Button variant={buying ? "primary" : "danger"} onClick={go} loading={busy} disabled={!!data.blocked || !placeable.length || data.expired.length > 0}>
              {buying ? <ShoppingCart size={17} /> : <TrendingDown size={17} />}
              {buying ? `Yes, buy in ${accountsTouched} account${accountsTouched === 1 ? "" : "s"}` : `Yes, sell in ${accountsTouched} account${accountsTouched === 1 ? "" : "s"}`}
            </Button>
          </div>
        </div>
      )}
    </Sheet>
  );
}

// ───────────────────────────────────────────── what the section holds

interface Group {
  key: string;
  symbol: string;
  quantity: number;
  lots: number | null;
  value: number | null;
  pnl: number | null;
  rows: SectionPositionRow[];
}

export function PositionsCard({ section, title, sellBlocked, refreshKey, onSell }: {
  section: SectionKey;
  title: string;
  sellBlocked?: string | null;
  refreshKey: number;
  onSell: (symbols: string[] | undefined, label?: string) => void;
}) {
  const positions = useApi<SectionPositions>(`/sections/${section}/positions`, { interval: 30000 });
  const [open, setOpen] = useState<string | null>(null);
  const reload = positions.reload;
  useEffect(() => {
    if (refreshKey) reload();
  }, [refreshKey, reload]);
  const data = positions.data;
  const groups: Group[] = useMemo(() => {
    const map = new Map<string, Group>();
    for (const r of data?.rows ?? []) {
      const g = map.get(r.trading_symbol) ?? { key: r.trading_symbol, symbol: r.symbol, quantity: 0, lots: r.lots === null ? null : 0, value: 0, pnl: 0, rows: [] };
      g.quantity += r.quantity;
      if (g.lots !== null && r.lots !== null) g.lots += r.lots;
      g.value = g.value === null || r.value === null ? null : g.value + r.value;
      g.pnl = g.pnl === null || r.pnl === null ? null : g.pnl + r.pnl;
      g.rows.push(r);
      map.set(r.trading_symbol, g);
    }
    return [...map.values()];
  }, [data]);

  return (
    <motion.div className="card" style={{ marginTop: 18 }} {...stagger(4)}>
      <div className="card-title">
        <span className="row"><span className="icon-bubble"><Wallet size={18} /></span> {title}</span>
        {groups.length > 0 && (
          <Button size="sm" variant="danger" onClick={() => onSell(undefined)} disabled={!!sellBlocked} title={sellBlocked ?? undefined}><TrendingDown size={15} /> Sell all</Button>
        )}
      </div>
      {positions.error ? (
        <div className="banner error"><AlertTriangle size={17} /> {positions.error}</div>
      ) : !data ? (
        <Skeleton h={60} />
      ) : groups.length === 0 ? (
        <div className="muted" style={{ fontSize: 13.5 }}>Nothing bought by TradeLab is open in this section. What you buy here shows up with its live value, and only these can be sold with Sell.</div>
      ) : (
        <>
          <div className="row between" style={{ marginBottom: 8 }}>
            <span className="muted" style={{ fontSize: 13 }}>Value now</span>
            <span className="num" style={{ fontWeight: 800 }}>{money(data.value, false)} <span className={pnlClass(data.pnl)} style={{ fontSize: 13 }}>{signedMoney(data.pnl)}</span></span>
          </div>
          {sellBlocked && <div className="banner warn" style={{ fontSize: 13, marginBottom: 8 }}><Info size={16} /> {sellBlocked}</div>}
          <div className="list">
            {groups.map((g) => (
              <div key={g.key}>
                <div className="list-item" style={{ padding: "10px 2px" }}>
                  <button className="row" style={{ flex: 1, minWidth: 0, background: "none", border: 0, padding: 0, cursor: "pointer", textAlign: "left", color: "inherit" }} onClick={() => setOpen(open === g.key ? null : g.key)}>
                    <Ticker symbol={g.symbol} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <b style={{ display: "block", wordBreak: "break-word" }}>{g.symbol}</b>
                      <div className="muted num" style={{ fontSize: 12.5 }}>
                        {g.lots !== null ? `${g.lots} lot${g.lots === 1 ? "" : "s"} · ` : ""}{g.quantity.toLocaleString("en-IN")} units · {g.rows.length} account{g.rows.length === 1 ? "" : "s"}
                      </div>
                    </div>
                    <div className="num" style={{ textAlign: "right", fontWeight: 800 }}>
                      {money(g.value, false)}
                      <div className={pnlClass(g.pnl)} style={{ fontSize: 12.5 }}>{signedMoney(g.pnl)}</div>
                    </div>
                    <ChevronDown size={17} className="muted" style={{ transform: open === g.key ? "rotate(180deg)" : undefined, transition: "transform .2s", flexShrink: 0 }} />
                  </button>
                  <Button size="sm" variant="ghost" onClick={() => onSell([g.key], g.symbol)} disabled={!!sellBlocked}>Sell</Button>
                </div>
                <AnimatePresence initial={false}>
                  {open === g.key && (
                    <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} style={{ overflow: "hidden" }}>
                      <div className="list" style={{ padding: "0 4px 8px 54px" }}>
                        {g.rows.map((r) => (
                          <div key={r.account_id} className="list-item" style={{ padding: "8px 0", fontSize: 13.5 }}>
                            <span style={{ flex: 1, minWidth: 0 }} className="truncate">{r.account_name}</span>
                            <span className="muted num">{r.lots !== null ? `${r.lots} lot${r.lots === 1 ? "" : "s"}` : `${r.quantity} units`}{r.avg_price ? ` @ ${money(r.avg_price)}` : ""}</span>
                            <b className={`num ${pnlClass(r.pnl)}`}>{signedMoney(r.pnl)}</b>
                          </div>
                        ))}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
            ))}
          </div>
          {data.errors.length > 0 && (
            <div className="banner warn" style={{ marginTop: 10, fontSize: 13 }}>
              <AlertTriangle size={16} /> Couldn't read {data.errors.map((e) => e.account_name).join(", ")} — <Link to="/accounts">check the login</Link>.
            </div>
          )}
        </>
      )}
    </motion.div>
  );
}

// ───────────────────────────────────────────── search pickers

export function Picker<T>({ open, onClose, placeholder, fetchResults, keyOf, renderRow, inList, onAdd, banner, emptyHint, header, refreshKey }: {
  open: boolean;
  onClose: () => void;
  placeholder: string;
  fetchResults: (q: string) => Promise<T[]>;
  keyOf: (t: T) => string;
  renderRow: (t: T) => ReactNode;
  inList: Set<string>;
  onAdd: (t: T) => Promise<void>;
  banner?: ReactNode;
  emptyHint: (q: string) => ReactNode;
  header?: ReactNode;
  refreshKey?: string; // search again when this changes (e.g. a filter in the header)
}) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 180);
  const [results, setResults] = useState<T[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [focus, setFocus] = useState(0);
  const [adding, setAdding] = useState<string | null>(null);
  const fetchRef = useRef(fetchResults);
  fetchRef.current = fetchResults;

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    fetchRef.current(dq)
      .then((r) => !cancelled && (setResults(r), setFocus(0)))
      .catch(() => !cancelled && setResults([]))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [dq, open, refreshKey]);

  const list = results ?? [];
  const pick = async (t: T) => {
    if (inList.has(keyOf(t))) return;
    setAdding(keyOf(t));
    await onAdd(t);
    setAdding(null);
  };
  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setFocus((f) => Math.min(f + 1, list.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setFocus((f) => Math.max(f - 1, 0));
    } else if (e.key === "Enter" && list[focus]) {
      e.preventDefault();
      pick(list[focus]);
    }
  };

  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div className="overlay search-overlay" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
          <motion.div className="search-panel" initial={{ y: 30, opacity: 0 }} animate={{ y: 0, opacity: 1 }} exit={{ y: 20, opacity: 0 }} transition={{ type: "spring", stiffness: 380, damping: 34 }}>
            <div className="search-top">
              <button className="icon-btn" onClick={onClose} aria-label="Close search"><ArrowLeft size={21} /></button>
              <div style={{ flex: 1 }}>
                <Input
                  icon={loading ? <span style={{ position: "absolute", left: 14, display: "flex" }}><Spinner size={16} /></span> : <Search size={18} />}
                  className="search-input" placeholder={placeholder} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey}
                  autoCapitalize="characters" autoCorrect="off" spellCheck={false} autoFocus
                />
              </div>
            </div>
            {header && <div style={{ padding: "10px 12px 0" }}>{header}</div>}
            <div className="search-results">
              {banner}
              {!loading && results !== null && list.length === 0 && <div className="empty" style={{ padding: "36px 10px" }}><Search size={24} /><p>{emptyHint(q)}</p></div>}
              {list.map((t, i) => {
                const key = keyOf(t);
                const added = inList.has(key);
                return (
                  <button key={key} className={`result ${i === focus ? "focus" : ""}`} onMouseEnter={() => setFocus(i)} onClick={() => pick(t)}>
                    {renderRow(t)}
                    {added ? <Pill tone="bull"><Check size={13} /> Added</Pill> : adding === key ? <Spinner /> : (
                      <span className="icon-btn" style={{ background: "var(--surface-2)", color: "var(--text)", borderColor: "var(--border)" }}><Plus size={19} /></span>
                    )}
                  </button>
                );
              })}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

/** The day in IST as YYYY-MM-DD. */
export const todayIst = () => new Date().toLocaleDateString("en-CA", { timeZone: "Asia/Kolkata" });
