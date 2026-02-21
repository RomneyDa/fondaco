import type { BotStatus, LivePayload, PrefixInfo, SessionData, SessionSummary, TradeLogEntry, TradingInfo } from "./types";

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

export async function fetchBotStatus(): Promise<BotStatus | null> {
  try {
    const res = await fetch("/api/bot/status");
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export async function tradeBotOn(body: {
  strategy: string;
  params?: Record<string, string>;
  cash?: number;
  dry_run?: boolean;
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

export async function tradeBotOff(strategy?: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const opts: RequestInit = { method: "POST" };
    if (strategy) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify({ strategy });
    }
    const res = await fetch("/api/bot/trade-off", opts);
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

export async function setBotCash(cash: number, strategy?: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const body: Record<string, any> = { cash };
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
  slug_prefix: string;
  window?: number;
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
