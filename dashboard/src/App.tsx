import { useCallback, useEffect, useState } from "react";
import { Routes, Route } from "react-router-dom";
import { fetchBotStatus, fetchLivePrefixes, fetchPrefixes } from "./api";
import { useSSE } from "./hooks/useSSE";
import type { BotStatus, LivePayload, PrefixInfo } from "./types";
import { Sidebar } from "./components/Sidebar";
import { Home } from "./components/Home";
import { SessionList } from "./components/SessionList";
import { SessionDetail } from "./components/SessionDetail";
import { LiveView } from "./components/LiveView";
import { BotPanel } from "./components/BotPanel";

export function App() {
  const [prefixes, setPrefixes] = useState<PrefixInfo[]>([]);
  const [livePrefixes, setLivePrefixes] = useState<string[]>([]);
  const [liveData, setLiveData] = useState<Record<string, LivePayload>>({});
  const [navVersion, setNavVersion] = useState(0);
  const [botStatus, setBotStatus] = useState<BotStatus | null>(null);

  const refreshNav = useCallback(() => setNavVersion((v) => v + 1), []);
  const refreshBotStatus = useCallback(() => {
    fetchBotStatus().then(setBotStatus);
  }, []);

  useEffect(() => {
    fetchPrefixes().then(setPrefixes);
  }, [navVersion]);

  useEffect(() => {
    fetchLivePrefixes().then(setLivePrefixes);
    fetchBotStatus().then(setBotStatus);
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
    <div className="app">
      <Sidebar
        prefixes={prefixes}
        livePrefixes={livePrefixes}
        botStatus={botStatus}
      />
      <main className="content">
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
        </Routes>
      </main>
    </div>
  );
}
