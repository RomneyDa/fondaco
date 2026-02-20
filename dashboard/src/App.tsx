import { useCallback, useEffect, useState } from "react";
import { Routes, Route } from "react-router-dom";
import { fetchLivePrefixes, fetchPrefixes } from "./api";
import { useSSE } from "./hooks/useSSE";
import type { PrefixInfo, SessionData } from "./types";
import { Sidebar } from "./components/Sidebar";
import { Home } from "./components/Home";
import { SessionList } from "./components/SessionList";
import { SessionDetail } from "./components/SessionDetail";
import { LiveView } from "./components/LiveView";

export function App() {
  const [prefixes, setPrefixes] = useState<PrefixInfo[]>([]);
  const [livePrefixes, setLivePrefixes] = useState<string[]>([]);
  const [liveData, setLiveData] = useState<Record<string, SessionData>>({});
  const [navVersion, setNavVersion] = useState(0);

  const refreshNav = useCallback(() => setNavVersion((v) => v + 1), []);

  useEffect(() => {
    fetchPrefixes().then(setPrefixes);
  }, [navVersion]);

  useEffect(() => {
    fetchLivePrefixes().then(setLivePrefixes);
  }, []);

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
    onSessionSaved: () => refreshNav(),
  });

  return (
    <div className="app">
      <Sidebar prefixes={prefixes} livePrefixes={livePrefixes} />
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
        </Routes>
      </main>
    </div>
  );
}
