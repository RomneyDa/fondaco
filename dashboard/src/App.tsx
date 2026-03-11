import { useCallback, useEffect, useRef, useState } from "react";
import { Routes, Route } from "react-router-dom";
import { Group, Panel, Separator } from "react-resizable-panels";
import { fetchBalance, fetchBotStatus, fetchLivePrefixes, fetchPrefixes } from "./api";
import type { BalanceInfo } from "./api";
import { useSSE } from "./hooks/useSSE";
import type { BotStatus, LivePayload, PrefixInfo } from "./types";
import { Sidebar } from "./components/Sidebar";
import { Home } from "./components/Home";
import { SessionList } from "./components/SessionList";
import { SessionDetail } from "./components/SessionDetail";
import { LiveView } from "./components/LiveView";
import { BotPanel } from "./components/BotPanel";
import { StrategyDetail } from "./components/StrategyDetail";
import { OptimizerPanel } from "./components/OptimizerPanel";
import { Positions } from "./components/Positions";

export interface PingEntry {
  time: Date;
  status: "ok" | "offline" | "error";
  detail?: string;
  latencyMs?: number;
}

const MAX_PINGS = 3;

export function App() {
  const [prefixes, setPrefixes] = useState<PrefixInfo[]>([]);
  const [livePrefixes, setLivePrefixes] = useState<string[]>([]);
  const [liveData, setLiveData] = useState<Record<string, LivePayload>>({});
  const [navVersion, setNavVersion] = useState(0);
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);
  const [pings, setPings] = useState<PingEntry[]>([]);
  const [balance, setBalance] = useState<BalanceInfo>({ portfolio: null });

  const addPing = useCallback((entry: PingEntry) => {
    setPings((prev) => [entry, ...prev].slice(0, MAX_PINGS));
  }, []);

  const refreshNav = useCallback(() => setNavVersion((v) => v + 1), []);

  const doPing = useCallback(async (): Promise<BotStatus | null> => {
    const t0 = performance.now();
    const res = await fetch("/api/bot/status");
    const latencyMs = Math.round(performance.now() - t0);
    const time = new Date();

    if (res.status === 502 || res.status === 503) {
      addPing({ time, status: "offline", detail: `${res.status}`, latencyMs });
      return null;
    }
    if (!res.ok) {
      addPing({ time, status: "error", detail: `HTTP ${res.status}`, latencyMs });
      return null;
    }
    try {
      const data = await res.json();
      if (typeof data.running !== "boolean") {
        addPing({ time, status: "error", detail: "bad payload", latencyMs });
        return null;
      }
      const markets = data.markets?.length ?? 0;
      addPing({
        time,
        status: "ok",
        detail: data.running ? `running · ${markets} market${markets !== 1 ? "s" : ""}` : "offline",
        latencyMs,
      });
      return data;
    } catch (e) {
      addPing({ time, status: "error", detail: String(e), latencyMs });
      return null;
    }
  }, [addPing]);

  const refreshBotStatus = useCallback(() => {
    doPing().then((s) => { if (s) setBotStatus(s); });
  }, [doPing]);

  useEffect(() => {
    fetchPrefixes().then(setPrefixes);
  }, [navVersion]);

  useEffect(() => {
    fetchLivePrefixes().then(setLivePrefixes);
    doPing().then((s) => setBotStatus(s ?? { running: false }));
    fetchBalance().then(setBalance);
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      doPing().then((s) => { if (s) setBotStatus(s); });
    }, 5000);
    const balanceInterval = setInterval(() => {
      fetchBalance().then(setBalance);
    }, 30000);
    return () => { clearInterval(interval); clearInterval(balanceInterval); };
  }, []);

  const [tradeRefresh, setTradeRefresh] = useState(0);

  useSSE({
    onLiveUpdate: (prefix, session) => {
      setLiveData((prev) => ({ ...prev, [prefix]: session }));
      setTradeRefresh((v) => v + 1);
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
    onSessionSaved: () => refreshNav(),
    onBotStatus: (status) => setBotStatus(status),
  });

  return (
    <Group direction="horizontal" autoSaveId="app-layout-v4" style={{ height: "100vh" }}>
      <Panel defaultSize="330px" min="180px" max="500px" className="min-w-0">
        <Sidebar
          prefixes={prefixes}
          livePrefixes={livePrefixes}
          botStatus={botStatus}
          pings={pings}
          balance={balance}
        />
      </Panel>
      <Separator className="resize-handle" />
      <Panel min="400px" className="min-w-0">
        <main className="h-full overflow-y-auto px-8 py-6">
          <Routes>
            <Route path="/" element={<Home prefixes={prefixes} />} />
            <Route
              path="/sessions/:prefix"
              element={<SessionList key={navVersion} />}
            />
            <Route
              path="/sessions/:prefix/:file"
              element={<SessionDetail />}
            />
            <Route
              path="/live/:prefix"
              element={<LiveView liveData={liveData} />}
            />
            <Route
              path="/bot"
              element={
                <BotPanel botStatus={botStatus} onRefresh={refreshBotStatus} tradeRefresh={tradeRefresh} />
              }
            />
            <Route
              path="/strategy/:market/:strategy"
              element={<StrategyDetail />}
            />
            <Route
              path="/optimizer"
              element={<OptimizerPanel />}
            />
            <Route
              path="/positions"
              element={<Positions />}
            />
          </Routes>
        </main>
      </Panel>
    </Group>
  );
}
