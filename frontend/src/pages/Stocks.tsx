import { AnimatePresence, motion } from "framer-motion";
import { ArrowLeft, Check, CircleSlash, Coins, Eye, Info, Plus, Scale, Search, Trash2, TrendingDown, TrendingUp, Users } from "lucide-react";
import { useEffect, useMemo, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { PageHeader } from "../components/AppShell";
import { LetsTrade } from "../components/LetsTrade";
import { Sheet } from "../components/Modal";
import { Button, Empty, Input, Pill, Segmented, Skeleton, Spinner, Switch, stagger } from "../components/ui";
import { api } from "../lib/api";
import { hm, money, sizingLabel } from "../lib/format";
import { useApi, useDebounced } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, AutomationSettings, AutomationStatus, BasketItem, Instrument, LiveSnapshot, PreviewRow, SizingMode, StockSearch } from "../lib/types";

export function StocksPage() {
  const toast = useToast();
  const basket = useApi<BasketItem[]>("/basket");
  const accounts = useApi<Account[]>("/accounts");
  const settings = useApi<AutomationSettings>("/automation/settings");
  const status = useApi<AutomationStatus>("/automation/status", { interval: 15000 });
  const live = useApi<LiveSnapshot[]>("/accounts/live");
  const [searchOpen, setSearchOpen] = useState(false);
  const [editing, setEditing] = useState<BasketItem | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const equal = settings.data?.sizing_mode !== "custom";

  const saveSettings = async (patch: Partial<AutomationSettings>) => {
    if (!settings.data) return;
    const before = settings.data;
    settings.setData({ ...before, ...patch });
    try {
      settings.setData(await api<AutomationSettings>("/automation/settings", { method: "PUT", body: patch }));
    } catch (e) {
      settings.setData(before);
      toast((e as Error).message, "error");
    }
    status.reload();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !(e.target as HTMLElement).closest("input,textarea"))) {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const add = async (inst: Instrument) => {
    try {
      const item = await api<BasketItem>("/basket", {
        body: { symbol: inst.symbol, trading_symbol: inst.trading_symbol, token: inst.token, exchange_segment: inst.exchange_segment, name: inst.name, sizing_mode: "qty", sizing_value: 1 },
      });
      basket.setData([...(basket.data ?? []), item]);
      status.reload();
      toast(equal ? `${inst.symbol} added — it gets an equal share of each account's money` : `${inst.symbol} added — tap Edit order to set its size`);
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  const remove = async (item: BasketItem) => {
    basket.setData(basket.data?.filter((b) => b.id !== item.id) ?? null);
    try {
      await api(`/basket/${item.id}`, { method: "DELETE" });
      toast(`${item.symbol} removed`);
    } catch (e) {
      toast((e as Error).message, "error");
      basket.reload();
    }
    status.reload();
  };

  const patch = async (item: BasketItem, body: Record<string, unknown>) => {
    try {
      const updated = await api<BasketItem>(`/basket/${item.id}`, { method: "PATCH", body });
      basket.setData(basket.data?.map((b) => (b.id === item.id ? updated : b)) ?? null);
      status.reload();
      return updated;
    } catch (e) {
      toast((e as Error).message, "error");
      return null;
    }
  };

  const items = basket.data ?? [];
  const active = items.filter((b) => b.active);
  const inBasket = useMemo(() => new Set(items.map((b) => b.trading_symbol)), [items]);
  const entry = settings.data ? hm(settings.data.entry_time) : "09:15";
  const exit = settings.data ? hm(settings.data.exit_time) : "14:30";

  return (
    <>
      <PageHeader title="Stocks" subtitle={`Your list stays here · trades only after Let's Trade · buys at ${entry}, sells at ${exit}`} />

      <motion.button className="search-trigger" onClick={() => setSearchOpen(true)} {...stagger(0)}>
        <Search size={20} />
        Search all NSE stocks…
        <span className="row" style={{ marginLeft: "auto", gap: 10 }}>
          <span className="kbd hide-sm">Ctrl K</span>
          <span className="go"><Plus size={20} /></span>
        </span>
      </motion.button>

      <SizingCard
        settings={settings.data}
        accounts={accounts.data}
        live={live.data}
        basket={items}
        onChange={saveSettings}
        onPreview={() => setPreviewOpen(true)}
      />

      <div className="row between" style={{ margin: "24px 2px 12px" }}>
        <b style={{ fontSize: 17 }}>Your stocks</b>
        {basket.data && <Pill tone={active.length ? "bull" : undefined}>{active.length} switched on</Pill>}
      </div>

      {!basket.data ? (
        <div className="stack">{[0, 1, 2].map((i) => <Skeleton key={i} h={86} r={20} />)}</div>
      ) : items.length === 0 ? (
        <div className="card">
          <Empty icon={<Search size={24} />} title="Pick your stocks" action={<Button onClick={() => setSearchOpen(true)}><Search size={18} /> Browse stocks</Button>}>
            Your list stays here from day to day. Nothing is bought until you press <b>Let's Trade</b> and confirm.
          </Empty>
        </div>
      ) : (
        <div className="grid-auto">
          <AnimatePresence>
            {items.map((item, i) => (
              <motion.div key={item.id} className="card" layout {...stagger(i)} exit={{ opacity: 0, scale: 0.95 }} style={{ opacity: item.active ? 1 : 0.62 }}>
                <div className="row" style={{ alignItems: "flex-start" }}>
                  <Ticker symbol={item.symbol} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <b style={{ fontSize: 16.5 }}>{item.symbol}</b>
                    <div className="muted truncate" style={{ fontSize: 13 }}>{item.name ?? item.trading_symbol}</div>
                  </div>
                  <Switch checked={item.active} onChange={(v) => patch(item, { active: v })} label={`Trade ${item.symbol}`} />
                </div>
                <div className="row wrap" style={{ marginTop: 14, gap: 8 }}>
                  <Pill tone={item.side === "B" ? "bull" : "bear"}>
                    {item.side === "B" ? <TrendingUp size={13} /> : <TrendingDown size={13} />}
                    {item.side === "B" ? "Buy" : "Short"}
                  </Pill>
                  <Pill>{equal ? <><Scale size={13} /> Equal share</> : sizingLabel(item.sizing_mode, item.sizing_value)}</Pill>
                  <Pill><Users size={13} /> {item.account_ids?.length ? `${item.account_ids.length} account${item.account_ids.length === 1 ? "" : "s"}` : "All accounts"}</Pill>
                </div>
                <div className="row" style={{ marginTop: 14 }}>
                  <Button size="sm" variant="ghost" style={{ flex: 1 }} onClick={() => setEditing(item)}>Edit order</Button>
                  <button className="icon-btn danger" onClick={() => remove(item)} aria-label={`Remove ${item.symbol}`}><Trash2 size={18} /></button>
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      )}

      {status.data && settings.data && (
        <motion.div className="card" style={{ marginTop: 18 }} {...stagger(3)}>
          <LetsTrade state={status.data.lets_trade} settings={settings.data} accounts={accounts.data} onChanged={() => { status.reload(); accounts.reload(); }} />
        </motion.div>
      )}

      <SearchOverlay open={searchOpen} onClose={() => setSearchOpen(false)} inBasket={inBasket} onAdd={add} />
      {editing && (
        <EditSheet
          item={items.find((b) => b.id === editing.id) ?? editing}
          accounts={accounts.data ?? []}
          equal={equal}
          onClose={() => setEditing(null)}
          onSave={async (body) => {
            const ok = await patch(editing, body);
            if (ok) {
              toast(`${editing.symbol} updated`);
              setEditing(null);
            }
          }}
        />
      )}
      {previewOpen && <PreviewSheet onClose={() => setPreviewOpen(false)} />}
    </>
  );
}

function Ticker({ symbol }: { symbol: string }) {
  const label = symbol.replace(/[^A-Z0-9&]/gi, "").slice(0, 4);
  return <div className="ticker-badge" style={{ fontSize: label.length >= 4 ? 11 : 13 }}>{label}</div>;
}

function SearchOverlay({ open, onClose, inBasket, onAdd }: { open: boolean; onClose: () => void; inBasket: Set<string>; onAdd: (i: Instrument) => Promise<void> }) {
  const [q, setQ] = useState("");
  const dq = useDebounced(q, 160);
  const [data, setData] = useState<StockSearch | null>(null);
  const [loading, setLoading] = useState(false);
  const [focus, setFocus] = useState(0);
  const [adding, setAdding] = useState<string | null>(null);

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
    api<StockSearch>(`/stocks/search?q=${encodeURIComponent(dq)}&limit=50`)
      .then((d) => !cancelled && (setData(d), setFocus(0)))
      .catch(() => !cancelled && setData(null))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [dq, open]);

  const results = data?.results ?? [];
  const pick = async (inst: Instrument) => {
    if (inBasket.has(inst.trading_symbol)) return;
    setAdding(inst.trading_symbol);
    await onAdd(inst);
    setAdding(null);
  };

  const onKey = (e: ReactKeyboardEvent) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setFocus((f) => Math.min(f + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setFocus((f) => Math.max(f - 1, 0));
    } else if (e.key === "Enter" && results[focus]) {
      e.preventDefault();
      pick(results[focus]);
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
                  className="search-input"
                  placeholder="Search by symbol or company"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  onKeyDown={onKey}
                  autoCapitalize="characters"
                  autoCorrect="off"
                  spellCheck={false}
                  autoFocus
                />
              </div>
            </div>
            <div className="search-results">
              {data?.source === "builtin" && (
                <div className="banner warn" style={{ margin: "8px 4px 10px", fontSize: 13 }}>
                  <Info size={17} />
                  <span>Showing {data.count} popular stocks. {data.message ?? "Connect a Kotak Neo account to search every NSE stock."}</span>
                </div>
              )}
              {!q && data?.source === "kotak" && (
                <div className="empty" style={{ padding: "40px 10px" }}>
                  <Search size={26} />
                  <p>Type a symbol or company name — {data.count.toLocaleString("en-IN")} NSE stocks available.</p>
                </div>
              )}
              {q && !loading && results.length === 0 && (
                <div className="empty"><CircleSlash size={24} /><p>No NSE stock matches “{q}”.</p></div>
              )}
              {results.map((r, i) => {
                const added = inBasket.has(r.trading_symbol);
                return (
                  <button key={r.trading_symbol} className={`result ${i === focus ? "focus" : ""}`} onMouseEnter={() => setFocus(i)} onClick={() => pick(r)}>
                    <Ticker symbol={r.symbol} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontWeight: 800 }}>{r.symbol}</div>
                      <div className="muted truncate" style={{ fontSize: 13 }}>{r.name}</div>
                    </div>
                    <span className="muted hide-sm" style={{ fontSize: 12 }}>NSE · {r.series}</span>
                    {added ? (
                      <Pill tone="bull"><Check size={13} /> Added</Pill>
                    ) : adding === r.trading_symbol ? (
                      <Spinner />
                    ) : (
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

function EditSheet({ item, accounts, equal, onClose, onSave }: { item: BasketItem; accounts: Account[]; equal: boolean; onClose: () => void; onSave: (b: Record<string, unknown>) => Promise<void> }) {
  const [side, setSide] = useState(item.side);
  const [mode, setMode] = useState<SizingMode>(item.sizing_mode);
  const [value, setValue] = useState(String(item.sizing_value));
  const [all, setAll] = useState(!item.account_ids?.length);
  const [picked, setPicked] = useState<number[]>(item.account_ids ?? []);
  const [busy, setBusy] = useState(false);

  const defaults: Record<SizingMode, string> = { qty: "1", amount: "10000", percent: "10" };
  const changeMode = (m: SizingMode) => {
    setMode(m);
    setValue(m === item.sizing_mode ? String(item.sizing_value) : defaults[m]);
  };
  const save = async () => {
    setBusy(true);
    await onSave({
      side,
      ...(equal ? {} : { sizing_mode: mode, sizing_value: Number(value) }),
      ...(all || picked.length === 0 ? { clear_account_ids: true } : { account_ids: picked }),
    });
    setBusy(false);
  };
  const num = Number(value);
  const invalid = !equal && (!(num > 0) || (mode === "qty" && !Number.isInteger(num)) || (mode === "percent" && num > 500));

  return (
    <Sheet open onClose={onClose} title={item.symbol} subtitle={item.name ?? item.trading_symbol}>
      <div className="stack">
        <div className="field">
          <span className="label-text">At entry time</span>
          <Segmented block value={side} onChange={setSide} options={[{ value: "B", label: <span className="row" style={{ gap: 6, justifyContent: "center" }}><TrendingUp size={15} /> Buy</span> }, { value: "S", label: <span className="row" style={{ gap: 6, justifyContent: "center" }}><TrendingDown size={15} /> Short sell</span> }]} />
          <span className="help">{side === "B" ? "Buys at market, sold at square-off time." : "Sells first at market, bought back at square-off time."}</span>
        </div>
        {equal ? (
          <div className="banner bull" style={{ fontSize: 13.5 }}>
            <Scale size={17} />
            <span>Size: an <b>equal share</b> of each account's available money, as many shares as fit. Switch to “Set per stock” on the Stocks page to choose a size for each stock.</span>
          </div>
        ) : (
        <div className="field">
          <span className="label-text">Order size per account</span>
          <Segmented block value={mode} onChange={changeMode} options={[{ value: "qty", label: "Shares" }, { value: "amount", label: "₹ Amount" }, { value: "percent", label: "% of funds" }]} />
          <Input type="number" inputMode="decimal" min={mode === "qty" ? 1 : 0} step={mode === "qty" ? 1 : "any"} value={value} onChange={(e) => setValue(e.target.value)}
            suffix={<span className="muted" style={{ paddingRight: 12, fontWeight: 700 }}>{mode === "qty" ? "shares" : mode === "amount" ? "₹" : "%"}</span>} />
          <span className="help">
            {mode === "qty" && "Exactly this many shares in every account."}
            {mode === "amount" && "Shares = amount ÷ price just before entry (rounded down)."}
            {mode === "percent" && "Uses this % of each account's available funds. Above 100% uses intraday leverage."}
          </span>
        </div>
        )}
        <div className="field">
          <div className="row between">
            <span className="label-text">Accounts</span>
            <label className="row" style={{ gap: 8, fontSize: 13, fontWeight: 600 }}>All autopilot accounts <Switch checked={all} onChange={setAll} label="All accounts" /></label>
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

function SizingCard({
  settings,
  accounts,
  live,
  basket,
  onChange,
  onPreview,
}: {
  settings: AutomationSettings | null;
  accounts: Account[] | null;
  live: LiveSnapshot[] | null;
  basket: BasketItem[] | null;
  onChange: (p: Partial<AutomationSettings>) => void;
  onPreview: () => void;
}) {
  const [reserve, setReserve] = useState("");
  useEffect(() => {
    if (settings) setReserve(String(settings.reserve_pct));
  }, [settings?.reserve_pct]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!settings) return <div style={{ marginTop: 16 }}><Skeleton h={120} r={20} /></div>;
  const equal = settings.sizing_mode === "equal";
  const auto = (accounts ?? []).filter((a) => a.automation_enabled);
  const active = (basket ?? []).filter((b) => b.active);
  const commitReserve = () => {
    const v = Math.min(50, Math.max(0, Number(reserve) || 0));
    setReserve(String(v));
    if (v !== settings.reserve_pct) onChange({ reserve_pct: v });
  };

  return (
    <motion.div className="card" style={{ marginTop: 16 }} {...stagger(1)}>
      <div className="card-title">
        <span className="row"><span className="icon-bubble"><Coins size={18} /></span> Money per stock</span>
        <Button size="sm" variant="ghost" onClick={onPreview} disabled={!active.length || !auto.length}><Eye size={15} /> <span className="hide-sm">Preview orders</span><span className="show-sm">Preview</span></Button>
      </div>
      <Segmented
        block
        tone="bull"
        value={settings.sizing_mode}
        onChange={(v) => onChange({ sizing_mode: v })}
        options={[
          { value: "equal", label: "Split funds equally" },
          { value: "custom", label: "Set per stock" },
        ]}
      />
      {equal ? (
        <div className="stack" style={{ gap: 12, marginTop: 14 }}>
          <div className="muted" style={{ fontSize: 13.5 }}>
            Just before {hm(settings.entry_time)}, each account's available money is divided equally between its stocks, and TradeLab buys the most shares that fit in each part.
          </div>
          {auto.length > 0 && active.length > 0 && (
            <div className="list">
              {auto.map((a) => {
                const l = live?.find((x) => x.id === a.id);
                const n = active.filter((b) => !b.account_ids?.length || b.account_ids.includes(a.id)).length;
                const usable = l?.ok ? (l.available ?? 0) * (1 - settings.reserve_pct / 100) : null;
                return (
                  <div key={a.id} className="list-item" style={{ padding: "9px 2px" }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <b className="truncate">{a.name}</b>
                      <div className="muted num" style={{ fontSize: 12.5 }}>
                        {l ? (l.ok ? `${money(usable, false)} ÷ ${n} stock${n === 1 ? "" : "s"}` : "Couldn't read funds") : "Checking funds…"}
                      </div>
                    </div>
                    <div className="num" style={{ fontWeight: 800, textAlign: "right" }}>
                      {usable !== null && n > 0 ? money(usable / n, false) : "—"}
                      <div className="muted" style={{ fontSize: 11.5, fontWeight: 600 }}>per stock</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <div className="row between" style={{ gap: 12 }}>
            <div style={{ minWidth: 0 }}>
              <b style={{ fontSize: 14 }}>Keep aside</b>
              <div className="muted" style={{ fontSize: 12.5 }}>Optional cushion left untraded in every account</div>
            </div>
            <div style={{ width: 110, flexShrink: 0 }}>
              <Input
                className="sm"
                type="number"
                inputMode="decimal"
                min={0}
                max={50}
                value={reserve}
                onChange={(e) => setReserve(e.target.value)}
                onBlur={commitReserve}
                onKeyDown={(e) => e.key === "Enter" && (e.currentTarget as HTMLInputElement).blur()}
                suffix={<span className="muted" style={{ paddingRight: 10, fontWeight: 700 }}>%</span>}
                style={{ paddingRight: 30 }}
              />
            </div>
          </div>
        </div>
      ) : (
        <div className="muted" style={{ fontSize: 13.5, marginTop: 14 }}>
          Each stock uses its own size — a number of shares, a ₹ amount or a % of funds. Tap <b>Edit order</b> on a stock to set it.
        </div>
      )}
    </motion.div>
  );
}

function PreviewSheet({ onClose }: { onClose: () => void }) {
  const rows = useApi<PreviewRow[]>("/automation/preview");
  const groups = useMemo(() => {
    const map = new Map<number, PreviewRow[]>();
    for (const r of rows.data ?? []) map.set(r.account_id, [...(map.get(r.account_id) ?? []), r]);
    return [...map.entries()];
  }, [rows.data]);
  return (
    <Sheet open onClose={onClose} title="Order preview" subtitle="What Let's Trade would place for your current list at today's prices. Nothing is sent to Kotak." wide>
      {rows.error ? (
        <div className="banner error">{rows.error}</div>
      ) : !rows.data ? (
        <div className="stack" style={{ alignItems: "center", padding: 24 }}>
          <Spinner size={26} />
          <span className="muted">Checking funds and prices…</span>
        </div>
      ) : groups.length === 0 ? (
        <Empty icon={<Eye size={24} />} title="Nothing to place">Switch on at least one stock and turn on autopilot for at least one account.</Empty>
      ) : (
        <div className="stack">
          {groups.map(([accountId, list]) => {
            const placed = list.filter((r) => !r.skip);
            const total = placed.reduce((s, r) => s + r.quantity * (r.price ?? 0), 0);
            const priced = placed.every((r) => r.price);
            return (
              <div key={accountId}>
                <div className="row between" style={{ marginBottom: 4 }}>
                  <b>{list[0].account_name}</b>
                  {placed.length > 0 && priced && <span className="muted num" style={{ fontSize: 13 }}>≈ {money(total, false)} total</span>}
                </div>
                <div className="list">
                  {list.map((r) => (
                    <div key={`${r.account_id}-${r.symbol}`} className="list-item" style={{ padding: "9px 2px", alignItems: "flex-start" }}>
                      <Pill tone={r.skip ? undefined : r.side === "B" ? "bull" : "bear"}>{r.side === "B" ? "BUY" : "SELL"}</Pill>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <b>{r.symbol}</b>
                        <div className="muted" style={{ fontSize: 12.5, wordBreak: "break-word" }}>{r.skip ?? r.note ?? (r.price ? `at ≈${money(r.price)}` : "")}</div>
                      </div>
                      <div className="num" style={{ fontWeight: 800, textAlign: "right", whiteSpace: "nowrap" }}>
                        {r.skip ? <span className="muted" style={{ fontWeight: 600 }}>skipped</span> : `× ${r.quantity}`}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
          <div className="banner" style={{ fontSize: 13 }}>
            <Info size={17} />
            <span>Prices and funds change. Quantities are worked out again a minute before entry, then all orders go out together at market price.</span>
          </div>
        </div>
      )}
    </Sheet>
  );
}
