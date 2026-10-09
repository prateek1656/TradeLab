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

export type SizingMode = "qty" | "amount" | "percent";

export interface BasketItem {
  id: number;
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
  price: number | null;
  note: string | null;
  skip: string | null;
}

export interface Run {
  id: number;
  kind: "entry" | "exit";
  trigger: "scheduled" | "manual" | "catchup";
  trading_date: string;
  status: "running" | "done" | "partial" | "failed";
  summary: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface LogEntry {
  id: number;
  run_id: number | null;
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
  next: { entry: string | null; exit: string | null };
  settings: AutomationSettings;
  today_runs: Run[];
  traded_today: boolean;
  lets_trade: LetsTradeState;
}

export interface LetsTradeState {
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
