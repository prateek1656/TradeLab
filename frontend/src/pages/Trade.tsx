import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Gem, Info, Layers, PiggyBank, Plus, Search, ShoppingCart, Zap } from "lucide-react";
import { useMemo, useState } from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";
import { PageHeader } from "../components/AppShell";
import {
  CommandSheet, ItemCard, ItemEditSheet, MoneyCard, Picker, PositionsCard, SectionAccountsCard, Ticker, todayIst, useRunWatcher, useSectionInfo,
} from "../components/SectionKit";
import { Button, Empty, Pill, Segmented, Skeleton, stagger } from "../components/ui";
import { api } from "../lib/api";
import { useApi } from "../lib/hooks";
import { useToast } from "../lib/toast";
import type { Account, AutomationStatus, BasketItem, Contract, ContractSearch, Etf, EtfSearch, Instrument, LiveSnapshot, SectionKey, StockSearch } from "../lib/types";
import { IntradayTab } from "./Stocks";

const TABS: { value: SectionKey; label: string; icon: typeof Zap }[] = [
  { value: "intraday", label: "Intraday", icon: Zap },
  { value: "holding", label: "Holding", icon: PiggyBank },
  { value: "fno", label: "F&O", icon: Layers },
  { value: "commodity", label: "Commodity", icon: Gem },
];

const SUBTITLE: Record<SectionKey, string> = {
  intraday: "Stocks bought in the morning and squared off the same day",
  holding: "Stocks & ETFs for delivery — buy and sell on your command",
  fno: "Futures & options — buy or sell first, on your command",
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

// ───────────────────────────────────────────── Buy now (Holding, F&O, Commodity)

/** The big Buy button of an on-command section, with what it will do or why it can't yet. */
function BuyCard({ title = "Buy now", blocked, count, accounts, custom, noun, note, onBuy }: {
  title?: string;
  blocked: string | null;
  count: number;
  accounts: number;
  custom: boolean;
  noun: [string, string];
  note: string;
  onBuy: () => void;
}) {
  return (
    <motion.div className="card" style={{ marginTop: 18 }} {...stagger(3)}>
      <motion.button className="lt-button" whileTap={{ scale: 0.98 }} disabled={!!blocked} onClick={onBuy}>
        <ShoppingCart size={24} />
        <span>
          <span className="lt-title">{title}</span>
          <span className="lt-sub">
            {blocked ?? `${count} ${count === 1 ? noun[0] : noun[1]} × ${accounts} account${accounts === 1 ? "" : "s"} · ${custom ? "your own sizes" : "money split equally"}`}
          </span>
        </span>
      </motion.button>
      <div className="muted" style={{ fontSize: 12.5, marginTop: 8, textAlign: "center" }}>{note}</div>
    </motion.div>
  );
}

/** Why Buy can't be pressed yet (or null). */
function buyBlockedReason(active: BasketItem[], chosen: Account[], expired: BasketItem[], noun: string, marketBlocked?: string | null) {
  if (!active.length) return `Add at least one ${noun} first`;
  if (!chosen.length) return "Choose at least one autopilot account below";
  if (expired.length) return `Remove expired contracts first: ${expired.map((b) => b.symbol).join(", ")}`;
  return marketBlocked ?? null;
}

// ───────────────────────────────────────────── Holding

function HoldingTab() {
  const toast = useToast();
  const { info, save } = useSectionInfo("holding");
  const accounts = useApi<Account[]>("/accounts");
  const status = useApi<AutomationStatus>("/automation/status", { interval: 15000 });
  const live = useApi<LiveSnapshot[]>("/accounts/live");
  const list = useSectionList("holding", () => status.reload());
  const [picker, setPicker] = useState<"stocks" | "etfs" | null>(null);
  const [group, setGroup] = useState<string>("");
  const [groups, setGroups] = useState<string[]>([]);
  const [etfTotal, setEtfTotal] = useState<number | null>(null);
  const [stockNote, setStockNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<BasketItem | null>(null);
  const [command, setCommand] = useState<{ action: "buy" | "sell"; symbols?: string[]; label?: string; opened?: "B" | "S" } | null>(null);
  const [refresh, setRefresh] = useState(0);
  const watch = useRunWatcher(() => {
    setRefresh((n) => n + 1);
    status.reload();
    live.reload();
  });
  const state = status.data?.sections.holding;
  const inList = useMemo(() => new Set(list.items.map((b) => b.trading_symbol)), [list.items]);
  const active = list.items.filter((b) => b.active);
  const chosen = (accounts.data ?? []).filter((a) => a.automation_enabled && (info?.account_ids === null || info?.account_ids?.includes(a.id)));
  const saveSection = async (patch: Record<string, unknown>) => {
    await save(patch);
    status.reload();
  };
  const today = todayIst();
  const blocked = buyBlockedReason(active, chosen, [], "stock or ETF", state?.buy_blocked);
  const addBody = (symbol: string, trading_symbol: string, token: string | null, name: string) =>
    ({ symbol, trading_symbol, token, exchange_segment: "nse_cm", name, sizing_mode: "qty", sizing_value: 1 });

  return (
    <>
      <p className="section-intro">
        Stocks and ETFs bought for <b>delivery</b> in every account. Nothing here happens by time: TradeLab buys only when you tap <b>Buy now</b> and sells only when you tap <b>Sell</b>. Delivery holdings are never squared off.
      </p>
      {info && (
        <div className="row wrap" style={{ gap: 8, margin: "-4px 2px 14px" }}>
          <Pill>Buy and sell {info.opens}–{info.sell_until}</Pill>
          <Pill>Delivery (CNC) · paid from cash</Pill>
          <Pill>Never sold by time</Pill>
        </div>
      )}
      <div className="grid grid-2-md" style={{ gap: 10 }}>
        <motion.button className="search-trigger" onClick={() => setPicker("stocks")} {...stagger(0)}>
          <Search size={20} />
          Search all NSE stocks…
          <span className="go" style={{ marginLeft: "auto" }}><Plus size={19} /></span>
        </motion.button>
        <motion.button className="search-trigger" onClick={() => setPicker("etfs")} {...stagger(0)}>
          <PiggyBank size={20} />
          Browse all ETFs…
          <span className="go" style={{ marginLeft: "auto" }}><Plus size={19} /></span>
        </motion.button>
      </div>

      <MoneyCard info={info} accounts={accounts.data} live={live.data} items={list.basket.data} onSave={saveSection} onPreview={() => setCommand({ action: "buy" })} />

      <div className="row between" style={{ margin: "24px 2px 12px" }}>
        <b style={{ fontSize: 17 }}>Your stocks &amp; ETFs</b>
        {list.basket.data && <Pill tone={active.length ? "bull" : undefined}>{active.length} switched on</Pill>}
      </div>
      {!list.basket.data ? (
        <div className="stack">{[0, 1].map((i) => <Skeleton key={i} h={86} r={20} />)}</div>
      ) : list.items.length === 0 ? (
        <div className="card">
          <Empty icon={<PiggyBank size={24} />} title="Pick what to hold"
            action={<div className="row wrap" style={{ justifyContent: "center" }}><Button onClick={() => setPicker("stocks")}><Search size={18} /> Stocks</Button><Button variant="ghost" onClick={() => setPicker("etfs")}><PiggyBank size={18} /> ETFs</Button></div>}>
            Any NSE stock, or any ETF — index (Nifty, Bank, IT…), bond &amp; gilt, gold, silver or liquid. Nothing is bought until you tap <b>Buy now</b>.
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

      <BuyCard blocked={blocked} count={active.length} accounts={chosen.length} custom={info?.sizing_mode === "custom"} noun={["stock / ETF", "stocks / ETFs"]}
        note="You'll see every account's quantities before anything is sent. Nothing is ever bought or sold by time."
        onBuy={() => setCommand({ action: "buy" })} />

      <PositionsCard section="holding" title="Your holdings (bought by TradeLab)" sellBlocked={state?.sell_blocked} refreshKey={refresh}
        onSell={(symbols, label, opened) => setCommand({ action: "sell", symbols, label, opened })}
        footer={
          <div className="muted" style={{ fontSize: 12.5, marginTop: 10 }}>
            To sell delivery shares from an app, Kotak needs <b>DDPI</b> turned on for the account (a one-time e-sign in the Kotak Neo app). Without it, the account holder must approve each day's sale with TPIN in the Kotak Neo app first. Units bought today can be sold without it.
          </div>
        } />

      <div style={{ marginTop: 18 }}>
        <SectionAccountsCard info={info} accounts={accounts.data} onSave={saveSection} />
      </div>

      <Picker<Instrument>
        open={picker === "stocks"}
        onClose={() => setPicker(null)}
        placeholder="Search by symbol or company"
        fetchResults={async (q) => {
          const r = await api<StockSearch>(`/stocks/search?q=${encodeURIComponent(q)}&limit=50`);
          setStockNote(r.source === "builtin" ? (r.message ?? "Connect a Kotak Neo account to search every NSE stock.") : null);
          return r.results;
        }}
        keyOf={(i) => i.trading_symbol}
        inList={inList}
        onAdd={(i) => list.add(addBody(i.symbol, i.trading_symbol, i.token, i.name), i.symbol)}
        banner={stockNote && <div className="banner warn" style={{ margin: "8px 4px 10px", fontSize: 13 }}><Info size={17} /><span>Showing popular stocks only. {stockNote}</span></div>}
        emptyHint={(q) => (q ? `No NSE stock matches “${q}”.` : "Type a symbol or company name.")}
        renderRow={(i) => (
          <>
            <Ticker symbol={i.symbol} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 800 }}>{i.symbol}</div>
              <div className="muted truncate" style={{ fontSize: 13 }}>{i.name}</div>
            </div>
            <span className="muted hide-sm" style={{ fontSize: 12 }}>NSE · delivery</span>
          </>
        )}
      />
      <Picker<Etf>
        open={picker === "etfs"}
        onClose={() => setPicker(null)}
        placeholder="Search ETFs: Nifty, gold, bond…"
        refreshKey={group}
        header={groups.length > 0 && (
          <div className="chip-select etf-groups">
            {["", ...groups].map((g) => (
              <button key={g || "all"} type="button" className={`chip ${group === g ? "on" : ""}`} onClick={() => setGroup(g)}>{g || "All ETFs"}</button>
            ))}
          </div>
        )}
        fetchResults={async (q) => {
          const r = await api<EtfSearch>(`/stocks/etfs?q=${encodeURIComponent(q)}&limit=${q ? 80 : 400}${group ? `&group=${encodeURIComponent(group)}` : ""}`);
          setGroups(r.groups);
          setEtfTotal(r.total);
          return r.results;
        }}
        keyOf={(e) => e.trading_symbol}
        inList={inList}
        onAdd={(e) => list.add(addBody(e.symbol, e.trading_symbol, e.token, e.name), e.symbol)}
        banner={etfTotal !== null && !group && (
          <div className="banner" style={{ margin: "8px 4px 10px", fontSize: 13 }}>
            <Info size={17} />
            <span>{etfTotal} ETFs on NSE, grouped: stock-market index ETFs first (most traded on top), then bond &amp; gilt, gold &amp; silver and liquid ETFs.</span>
          </div>
        )}
        emptyHint={(q) => `No ETF matches “${q}”.`}
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
      {command && (
        <CommandSheet section="holding" action={command.action} symbols={command.symbols} label={command.label} opened={command.opened} onClose={() => setCommand(null)}
          onDone={(runId) => {
            setCommand(null);
            toast(command.action === "buy" ? "Buying now in every account…" : "Selling now…");
            watch(runId, `Holding ${command.action}`);
          }} />
      )}
    </>
  );
}

// ───────────────────────────────────────────── F&O and Commodity

const DERIV = {
  fno: {
    intro: "NSE and BSE futures and options. Tap Buy now and every account trades at once: the money free in each is split equally between your contracts and turned into lots. Each contract can be bought or sold first — choose it with Edit. Tap Exit to close what TradeLab opened.",
    placeholder: "NIFTY 25000 CE, BANKNIFTY FUT, SENSEX…",
    hint: "Type an index or stock, a strike, CE/PE or FUT — e.g. NIFTY 25000 CE, BANKNIFTY FUT, RELIANCE FUT.",
    browse: "Search futures & options…",
  },
  commodity: {
    intro: "MCX gold, silver, crude oil, natural gas and more. Tap Buy now and every account trades at once: the money free in each is split equally between your contracts and turned into lots. Each contract can be bought or sold first — choose it with Edit. Tap Exit to close what TradeLab opened.",
    placeholder: "GOLDM, SILVERM, CRUDEOIL, NATURALGAS…",
    hint: "Type a commodity — e.g. GOLD, GOLDM, SILVERM, CRUDEOIL, NATURALGAS, COPPER. Add CE or PE (and a strike) for options, e.g. CRUDEOIL 5500 CE.",
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
  const [listNote, setListNote] = useState<string | null>(null);
  const [editing, setEditing] = useState<BasketItem | null>(null);
  const [command, setCommand] = useState<{ action: "buy" | "sell"; symbols?: string[]; label?: string; opened?: "B" | "S" } | null>(null);
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
  const buyBlocked = buyBlockedReason(active, chosen, expired, "contract", state?.buy_blocked);
  const writing = active.some((b) => b.side === "S");

  return (
    <>
      <p className="section-intro">{text.intro}</p>
      {info && (
        <div className="row wrap" style={{ gap: 8, margin: "-4px 2px 14px" }}>
          <Pill>Buy and sell {info.opens}–{info.sell_until}</Pill>
          <Pill>Carry forward (NRML)</Pill>
          <Pill>Never closed by time</Pill>
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
            Your list stays here. Nothing is traded until you tap <b>Buy now</b> and confirm.
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

      <BuyCard title={writing ? "Trade now" : "Buy now"} blocked={buyBlocked} count={active.length} accounts={chosen.length} custom={info?.sizing_mode === "custom"} noun={["contract", "contracts"]}
        note="You'll see every account's lots before anything is sent. Nothing is opened or closed by time — only when you tap this button or Exit."
        onBuy={() => setCommand({ action: "buy" })} />

      <PositionsCard section={section} title="Open positions (opened by TradeLab)" sellBlocked={state?.sell_blocked} refreshKey={refresh}
        onSell={(symbols, label, opened) => setCommand({ action: "sell", symbols, label, opened })} />

      <div style={{ marginTop: 18 }}>
        <SectionAccountsCard info={info} accounts={accounts.data} onSave={saveSection} />
      </div>

      <Picker<Contract>
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        placeholder={text.placeholder}
        refreshKey={kind}
        header={<Segmented value={kind} onChange={setKind} options={[{ value: "all", label: "All" }, { value: "future", label: "Futures" }, { value: "option", label: "Options" }]} />}
        fetchResults={async (q) => {
          if (!q.trim()) return [];
          const r = await api<ContractSearch>(`/stocks/contracts?section=${section}&q=${encodeURIComponent(q)}${kind === "all" ? "" : `&kind=${kind}`}`);
          const note = Object.values(r.status).find((s) => s.source !== "kotak")?.message ?? null;
          setListNote(note);
          return r.results;
        }}
        keyOf={(c) => c.trading_symbol}
        inList={inList}
        onAdd={(c) => list.add({ symbol: c.symbol, trading_symbol: c.trading_symbol, token: c.token, exchange_segment: c.exchange_segment, name: c.description, sizing_mode: "lots", sizing_value: 1 }, c.display)}
        banner={listNote && (
          <div className="banner warn" style={{ margin: "8px 4px 10px", fontSize: 13 }}><AlertTriangle size={17} /><span>{listNote}</span></div>
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
        <CommandSheet section={section} action={command.action} symbols={command.symbols} label={command.label} opened={command.opened} onClose={() => setCommand(null)}
          onDone={(runId) => {
            setCommand(null);
            toast(command.action === "buy" ? (writing ? "Placing orders in every account…" : "Buying now in every account…") : "Closing now…");
            watch(runId, `${info?.label ?? "Section"} ${command.action === "sell" ? "exit" : command.action}`);
          }} />
      )}
    </>
  );
}
