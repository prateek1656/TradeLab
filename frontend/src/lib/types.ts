export interface User {
  id: number;
  name: string;
  email: string;
  created_at: string;
  last_login_at: string | null;
  is_owner: boolean;
}

export interface Account {
  id: number;
  name: string;
  broker: "kotak" | "paper";
  ucc_masked: string | null;
  client_name: string | null;
  automation_enabled: boolean;
  status: "connected" | "error";
  last_error: string | null;
  last_login_at: string | null;
  created_at: string;
}

export interface LiveSnapshot {
  id: number;
  ok: boolean;
  error?: string;
  available?: number;
  cash_free?: number | null;
  margin_used?: number;
  open_positions?: number;
  pnl?: number;
}

export interface Position {
  exchange_segment: string;
  trading_symbol: string;
  product: string;
  net_qty: number;
  buy_qty: number;
  sell_qty: number;
  buy_avg: number;
  sell_avg: number;
  ltp: number | null;
  pnl: number | null;
}

export interface Order {
  order_id: string;
  status: string;
  trading_symbol: string;
  exchange_segment: string;
  product: string;
  side: string;
  quantity: number;
  filled_qty: number;
  avg_price: number;
  order_type: string;
  placed_at: string | null;
  message: string | null;
  is_open: boolean;
}

export interface Instrument {
  symbol: string;
  trading_symbol: string;
  name: string;
  token: string | null;
  exchange_segment: string;
  series: string;
  isin: string | null;
}

export interface StockSearch {
  results: Instrument[];
  source: "kotak" | "builtin";
  count: number;
  loaded_on: string | null;
  message: string | null;
}

export type SizingMode = "qty" | "amount" | "percent" | "lots";

export type SectionKey = "intraday" | "holding" | "fno" | "commodity";

export interface BasketItem {
  id: number;
  section: SectionKey;
  lot_size: number;
  expiry: string | null;
  instrument_type: string | null;
  option_type: string | null;
  strike: number | null;
  symbol: string;
  trading_symbol: string;
  token: string | null;
  exchange_segment: string;
  name: string | null;
  side: "B" | "S";
  sizing_mode: SizingMode;
  sizing_value: number;
  active: boolean;
  account_ids: number[] | null;
}

export interface AutomationSettings {
  entry_time: string;
  exit_time: string;
  auto_exit: boolean;
  squareoff_scope: "all" | "intraday";
  sizing_mode: "equal" | "custom";
  reserve_pct: number;
}

export interface PreviewRow {
  account_id: number;
  account_name: string;
  symbol: string;
  side: "B" | "S";
  quantity: number;
  lots?: number | null;
  price: number | null;
  note: string | null;
  skip: string | null;
}

export interface Run {
  id: number;
  kind: "entry" | "exit";
  section: SectionKey | "all";
  funds_short: string[] | null;
  trigger: "scheduled" | "manual" | "catchup" | "command";
  trading_date: string;
  status: "running" | "done" | "partial" | "failed";
  summary: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface LogEntry {
  id: number;
  run_id: number | null;
  section: SectionKey | null;
  product: string | null;
  account_id: number | null;
  account_name: string;
  action: "buy" | "sell" | "cancel" | "login";
  symbol: string | null;
  quantity: number | null;
  status: "success" | "failed" | "skipped" | "unknown" | "pending";
  order_id: string | null;
  message: string | null;
  created_at: string;
}

export interface AutomationStatus {
  server_time: string;
  trading_day: boolean;
  holiday: string | null;
  phase: "closed" | "preopen" | "open" | "weekend" | "holiday";
  next: { entry: string | null; exit: string | null; sections: Partial<Record<SectionKey, { entry: string | null; exit: string | null }>> };
  settings: AutomationSettings;
  today_runs: Run[];
  traded_today: boolean;
  lets_trade: LetsTradeState;
  sections: Record<SectionKey, SectionState>;
}

/** A section as shown on Home and the Trade page: Let's Trade state (scheduled sections) or Buy/Sell state. */
export interface SectionState extends Partial<Omit<LetsTradeState, "section">> {
  section: SectionKey;
  label: string;
  stocks: string[];
  account_ids: number[];
  expired: string[];
  settings: SectionInfo;
  traded_today: boolean;
  buy_blocked?: string | null;
  sell_blocked?: string | null;
}

export interface SectionInfo {
  section: SectionKey;
  label: string;
  description: string;
  scheduled: boolean;
  can_sell: boolean;
  entry_time: string | null;
  exit_time: string | null;
  auto_exit: boolean;
  has_exit: boolean;
  product: string;
  products: string[];
  sides: ("B" | "S")[];
  segments: string[];
  sizing_mode: "equal" | "custom";
  reserve_pct: number;
  max_lots: number;
  lots: boolean;
  account_ids: number[] | null;
  opens: string;
  buy_until: string;
  sell_until: string;
}

export interface CommandOrder {
  account_id: number;
  account_name: string;
  symbol: string;
  trading_symbol: string;
  side: "B" | "S";
  quantity: number;
  lots: number | null;
  price: number | null;
  product: string;
  note: string | null;
  skip: string | null;
  funds_short: boolean;
}

export interface CommandPreview {
  section: SectionKey;
  action: "buy" | "sell";
  fingerprint: string;
  blocked: string | null;
  expired: string[];
  warnings?: string[];
  orders: CommandOrder[];
}

export interface SectionPositionRow {
  account_id: number;
  account_name: string;
  symbol: string;
  trading_symbol: string;
  exchange_segment: string;
  product: string;
  quantity: number;
  lots: number | null;
  avg_price: number | null;
  ltp: number | null;
  multiplier: number;
  value: number | null;
  pnl: number | null;
  side?: "B" | "S"; // S = sold first (a written option): closed by buying back
  expiry?: string | null; // contracts: the exchange settles them on this day if not sold
  expiry_note?: { level: "warn" | "info"; text: string } | null; // exchange / Kotak rule near expiry
}

export interface SectionPositions {
  section: SectionKey;
  rows: SectionPositionRow[];
  errors: { account_id: number; account_name: string; error: string }[];
  value: number;
  pnl: number;
}

export interface Contract {
  exchange_segment: string;
  trading_symbol: string;
  token: string;
  symbol: string;
  instrument_type: string;
  option_type: "CE" | "PE" | null;
  strike: number | null;
  expiry: string | null;
  lot_size: number;
  tick_size: number;
  multiplier: number;
  sample: boolean;
  kind: "future" | "option";
  display: string;
  description: string;
}

export interface ContractSearch {
  results: Contract[];
  status: Record<string, { source: "kotak" | "sample" | "none"; count: number; loaded_on: string | null; message: string | null }>;
}

export interface Etf {
  symbol: string;
  trading_symbol: string;
  name: string;
  isin: string | null;
  group: string;
  core: boolean;
  kind?: "index" | "bond" | "gold" | "silver" | "liquid" | "other";
  token: string | null;
  exchange_segment: string;
}

export interface EtfSearch {
  results: Etf[];
  groups: string[];
  total: number;
  source: "kotak" | "builtin";
  message: string | null;
}

export interface LetsTradeState {
  section?: SectionKey;
  label?: string;
  exit_at?: string | null;
  expired?: string[];
  product?: string;
  lots?: boolean;
  trade_date: string;
  entry_at: string;
  other_confirmed: string[];
  closing_soon: boolean;
  buys_now: boolean;
  stocks: string[];
  shorts: string[];
  sizing_mode: "equal" | "custom";
  reserve_pct: number;
  autopilot_accounts: number;
  account_ids: number[];
  fingerprint: string;
  session: {
    status: "armed" | "missed" | "cancelled" | "running" | "firing" | "done" | "skipped";
    confirmed_at: string;
    stocks: string[];
    sizing_mode: string;
    reserve_pct: number;
    changed: boolean;
    note: string | null;
  } | null;
}

export interface Holiday {
  day: string;
  description: string;
  source: "manual" | "nse" | "detected";
}

export interface HolidayList {
  items: Holiday[];
  nse: { last_success: string | null; last_error: string | null; count: number };
}

export interface StockListStatus {
  source: "kotak" | "builtin";
  count: number;
  loaded_on: string | null;
  message: string | null;
}

export interface FundsAlert {
  run_id: number;
  section: string;
  trading_date: string;
  started_at: string;
  accounts: string[];
}

export interface ReportDay {
  day: string;
  pnl: number;
  rows: number;
  accounts: number;
}

export interface ReportList {
  today: string;
  keep_days: number;
  oldest: string;
  today_live: boolean;
  days: ReportDay[];
}
