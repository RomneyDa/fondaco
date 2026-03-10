export interface PriceSample {
  timestamp_utc: string;
  elapsed_seconds: number;
  btc_price_usd: number | null;
  up_token_midpoint: number | null;
  down_token_midpoint: number | null;
  up_token_best_bid: number | null;
  up_token_best_ask: number | null;
  down_token_best_bid: number | null;
  down_token_best_ask: number | null;
}

export interface MarketMetadata {
  event_id: string;
  event_slug: string;
  event_title: string;
  condition_id: string;
  up_token_id: string;
  down_token_id: string;
  window_start_utc: string;
  window_end_utc: string;
  resolution_source: string;
}

export interface SessionData {
  metadata: MarketMetadata;
  initial_btc_price_usd: number | null;
  final_btc_price_usd: number | null;
  outcome: string | null;
  outcome_prices: string[] | null;
  samples: PriceSample[];
  collection_started_utc: string;
  collection_ended_utc: string;
  error_count: number;
  errors: string[];
  trading?: TradingInfo;
}

export interface TradeLogEntry {
  tick_index?: number;
  side: string;
  price: number;
  amount: number;
  tokens: number;
  order_id?: string | null;
  token_id?: string;
  success: boolean | null;
  error?: string | null;
  timestamp?: string | null;
  requested_tokens?: number | null;
  elapsed_seconds: number;
  session_start?: string;
  strategy?: string;
  market?: string;
  // Resolution record fields
  _type?: "resolution";
  outcome?: string;
  pnl?: number;
  starting_cash?: number;
  final_cash?: number;
  credit?: number;
  trades?: number;
}

export interface BotTrade {
  elapsed_seconds: number;
  side: string;
  price: number;
  amount: number;
  tokens: number;
  success: boolean | null;
  _strategy?: string;
}

export interface TradingInfo {
  enabled: boolean;
  strategy: string;
  params?: Record<string, any>;
  dry_run: boolean;
  cash: number;
  trades: BotTrade[];
}

export interface StrategySlotStatus {
  name: string;
  params?: Record<string, any>;
  dry_run: boolean;
  cash: number;
  bankroll?: number;
  starting_cash: number;
  per_session_cash?: number;
  session_cap?: number;
  session_cash_ratio?: { ratio: number; max: number };
  pid?: number | null;
  alive?: boolean;
  auto_exit?: boolean;
}

export interface MarketStatus {
  slug_prefix: string;
  slug_key: string;
  market_type?: string;
  running: boolean;
  stop_after_session?: boolean;
  session_count: number;
  trading_enabled: boolean;
  strategies: StrategySlotStatus[];
  tick_clients?: number;
}

export interface AppStatus {
  running: boolean;
  markets: MarketStatus[];
}

/** Backward compat — single-market status (deprecated) */
export interface BotStatus {
  running: boolean;
  slug_prefix?: string;
  session_count?: number;
  trading_enabled?: boolean;
  strategy?: string;
  dry_run?: boolean;
  cash?: number | null;
  strategies?: StrategySlotStatus[];
  tick_clients?: number;
  stop_after_session?: boolean;
  // Multi-market
  markets?: MarketStatus[];
}

export interface SessionSummary {
  file: string;
  outcome: string | null;
}

export interface PrefixInfo {
  name: string;
  count: number;
  recentSessions: SessionSummary[];
}

/** Status-only payload written when collector is waiting or resolving */
export interface LiveStatus {
  status: "waiting" | "resolving";
  slug_key: string;
  timestamp: string;
  next_window_utc: string | null;
  trading?: TradingInfo;
}

/** Data from the live file — either a status object or a full session */
export type LivePayload = LiveStatus | SessionData;

export function isSessionData(p: LivePayload): p is SessionData {
  return "samples" in p;
}

export function isLiveStatus(p: LivePayload): p is LiveStatus {
  return "status" in p && !("samples" in p);
}
