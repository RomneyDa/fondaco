import type { BotStatus, LivePayload, PrefixInfo, SessionData, TradingInfo } from "./types";

export async function fetchPrefixes(): Promise<PrefixInfo[]> {
  const res = await fetch("/api/prefixes");
  const { prefixes } = (await res.json()) as { prefixes: string[] };

  const sessionLists = await Promise.all(
    prefixes.map((p) =>
      fetch(`/api/sessions/${p}`)
        .then((r) => r.json())
        .then((d: { sessions: string[] }) => d.sessions)
    )
  );

  return prefixes.map((name, i) => ({
    name,
    count: sessionLists[i].length,
    recentSessions: sessionLists[i].slice(0, 20),
  }));
}

export async function fetchSessionList(
  prefix: string
): Promise<string[]> {
  const res = await fetch(`/api/sessions/${prefix}`);
  const { sessions } = (await res.json()) as { sessions: string[] };
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

export async function tradeBotOff(): Promise<{ ok: boolean; error?: string }> {
  try {
    const res = await fetch("/api/bot/trade-off", { method: "POST" });
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
