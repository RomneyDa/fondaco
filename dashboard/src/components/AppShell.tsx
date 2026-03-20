"use client";

import { useCallback, useEffect, useState } from "react";
import { Group, Panel, Separator } from "react-resizable-panels";
import { trpc } from "@/trpc/client";
import { useSSE } from "@/hooks/useSSE";
import type { BotStatus, LivePayload } from "@/types";
import { Sidebar } from "./Sidebar";

export interface PingEntry {
  time: Date;
  status: "ok" | "offline" | "error";
  detail?: string;
  latencyMs?: number;
}

const MAX_PINGS = 3;

export function AppShell({ children }: { children: React.ReactNode }) {
  const [livePrefixes, setLivePrefixes] = useState<string[]>([]);
  const [liveData, setLiveData] = useState<Record<string, LivePayload>>({});
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [pings, setPings] = useState<PingEntry[]>([]);
  const [serverDown, setServerDown] = useState(false);

  const addPing = useCallback((entry: PingEntry) => {
    setPings((prev) => [entry, ...prev].slice(0, MAX_PINGS));
  }, []);

  // Prefixes query
  const prefixesQuery = trpc.sessions.prefixes.useQuery(undefined, {
    refetchInterval: 30000,
  });
  const summariesQueries = trpc.sessions.summaries.useQuery(
    { prefix: "", limit: 10 },
    { enabled: false }
  );

  // Build prefix info from query data
  const prefixNames = prefixesQuery.data ?? [];

  // Live prefixes query
  const livePrefixesQuery = trpc.live.prefixes.useQuery(undefined, {
    refetchInterval: 10000,
  });

  useEffect(() => {
    if (livePrefixesQuery.data) {
      setLivePrefixes(livePrefixesQuery.data);
    }
  }, [livePrefixesQuery.data]);

  // Balance query
  const balanceQuery = trpc.portfolio.balance.useQuery(undefined, {
    refetchInterval: 30000,
  });

  // Bot status ping
  const doPing = useCallback(async (): Promise<BotStatus | null> => {
    const t0 = performance.now();
    let res: Response;
    try {
      res = await fetch("/api/trpc/bot.status");
    } catch {
      const latencyMs = Math.round(performance.now() - t0);
      addPing({ time: new Date(), status: "offline", detail: "server unreachable", latencyMs });
      setServerDown(true);
      return null;
    }
    const latencyMs = Math.round(performance.now() - t0);
    const time = new Date();

    if (res.status === 502 || res.status === 503) {
      addPing({ time, status: "offline", detail: `${res.status}`, latencyMs });
      setServerDown(false);
      return null;
    }
    if (!res.ok) {
      addPing({ time, status: "error", detail: `HTTP ${res.status}`, latencyMs });
      setServerDown(false);
      return null;
    }
    try {
      const json = await res.json();
      const data = json.result?.data?.json ?? json.result?.data;
      if (!data) {
        addPing({ time, status: "error", detail: "bad payload", latencyMs });
        setServerDown(false);
        return null;
      }
      setServerDown(false);
      const markets = Array.isArray(data.markets) ? data.markets.length : 0;
      const running = data.running ?? false;
      addPing({
        time,
        status: "ok",
        detail: running ? `running · ${markets} market${markets !== 1 ? "s" : ""}` : "offline",
        latencyMs,
      });
      setBotStatus(data);
      return data;
    } catch (e) {
      addPing({ time, status: "error", detail: String(e), latencyMs });
      setServerDown(false);
      return null;
    }
  }, [addPing]);

  useEffect(() => {
    doPing();
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      doPing();
    }, 5000);
    return () => clearInterval(interval);
  }, [doPing]);

  const refreshBotStatus = useCallback(() => {
    doPing();
  }, [doPing]);

  useSSE({
    onLiveUpdate: (prefix, session) => {
      setLiveData((prev) => ({ ...prev, [prefix]: session }));
    },
    onLiveStart: (prefix) => {
      setLivePrefixes((prev) => [...new Set([...prev, prefix])]);
    },
    onLiveEnd: (prefix) => {
      setLivePrefixes((prev) => prev.filter((p) => p !== prefix));
      setLiveData((prev) => {
        const next = { ...prev };
        delete next[prefix];
        return next;
      });
    },
    onBotStatus: (status) => setBotStatus(status),
  });

  if (serverDown) {
    return (
      <div style={{
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "16px",
        background: "var(--bg, #0a0a0a)",
        color: "var(--fg, #e0e0e0)",
      }}>
        <div style={{ fontSize: "48px", opacity: 0.5 }}>!</div>
        <h1 style={{ fontSize: "24px", fontWeight: 600 }}>API Server Unreachable</h1>
        <p style={{ opacity: 0.6, maxWidth: "400px", textAlign: "center", lineHeight: 1.5 }}>
          The dashboard API server is not running on port 3456.
          <br />
          Start it with <code style={{ background: "rgba(255,255,255,0.1)", padding: "2px 6px", borderRadius: "4px" }}>cd dashboard && npm run dev</code>
        </p>
        <button
          onClick={() => doPing()}
          style={{
            marginTop: "8px",
            padding: "8px 24px",
            border: "1px solid rgba(255,255,255,0.2)",
            borderRadius: "6px",
            background: "transparent",
            color: "inherit",
            cursor: "pointer",
            fontSize: "14px",
          }}
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <Group orientation="horizontal" style={{ height: "100vh" }}>
      <Panel defaultSize="230px" minSize="180px" maxSize="500px" className="min-w-0">
        <Sidebar
          prefixNames={prefixNames}
          livePrefixes={livePrefixes}
          botStatus={botStatus}
          pings={pings}
          balance={balanceQuery.data ?? { portfolio: null }}
        />
      </Panel>
      <Separator className="resize-handle" />
      <Panel minSize="400px" className="min-w-0">
        <main className="h-full overflow-y-auto px-8 py-6">
          {children}
        </main>
      </Panel>
    </Group>
  );
}

// Export these for use in page components
export { type BotStatus, type LivePayload };
export function useBotStatus() {
  // This is a simple hook that pages can use. The actual state lives in AppShell.
  // For now, we use the tRPC query directly in pages.
  return trpc.bot.status.useQuery(undefined, { refetchInterval: 5000 });
}
