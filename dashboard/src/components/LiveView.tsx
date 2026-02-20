import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchLiveSession } from "../api";
import type { SessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";

interface Props {
  liveData: Record<string, SessionData>;
}

export function LiveView({ liveData }: Props) {
  const { prefix } = useParams<{ prefix: string }>();
  const [session, setSession] = useState<SessionData | null>(null);
  const [ended, setEnded] = useState(false);

  // Initial fetch
  useEffect(() => {
    if (prefix) {
      setEnded(false);
      fetchLiveSession(prefix).then((data) => {
        if (data) setSession(data);
      });
    }
  }, [prefix]);

  // SSE updates
  useEffect(() => {
    if (prefix && liveData[prefix]) {
      setSession(liveData[prefix]);
    }
  }, [prefix, liveData]);

  // Detect live-end (prefix disappears from liveData after being present)
  useEffect(() => {
    if (prefix && session && !liveData[prefix]) {
      setEnded(true);
    }
  }, [prefix, session, liveData]);

  return (
    <div>
      <h2>Live: {prefix}</h2>
      <div className={`status-badge ${ended ? "ended" : "collecting"}`}>
        {ended ? "Session Complete" : "Collecting"}
      </div>
      {session ? (
        <>
          <MetaGrid session={session} />
          <SessionCharts session={session} />
        </>
      ) : (
        <p className="subtitle">Waiting for data...</p>
      )}
    </div>
  );
}
