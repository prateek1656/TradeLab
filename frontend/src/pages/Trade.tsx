import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CalendarClock, Gem, Info, Layers, PiggyBank, Search, ShoppingCart, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "../components/AppShell";
import { LetsTrade } from "../components/LetsTrade";
import {
  CommandSheet, ItemCard, ItemEditSheet, MoneyCard, Picker, PositionsCard, SectionAccountsCard, Ticker, todayIst, useRunWatcher, useSectionInfo,
} from "../components/SectionKit";
import { Button, Empty, Field, Input, Pill, Segmented, Skeleton, stagger } from "../components/ui";
import { api } from "../lib/api";
import { hm } from "../lib/format";
import { useApi } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, AutomationStatus, BasketItem, Contract, ContractSearch, Etf, EtfSearch, LetsTradeState, LiveSnapshot, SectionKey } from "../lib/types";
import { IntradayTab } from "./Stocks";

const TABS: { value: SectionKey; label: string; icon: typeof Zap }[] = [
  { value: "intraday", label: "Intraday", icon: Zap },
  { value: "holding", label: "Holding", icon: PiggyBank },
  { value: "fno", label: "F&O", icon: Layers },
  { value: "commodity", label: "Commodity", icon: Gem },
];

const SUBTITLE: Record<SectionKey, string> = {
  intraday: "Stocks bought in the morning and squared off the same day",
  holding: "Index ETFs bought for delivery and kept",
  fno: "Futures & options — buy and sell on your command",
  commodity: "MCX gold, silver, crude oil and more — on your command",
};

export function TradePage() {
  const { section } = useParams();
  const navigate = useNavigate();
  const key = TABS.find((t) => t.value === section)?.value;
  if (!key) return <Navigate to="/trade/intraday" replace />;
  return (
    <>
      <PageHeader title="Trade" subtitle={SUBTITLE[key]} />
      <div className="section-tabs">
        <Segmented
          block
          value={key}
          onChange={(v) => navigate(`/trade/${v}`)}
          options={TABS.map((t) => ({ value: t.value, label: <span className="tab-label"><t.icon size={15} />{t.label}</span> }))}
        />
      </div>
      {key === "intraday" ? <IntradayTab /> : key === "holding" ? <HoldingTab /> : <DerivativesTab key={key} section={key} />}
    </>
  );
}

/** Shared list handling for the Holding, F&O and Commodity tabs. */
function useSectionList(section: SectionKey, onChange: () => void) {
  const toast = useToast();
  const basket = useApi<BasketItem[]>(`/basket?section=${section}`);
  const items = basket.data ?? [];
  const add = async (body: Record<string, unknown>, label: string) => {
    try {
      const item = await api<BasketItem>("/basket", { body: { section, ...body } });
      basket.setData([...(basket.data ?? []), item]);
      toast(`${label} added`);
      onChange();
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
    onChange();
  };
  const patch = async (item: BasketItem, body: Record<string, unknown>) => {
    try {
      const updated = await api<BasketItem>(`/basket/${item.id}`, { method: "PATCH", body });
      basket.setData(basket.data?.map((b) => (b.id === item.id ? updated : b)) ?? null);
      onChange();
      return updated;
    } catch (e) {
      toast((e as Error).message, "error");
      return null;
    }
  };
  return { basket, items, add, remove, patch };
}

// ───────────────────────────────────────────── Holding

function HoldingTab() {
  const toast = useToast();
  const { info, save } = useSectionInfo("holding");
  const accounts = useApi<Account[]>("/accounts");
  const status = useApi<AutomationStatus>("/automation/status", { interval: 15000 });
  const live = useApi<LiveSnapshot[]>("/accounts/live");
  const list = useSectionList("holding", () => status.reload());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [editing, setEditing] = useState<BasketItem | null>(null);
  const [selling, setSelling] = useState<{ symbols?: string[]; label?: string } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [entry, setEntry] = useState<string | null>(null);
  const [catalogue, setCatalogue] = useState<{ total: number; builtin: boolean } | null>(null);
  const watch = useRunWatcher(() => {
    setRefresh((n) => n + 1);
    status.reload();
    live.reload();
  });
  const state = status.data?.sections.holding;
  const inList = useMemo(() => new Set(list.items.map((b) => b.trading_symbol)), [list.items]);
  const saveSection = async (patch: Record<string, unknown>) => {
    await save(patch);
    status.reload();
  };
  const entryValue = entry ?? (info?.entry_time ? hm(info.entry_time) : "");
  const today = todayIst();

  return (
    <>
      <p className="section-intro">
        Index ETFs only, bought for <b>delivery</b> in every account and kept until you tap <b>Sell</b>. They buy at {info?.entry_time ? hm(info.entry_time) : "09:20"} on days you confirm with Let's Trade — straight away if the market is open.
      </p>
      <motion.button className="search-trigger" onClick={() => setPickerOpen(true)} {...stagger(0)}>
        <Search size={20} />
        Browse all index ETFs…
        <span className="go" style={{ marginLeft: "auto" }}><PiggyBank size={19} /></span>
      </motion.button>

      <MoneyCard info={info} accounts={accounts.data} live={live.data} items={list.basket.data} onSave={saveSection} />

      <div className="row between" style={{ margin: "24px 2px 12px" }}>
        <b style={{ fontSize: 17 }}>Your ETFs</b>
        {list.basket.data && <Pill tone={list.items.some((b) => b.active) ? "bull" : undefined}>{list.items.filter((b) => b.active).length} switched on</Pill>}
      </div>
      {!list.basket.data ? (
        <div className="stack">{[0, 1].map((i) => <Skeleton key={i} h={86} r={20} />)}</div>
      ) : list.items.length === 0 ? (
        <div className="card">
          <Empty icon={<PiggyBank size={24} />} title="Pick your index ETFs" action={<Button onClick={() => setPickerOpen(true)}><Search size={18} /> Browse ETFs</Button>}>
            Choose from every index ETF on NSE — Nifty, Bank, IT, Midcap, Next 50 and more. The money is split equally between the ones you pick.
          </Empty>
        </div>
      ) : (
        <div className="grid-auto">
          <AnimatePresence>
            {list.items.map((item, i) => (
              <ItemCard key={item.id} item={item} info={info} today={today} index={i} onToggle={(v) => list.patch(item, { active: v })} onEdit={() => setEditing(item)} onRemove={() => list.remove(item)} />
            ))}
          </AnimatePresence>
        </div>
      )}

      {state && info && state.trade_date && (
        <motion.div className="card" style={{ marginTop: 18 }} {...stagger(3)}>
          <LetsTrade state={state as LetsTradeState} settings={info} accounts={accounts.data} onChanged={() => { status.reload(); accounts.reload(); }} />
        </motion.div>
      )}

      <PositionsCard section="holding" title="Your holdings (bought by TradeLab)" sellBlocked={state?.sell_blocked} refreshKey={refresh} onSell={(symbols, label) => setSelling({ symbols, label })} />

      <div className="grid grid-2-md" style={{ marginTop: 18 }}>
        <div className="card">
          <div className="card-title"><span className="row"><span className="icon-bubble"><CalendarClock size={18} /></span> Buy time (IST)</span></div>
          {!info ? <Skeleton h={60} /> : (
            <div className="stack" style={{ gap: 10 }}>
              <Field label="Holding buys at" help={`Between ${info.opens} and ${info.buy_until} on days you confirm`}>
                <Input type="time" value={entryValue} min={info.opens} max={info.buy_until} onChange={(e) => setEntry(e.target.value)} />
              </Field>
              {entry && info.entry_time && entry !== hm(info.entry_time) && (
                <Button onClick={async () => { const r = await save({ entry_time: entry }); if (r) { setEntry(null); toast("Buy time saved"); status.reload(); } }}>Save time</Button>
              )}
            </div>
          )}
        </div>
        <SectionAccountsCard info={info} accounts={accounts.data} onSave={saveSection} />
      </div>

      <Picker<Etf>
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        placeholder="Search ETFs: Nifty, Bank, IT, Midcap…"
        fetchResults={async (q) => {
          const r = await api<EtfSearch>(`/stocks/etfs?q=${encodeURIComponent(q)}&limit=${q ? 80 : 400}`);
          setCatalogue({ total: r.total, builtin: r.source !== "kotak" });
          return r.results;
        }}
        keyOf={(e) => e.trading_symbol}
        inList={inList}
        onAdd={(e) => list.add({ symbol: e.symbol, trading_symbol: e.trading_symbol, token: e.token, exchange_segment: "nse_cm", name: e.name, sizing_mode: "qty", sizing_value: 1 }, e.symbol)}
        banner={catalogue && (
          <div className="banner" style={{ margin: "8px 4px 10px", fontSize: 13 }}>
            <Info size={17} />
            <span>{catalogue.total} index ETFs on NSE (no gold, silver, liquid or bond ETFs). Most traded first.{catalogue.builtin ? " Live prices come once a Kotak Neo account is connected." : ""}</span>
          </div>
        )}
        emptyHint={(q) => `No index ETF matches “${q}”.`}
        renderRow={(e) => (
          <>
            <Ticker symbol={e.symbol} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row" style={{ gap: 6 }}><b>{e.symbol}</b>{e.core && <Pill tone="bull">Most traded</Pill>}</div>
              <div className="muted truncate" style={{ fontSize: 13 }}>{e.name}</div>
            </div>
            <span className="muted hide-sm" style={{ fontSize: 12 }}>{e.group}</span>
          </>
        )}
      />
      {editing && info && (
        <ItemEditSheet item={list.items.find((b) => b.id === editing.id) ?? editing} info={info} accounts={accounts.data ?? []} onClose={() => setEditing(null)}
          onSave={async (body) => { if (await list.patch(editing, body)) { toast(`${editing.symbol} updated`); setEditing(null); } }} />
      )}
      {selling && (
        <CommandSheet section="holding" action="sell" symbols={selling.symbols} label={selling.label} onClose={() => setSelling(null)}
          onDone={(runId) => { setSelling(null); toast("Selling now…"); watch(runId, "Holding sell"); }} />
      )}
    </>
  );
}

// ───────────────────────────────────────────── F&O and Commodity

const DERIV = {
  fno: {
    intro: "NSE and BSE futures and options. Tap Buy and every account buys at once: the money free in each is split equally between your contracts and turned into lots. Tap Sell to sell what TradeLab bought.",
    placeholder: "NIFTY 25000 CE, BANKNIFTY FUT, SENSEX…",
    hint: "Type an index or stock, a strike, CE/PE or FUT — e.g. NIFTY 25000 CE, BANKNIFTY FUT, RELIANCE FUT.",
    browse: "Search futures & options…",
  },
  commodity: {
    intro: "MCX gold, silver, crude oil, natural gas and more. Tap Buy and every account buys at once: the money free in each is split equally between your contracts and turned into lots. Tap Sell to sell what TradeLab bought.",
    placeholder: "GOLDM, SILVERM, CRUDEOIL, NATURALGAS…",
    hint: "Type a commodity — e.g. GOLD, GOLDM, SILVERM, CRUDEOIL, NATURALGAS, COPPER.",
    browse: "Search gold, silver, crude oil…",
  },
} as const;

function DerivativesTab({ section }: { section: "fno" | "commodity" }) {
  const toast = useToast();
  const text = DERIV[section];
  const { info, save } = useSectionInfo(section);
  const accounts = useApi<Account[]>("/accounts");
  const status = useApi<AutomationStatus>("/automation/status", { interval: 15000 });
  const live = useApi<LiveSnapshot[]>("/accounts/live");
  const list = useSectionList(section, () => status.reload());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [kind, setKind] = useState<"all" | "future" | "option">("all");
  const [sample, setSample] = useState<string | null>(null);
  const [editing, setEditing] = useState<BasketItem | null>(null);
  const [command, setCommand] = useState<{ action: "buy" | "sell"; symbols?: string[]; label?: string } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const watch = useRunWatcher(() => {
    setRefresh((n) => n + 1);
    status.reload();
    live.reload();
  });
  const state = status.data?.sections[section];
  const inList = useMemo(() => new Set(list.items.map((b) => b.trading_symbol)), [list.items]);
  const today = todayIst();
  const active = list.items.filter((b) => b.active);
  const expired = active.filter((b) => b.expiry && b.expiry < today);
  const chosen = (accounts.data ?? []).filter((a) => a.automation_enabled && (info?.account_ids === null || info?.account_ids?.includes(a.id)));
  const saveSection = async (patch: Record<string, unknown>) => {
    await save(patch);
    status.reload();
  };
  const buyBlocked = !active.length
    ? "Add at least one contract first"
    : !chosen.length
      ? "Choose at least one autopilot account below"
      : expired.length
        ? `Remove expired contracts first: ${expired.map((b) => b.symbol).join(", ")}`
        : state?.buy_blocked ?? null;

  return (
    <>
      <p className="section-intro">{text.intro}</p>
      {info && (
        <div className="row wrap" style={{ gap: 8, margin: "-4px 2px 14px" }}>
          <Pill>Buy {info.opens}–{info.buy_until}</Pill>
          <Pill>Sell until {info.sell_until}</Pill>
          <Pill>{info.product === "NRML" ? "Carry forward (NRML)" : "Intraday (MIS)"}</Pill>
        </div>
      )}
      <motion.button className="search-trigger" onClick={() => setPickerOpen(true)} {...stagger(0)}>
        <Search size={20} />
        {text.browse}
        <span className="go" style={{ marginLeft: "auto" }}>{section === "fno" ? <Layers size={19} /> : <Gem size={19} />}</span>
      </motion.button>

      <MoneyCard info={info} accounts={accounts.data} live={live.data} items={list.basket.data} onSave={saveSection} onPreview={() => setCommand({ action: "buy" })} />

      <div className="row between" style={{ margin: "24px 2px 12px" }}>
        <b style={{ fontSize: 17 }}>Your contracts</b>
        {list.basket.data && <Pill tone={active.length ? "bull" : undefined}>{active.length} switched on</Pill>}
      </div>
      {!list.basket.data ? (
        <div className="stack">{[0, 1].map((i) => <Skeleton key={i} h={86} r={20} />)}</div>
      ) : list.items.length === 0 ? (
        <div className="card">
          <Empty icon={section === "fno" ? <Layers size={24} /> : <Gem size={24} />} title="Pick your contracts" action={<Button onClick={() => setPickerOpen(true)}><Search size={18} /> Search contracts</Button>}>
            Your list stays here. Nothing is bought until you tap <b>Buy</b> and confirm.
          </Empty>
        </div>
      ) : (
        <div className="grid-auto">
          <AnimatePresence>
            {list.items.map((item, i) => (
              <ItemCard key={item.id} item={item} info={info} today={today} index={i} onToggle={(v) => list.patch(item, { active: v })} onEdit={() => setEditing(item)} onRemove={() => list.remove(item)} />
            ))}
          </AnimatePresence>
        </div>
      )}

      <motion.div className="card" style={{ marginTop: 18 }} {...stagger(3)}>
        <motion.button className="lt-button" whileTap={{ scale: 0.98 }} disabled={!!buyBlocked} onClick={() => setCommand({ action: "buy" })}>
          <ShoppingCart size={24} />
          <span>
            <span className="lt-title">Buy now</span>
            <span className="lt-sub">
              {buyBlocked ?? `${active.length} contract${active.length === 1 ? "" : "s"} × ${chosen.length} account${chosen.length === 1 ? "" : "s"} · ${info?.sizing_mode === "custom" ? "fixed lots" : "money split equally"}`}
            </span>
          </span>
        </motion.button>
        <div className="muted" style={{ fontSize: 12.5, marginTop: 8, textAlign: "center" }}>
          You'll see every account's lots before anything is sent. Options are only bought, never sold first.
        </div>
      </motion.div>

      <PositionsCard section={section} title="Open positions (bought by TradeLab)" sellBlocked={state?.sell_blocked} refreshKey={refresh}
        onSell={(symbols, label) => setCommand({ action: "sell", symbols, label })} />

      <div style={{ marginTop: 18 }}>
        <SectionAccountsCard info={info} accounts={accounts.data} onSave={saveSection} />
      </div>

      <Picker<Contract>
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        placeholder={text.placeholder}
        refreshKey={kind}
        header={section === "fno" ? (
          <Segmented value={kind} onChange={setKind} options={[{ value: "all", label: "All" }, { value: "future", label: "Futures" }, { value: "option", label: "Options" }]} />
        ) : undefined}
        fetchResults={async (q) => {
          if (!q.trim()) return [];
          const r = await api<ContractSearch>(`/stocks/contracts?section=${section}&q=${encodeURIComponent(q)}${kind === "all" ? "" : `&kind=${kind}`}`);
          const note = Object.values(r.status).find((s) => s.source !== "kotak")?.message ?? null;
          setSample(note);
          return r.results;
        }}
        keyOf={(c) => c.trading_symbol}
        inList={inList}
        onAdd={(c) => list.add({ symbol: c.symbol, trading_symbol: c.trading_symbol, token: c.token, exchange_segment: c.exchange_segment, name: c.description, sizing_mode: "lots", sizing_value: 1 }, c.display)}
        banner={sample && (
          <div className="banner warn" style={{ margin: "8px 4px 10px", fontSize: 13 }}><AlertTriangle size={17} /><span>{sample}</span></div>
        )}
        emptyHint={(q) => (q.trim() ? `No contract matches “${q}”.` : text.hint)}
        renderRow={(c) => (
          <>
            <Ticker symbol={c.symbol} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="row" style={{ gap: 6 }}><b className="truncate">{c.display}</b></div>
              <div className="muted truncate" style={{ fontSize: 13 }}>{c.description}</div>
            </div>
            <Pill tone={c.kind === "option" ? (c.option_type === "CE" ? "bull" : "bear") : undefined}>{c.kind === "option" ? c.option_type : "FUT"}</Pill>
          </>
        )}
      />
      {editing && info && (
        <ItemEditSheet item={list.items.find((b) => b.id === editing.id) ?? editing} info={info} accounts={accounts.data ?? []} onClose={() => setEditing(null)}
          onSave={async (body) => { if (await list.patch(editing, body)) { toast(`${editing.symbol} updated`); setEditing(null); } }} />
      )}
      {command && (
        <CommandSheet section={section} action={command.action} symbols={command.symbols} label={command.label} onClose={() => setCommand(null)}
          onDone={(runId) => {
            setCommand(null);
            toast(command.action === "buy" ? "Buying now in every account…" : "Selling now…");
            watch(runId, `${info?.label ?? "Section"} ${command.action}`);
          }} />
      )}
    </>
  );
}
