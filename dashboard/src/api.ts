import type { AppStatus, BotStatus, LivePayload, PrefixInfo, SessionData, SessionSummary, TradeLogEntry, TradingInfo } from "./types";

export interface BalanceInfo {
  portfolio: number | null;
}

export async function fetchBalance(): Promise<BalanceInfo> {
  try {
    const res = await fetch("/api/balance");
    if (!res.ok) return { portfolio: null };
    return res.json();
  } catch {
    return { portfolio: null };
  }
}

export interface PositionInfo {
  asset: string;
  conditionId: string;
  size: number;
  avgPrice: number;
  currentValue: number;
  curPrice: number;
  cashPnl: number;
  percentPnl: number;
  redeemable: boolean;
  title: string;
  slug: string;
  outcome: string;
  icon: string;
}

export interface PositionsResponse {
  positions: PositionInfo[];
  usdc: number | null;
}

export async function fetchPositions(): Promise<PositionsResponse> {
  try {
    const res = await fetch("/api/positions");
    if (!res.ok) return { positions: [], usdc: null };
    return res.json();
  } catch {
    return { positions: [], usdc: null };
  }
}

export async function fetchPrefixes(): Promise<PrefixInfo[]> {
  const res = await fetch("/api/prefixes");
  const { prefixes } = (await res.json()) as { prefixes: string[] };

  const results = await Promise.all(
    prefixes.map(async (name) => {
      const sessRes = await fetch(`/api/sessions/${name}`);
      const { sessions: files } = (await sessRes.json()) as { sessions: string[] };

      let recentSessions: SessionSummary[];
      try {
        const sumRes = await fetch(`/api/sessions/${name}/summaries?limit=20`);
        if (sumRes.ok) {
          const data = (await sumRes.json()) as { sessions: SessionSummary[] };
          recentSessions = data.sessions;
        } else {
          recentSessions = files.slice(0, 20).map((f) => ({ file: f, outcome: null }));
        }
      } catch {
        recentSessions = files.slice(0, 20).map((f) => ({ file: f, outcome: null }));
      }

      return { name, count: files.length, recentSessions };
    })
  );

  return results;
}

export async function fetchSessionList(
  prefix: string
): Promise<string[]> {
  const res = await fetch(`/api/sessions/${prefix}`);
  const { sessions } = (await res.json()) as { sessions: string[] };
  return sessions;
}

export interface SessionStat {
  window_start: string;
  outcome: string;
  initial_btc: number;
  final_btc: number;
  return_bps: number;
  abs_return_bps: number;
  source_file: string;
}

export async function fetchSessionStats(
  prefix: string,
  params?: { last?: number; before?: string; after?: string; session?: string }
): Promise<SessionStat[]> {
  try {
    const qs = new URLSearchParams();
    if (params?.last) qs.set("last", String(params.last));
    if (params?.before) qs.set("before", params.before);
    if (params?.after) qs.set("after", params.after);
    if (params?.session) qs.set("session", params.session);
    const res = await fetch(`/api/sessions/${prefix}/stats?${qs.toString()}`);
    if (!res.ok) return [];
    const { stats } = (await res.json()) as { stats: SessionStat[] };
    return stats;
  } catch {
    return [];
  }
}

export async function fetchSessionSummaries(
  prefix: string,
  limit: number = 100
): Promise<SessionSummary[]> {
  const res = await fetch(`/api/sessions/${prefix}/summaries?limit=${limit}`);
  const { sessions } = (await res.json()) as { sessions: SessionSummary[] };
  return sessions;
}

export async function fetchSession(
  prefix: string,
  file: string
): Promise<SessionData> {
  const res = await fetch(`/api/session/${prefix}/${file}`);
  return res.json();
}

export async function fetchLivePrefixes(): Promise<string[]> {
  const res = await fetch("/api/live");
  const { live } = (await res.json()) as { live: string[] };
  return live;
}

export async function fetchLiveSession(
  prefix: string
): Promise<LivePayload | null> {
  const res = await fetch(`/api/live/${prefix}`);
  if (!res.ok) return null;
  return res.json();
}

/**
 * Fetch bot/app status. Returns:
 * - AppStatus object when app is running
 * - { running: false } when app is not running (502/503)
 * - null only on network failure
 */
export async function fetchBotStatus(): Promise<BotStatus | null> {
  try {
    const res = await fetch("/api/bot/status");
    if (res.status === 502 || res.status === 503) {
      // Can't reach control server — return null so callers
      // don't overwrite existing state (important during startup)
      return null;
    }
    if (!res.ok) return null;
    const data = await res.json();
    if (typeof data.running !== "boolean") {
      return null;
    }
    return data;
  } catch {
    return null;
  }
}

// --- Market lifecycle ---

export async function startMarket(body: {
  slug_prefix: string;
  window?: number;
  market_type?: string;
  restore?: boolean;
}): Promise<{ ok: boolean; error?: string; slug_key?: string }> {
  try {
    const res = await fetch("/api/bot/market-on", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function stopMarket(body: {
  slug_prefix: string;
  after_session?: boolean;
}): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/market-off", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// --- Bot lifecycle (with slug_prefix) ---

export async function tradeBotOn(body: {
  slug_prefix: string;
  strategy: string;
  params?: Record<string, string>;
  cash?: number;
  dry_run?: boolean;
  session_cap?: number;
  session_cash_ratio?: { ratio: number; max: number };
}): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/trade-on", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function tradeBotOff(slug_prefix: string, strategy?: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const body: Record<string, any> = { slug_prefix };
    if (strategy) body.strategy = strategy;
    const res = await fetch("/api/bot/trade-off", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function stopBot(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/stop", { method: "POST" });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function killBot(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/kill", { method: "POST" });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function reloadBot(slug_prefix: string, strategy: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/reload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug_prefix, strategy }),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function setBotCash(slug_prefix: string, cash: number, strategy?: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const body: Record<string, any> = { slug_prefix, cash };
    if (strategy) body.strategy = strategy;
    const res = await fetch("/api/bot/set-cash", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export interface SlugInfo {
  slug_prefix: string;
  window_duration_seconds: number;
  asset: string;
  example_title: string;
}

export async function fetchSlugs(): Promise<SlugInfo[]> {
  try {
    const res = await fetch("/api/slugs");
    const { slugs } = (await res.json()) as { slugs: SlugInfo[] };
    return slugs;
  } catch {
    return [];
  }
}

export async function startBot(body: {
  restore?: boolean;
}): Promise<{ ok: boolean; error?: string; pid?: number }> {
  try {
    const res = await fetch("/api/bot/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function setAutoExit(slug_prefix: string, enabled: boolean, strategy?: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const body: Record<string, any> = { slug_prefix, enabled };
    if (strategy) body.strategy = strategy;
    const res = await fetch("/api/bot/set-auto-exit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function fetchStrategies(): Promise<string[]> {
  try {
    const res = await fetch("/api/strategies");
    if (!res.ok) return [];
    const { strategies } = (await res.json()) as { strategies: string[] };
    return strategies;
  } catch {
    return [];
  }
}

export interface VersionSummary {
  sessions: number;
  traded: number;
  pnl: number;
  wins: number;
  losses: number;
  first: string;
  last: string;
}

export interface StrategyStats {
  strategy: string;
  market: string;
  stats: {
    total_sessions: number;
    traded_sessions: number;
    total_pnl: number;
    wins: number;
    losses: number;
    win_rate: number;
    sharpe: number;
    current_cash: number | null;
    current_version: string | null;
    total_fees: number;
  };
  sessions: Array<{
    timestamp: string;
    strategy: string;
    strategy_version?: string;
    outcome: string;
    trades: number;
    cash: number | null;
    pnl: number;
    credit: number;
    session_number: number | null;
    _active?: boolean;
    _tick_index?: number | null;
    _elapsed_seconds?: number | null;
    _window_end_utc?: string | null;
    _window_start_utc?: string | null;
  }>;
  trades: TradeLogEntry[];
  resolutions: TradeLogEntry[];
  version_summary: Record<string, VersionSummary>;
}

export async function fetchStrategyStats(market: string, strategy: string): Promise<StrategyStats | null> {
  try {
    const res = await fetch(`/api/strategy/${market}/${strategy}`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function fetchTrades(limit: number = 100): Promise<TradeLogEntry[]> {
  try {
    const res = await fetch(`/api/bot/trades?limit=${limit}`);
    if (!res.ok) return [];
    const { trades } = (await res.json()) as { trades: TradeLogEntry[] };
    return trades;
  } catch {
    return [];
  }
}

export interface MarketPnl {
  overall: number;
  last_24h: number;
  last_1h: number;
  by_strategy: Record<string, { overall: number; last_24h: number; last_1h: number }>;
}

export async function fetchMarketPnl(prefix: string): Promise<MarketPnl | null> {
  try {
    const res = await fetch(`/api/market/${prefix}/pnl`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function archiveSession(
  prefix: string,
  file: string
): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch(`/api/session/${prefix}/${file}/archive`, {
      method: "POST",
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function fetchSessionTrading(
  prefix: string,
  file: string
): Promise<TradingInfo | null> {
  try {
    const res = await fetch(`/api/session/${prefix}/${file}/trading`);
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

// --- Optimizer ---

export interface OptimizerStatus {
  running: boolean;
  pid: number | null;
  state?: string;
  interval_minutes?: number;
  lookback?: number;
  min_sharpe?: number;
  current_strategy?: string;
  current_market?: string;
  last_run?: string;
  next_run?: string;
  last_duration_seconds?: number;
  last_results?: OptimizerResult[];
  strategies_optimized?: number;
  error?: string;
}

export interface OptimizerResult {
  strategy: string;
  market: string;
  action: string;
  reason?: string;
  new_sharpe?: number;
  best_sharpe?: number;
  sharpe?: number;
  baseline_sharpe?: number;
  new_pnl?: number;
  baseline_pnl?: number;
  best_pnl?: number;
  new_params?: Record<string, any>;
  best_params?: Record<string, any>;
  baseline_params?: Record<string, any>;
  new_ratio?: { ratio: number; max: number };
  new_win_rate?: number;
  new_geo_return?: number;
  variants_tested?: number;
  sessions_used?: number;
}

export interface OptimizerEvent {
  timestamp: string;
  type: string;
  strategy?: string;
  market?: string;
  action?: string;
  success?: boolean;
  new_sharpe?: number;
  baseline_sharpe?: number;
  new_params?: Record<string, any>;
  reason?: string;
  [key: string]: any;
}

export interface OptimizerConfig {
  interval_minutes?: number;
  lookback?: number;
  min_sharpe?: number;
  starting_cash?: number;
}

export async function fetchOptimizerConfig(): Promise<OptimizerConfig> {
  try {
    const res = await fetch("/api/bot/optimizer-config");
    if (!res.ok) return {};
    const { config } = await res.json();
    return config ?? {};
  } catch {
    return {};
  }
}

export async function fetchOptimizerStatus(): Promise<OptimizerStatus | null> {
  try {
    const res = await fetch("/api/bot/optimizer-status");
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function fetchOptimizerEvents(): Promise<OptimizerEvent[]> {
  try {
    const res = await fetch("/api/bot/optimizer-events");
    if (!res.ok) return [];
    const { events } = (await res.json()) as { events: OptimizerEvent[] };
    return events;
  } catch {
    return [];
  }
}

export async function startOptimizer(body: {
  interval?: number;
  lookback?: number;
  min_sharpe?: number;
  starting_cash?: number;
}): Promise<{ ok: boolean; error?: string; pid?: number }> {
  try {
    const res = await fetch("/api/bot/optimizer-on", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function stopOptimizer(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/optimizer-off", { method: "POST" });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function updateOptimizerConfig(body: {
  interval_minutes?: number;
  lookback?: number;
  min_sharpe?: number;
  starting_cash?: number;
}): Promise<{ ok: boolean; error?: string; config?: Record<string, any> }> {
  try {
    const res = await fetch("/api/bot/optimizer-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return res.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
