import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchLiveSession } from "../api";
import type { LivePayload } from "../types";
import { isLiveStatus, isSessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";

interface Props {
  liveData: Record<string, LivePayload>;
}

function formatCountdown(targetUtc: string): string {
  const diff = Math.max(
    0,
    Math.round((new Date(targetUtc).getTime() - Date.now()) / 1000)
  );
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function LiveView({ liveData }: Props) {
  const { prefix } = useParams<{ prefix: string }>();
  const [payload, setPayload] = useState<LivePayload | null>(null);
  const [ended, setEnded] = useState(false);
  const [, setTick] = useState(0);

  // Initial fetch
  useEffect(() => {
    if (prefix) {
      setEnded(false);
      fetchLiveSession(prefix).then((data) => {
        if (data) setPayload(data);
      });
    }
  }, [prefix]);

  // SSE updates
  useEffect(() => {
    if (prefix && liveData[prefix]) {
      setPayload(liveData[prefix]);
    }
  }, [prefix, liveData]);

  // Detect live-end
  useEffect(() => {
    if (prefix && payload && !liveData[prefix]) {
      setEnded(true);
    }
  }, [prefix, payload, liveData]);

  // Tick the countdown every second when waiting
  useEffect(() => {
    if (payload && isLiveStatus(payload) && payload.status === "waiting") {
      const id = setInterval(() => setTick((t) => t + 1), 1000);
      return () => clearInterval(id);
    }
  }, [payload]);

  const statusLabel = ended
    ? "Session Complete"
    : payload && isLiveStatus(payload)
      ? payload.status === "waiting"
        ? "Waiting for next window"
        : "Waiting for resolution"
      : payload && isSessionData(payload)
        ? "Collecting"
        : "Connecting...";

  const statusClass = ended
    ? "ended"
    : payload && isSessionData(payload)
      ? "collecting"
      : "waiting";

  return (
    <div>
      <h2>Live: {prefix}</h2>
      <div className={`status-badge ${statusClass}`}>
        {statusLabel}
        {payload &&
          isLiveStatus(payload) &&
          payload.status === "waiting" &&
          payload.next_window_utc && (
            <span> ({formatCountdown(payload.next_window_utc)})</span>
          )}
      </div>

      {payload && isSessionData(payload) ? (
        <>
          <MetaGrid session={payload} />
          <SessionCharts session={payload} />
        </>
      ) : payload && isLiveStatus(payload) ? (
        <p className="subtitle">
          {payload.status === "waiting"
            ? `Next window starts at ${new Date(payload.next_window_utc || "").toLocaleTimeString()}`
            : "Window ended, waiting for market to resolve..."}
        </p>
      ) : (
        <p className="subtitle">Waiting for data...</p>
      )}
    </div>
  );
}
