import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchLiveSession, archiveSession, fetchSessionList } from "../api";
import type { LivePayload } from "../types";
import { isLiveStatus, isSessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";
import { fmtTimeOnly } from "../utils/formatTime";

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
  const [hadSSE, setHadSSE] = useState(false);
  const [, setTick] = useState(0);
  const stickyTradesRef = useRef<any[]>([]);
  const [archiving, setArchiving] = useState(false);
  const [archived, setArchived] = useState(false);

  // Initial fetch
  useEffect(() => {
    if (prefix) {
      let cancelled = false;
      setEnded(false);
      setHadSSE(false);
      setArchived(false);
      stickyTradesRef.current = [];
      fetchLiveSession(prefix).then((data) => {
        if (!cancelled && data) setPayload(data);
      });
      return () => { cancelled = true; };
    }
  }, [prefix]);

  // SSE updates
  useEffect(() => {
    if (prefix && liveData[prefix]) {
      setPayload(liveData[prefix]);
      setHadSSE(true);
    }
  }, [prefix, liveData]);

  // Detect live-end (only after we've received at least one SSE update)
  useEffect(() => {
    if (prefix && hadSSE && payload && !liveData[prefix]) {
      setEnded(true);
    }
  }, [prefix, hadSSE, payload, liveData]);

  // Tick the countdown every second when waiting
  const isWaiting = payload && isLiveStatus(payload) && payload.status === "waiting";
  useEffect(() => {
    if (isWaiting) {
      const id = setInterval(() => setTick((t) => t + 1), 1000);
      return () => clearInterval(id);
    }
  }, [isWaiting]);

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

  // Extract trading info from payload
  const trading =
    payload && "trading" in payload ? (payload as any).trading : undefined;

  // Collect all trades across strategies, tagged with strategy name.
  // Persist trades — some live updates may omit trading data
  // (race between session file and sidecar writes), so never shrink the list
  // within the same session. Reset when a new session starts (status goes
  // to waiting/resolving, or elapsed_seconds drops).
  const currentTrades: any[] = [];
  if (trading?.strategies && Array.isArray(trading.strategies)) {
    for (const s of trading.strategies) {
      if (Array.isArray(s.trades)) {
        for (const t of s.trades) {
          currentTrades.push({ ...t, _strategy: s.name });
        }
      }
    }
  } else if (trading?.trades && Array.isArray(trading.trades)) {
    for (const t of trading.trades) {
      currentTrades.push({ ...t, _strategy: trading.strategy });
    }
  }

  // Detect new session: if payload is a status (waiting/resolving) or
  // the latest elapsed_seconds is less than what we had, clear stale trades
  const isStatus = payload && isLiveStatus(payload);
  if (isStatus) {
    stickyTradesRef.current = [];
  } else if (currentTrades.length >= stickyTradesRef.current.length) {
    currentTrades.sort((a: any, b: any) => (a.elapsed_seconds ?? 0) - (b.elapsed_seconds ?? 0));
    stickyTradesRef.current = currentTrades;
  } else if (currentTrades.length === 0 && payload && isSessionData(payload)) {
    // New session with no trades yet — check if elapsed is early
    const elapsed = payload.samples?.length
      ? payload.samples[payload.samples.length - 1].elapsed_seconds
      : 0;
    const prevMaxElapsed = stickyTradesRef.current.length > 0
      ? Math.max(...stickyTradesRef.current.map((t: any) => t.elapsed_seconds ?? 0))
      : 0;
    if (elapsed < prevMaxElapsed) {
      stickyTradesRef.current = [];
    }
  }
  const allTrades = stickyTradesRef.current;

  const handleArchive = async () => {
    if (!prefix || archiving) return;
    if (!window.confirm("Archive the most recent session?")) return;
    setArchiving(true);
    try {
      const sessions = await fetchSessionList(prefix);
      if (sessions.length === 0) {
        alert("No saved sessions found to archive.");
        setArchiving(false);
        return;
      }
      const latest = sessions[0]; // sorted newest-first by the API
      const result = await archiveSession(prefix, latest);
      if (result.ok) {
        setArchived(true);
      } else {
        alert("Failed to archive: " + (result.error || "unknown error"));
      }
    } catch (e) {
      alert("Failed to archive: " + String(e));
    }
    setArchiving(false);
  };

  return (
    <div>
      <h2>
        Live: {prefix}
        <a
          href={
            payload && isSessionData(payload) && payload.metadata?.event_slug
              ? `https://polymarket.com/event/${payload.metadata.event_slug}`
              : `https://polymarket.com/event/${prefix}`
          }
          target="_blank"
          rel="noopener noreferrer"
          style={{ fontSize: 13, marginLeft: 8, fontWeight: 400 }}
        >
          polymarket
        </a>
      </h2>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <div className={`status-badge ${statusClass}`}>
          {statusLabel}
          {payload &&
            isLiveStatus(payload) &&
            payload.status === "waiting" &&
            payload.next_window_utc && (
              <span> ({formatCountdown(payload.next_window_utc)})</span>
            )}
        </div>
        <div style={{ flex: 1 }} />
        {(ended || (payload && isSessionData(payload))) && !archived && (
          <button
            className="archive-btn"
            onClick={handleArchive}
            disabled={archiving}
          >
            {archiving ? "Archiving..." : "Archive"}
          </button>
        )}
        {archived && <span className="subtitle">Archived</span>}
      </div>

      {trading && (
        <div className="trading-badge-row" style={{ flexWrap: "wrap", gap: 6 }}>
          {trading.strategies && trading.strategies.length > 0
            ? trading.strategies.map((s: any) => {
                const trades = Array.isArray(s.trades) ? s.trades.length : (s.trades ?? 0);
                return (
                  <span key={s.name} className="status-badge collecting" style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                    {s.name}
                    {s.dry_run && <span className="dry-run-tag">DRY</span>}
                    {trades > 0 && (
                      <span className="trade-count-badge">{trades} trade{trades !== 1 ? "s" : ""}</span>
                    )}
                    {s.cash != null && (
                      <span className="trade-count-badge">${s.cash.toFixed(2)}</span>
                    )}
                  </span>
                );
              })
            : (
              <>
                <span className="status-badge collecting">
                  {trading.strategy || "Trading"}
                </span>
                {trading.dry_run && (
                  <span className="dry-run-tag">DRY RUN</span>
                )}
              </>
            )}
        </div>
      )}

      {payload && isSessionData(payload) ? (
        <>
          <MetaGrid session={payload} />
          <SessionCharts
            session={payload}
            trades={allTrades.length > 0 ? allTrades : undefined}
            isDryRun={trading?.dry_run}
          />
          <TradeTable trades={allTrades} />
        </>
      ) : payload && isLiveStatus(payload) ? (
        <p className="subtitle">
          {payload.status === "waiting"
            ? `Next window starts at ${fmtTimeOnly(payload.next_window_utc)}`
            : "Window ended, waiting for market to resolve..."}
        </p>
      ) : (
        <p className="subtitle">Waiting for data...</p>
      )}
    </div>
  );
}
