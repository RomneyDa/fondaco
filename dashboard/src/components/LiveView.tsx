import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchLiveSession } from "../api";
import type { LivePayload } from "../types";
import { isLiveStatus, isSessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";
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

  // Initial fetch
  useEffect(() => {
    if (prefix) {
      let cancelled = false;
      setEnded(false);
      setHadSSE(false);
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

  // Collect all trades across strategies, tagged with strategy name
  const allTrades: any[] = [];
  if (trading?.strategies && Array.isArray(trading.strategies)) {
    for (const s of trading.strategies) {
      if (Array.isArray(s.trades)) {
        for (const t of s.trades) {
          allTrades.push({ ...t, _strategy: s.name });
        }
      }
    }
  } else if (trading?.trades && Array.isArray(trading.trades)) {
    for (const t of trading.trades) {
      allTrades.push({ ...t, _strategy: trading.strategy });
    }
  }
  allTrades.sort((a: any, b: any) => (a.elapsed_seconds ?? 0) - (b.elapsed_seconds ?? 0));

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
      <div className={`status-badge ${statusClass}`}>
        {statusLabel}
        {payload &&
          isLiveStatus(payload) &&
          payload.status === "waiting" &&
          payload.next_window_utc && (
            <span> ({formatCountdown(payload.next_window_utc)})</span>
          )}
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
          {allTrades.length > 0 && (
            <div className="trade-log" style={{ marginTop: 16 }}>
              <h3>Session Trades</h3>
              <div className="trade-log-table-wrap">
                <table className="trade-log-table">
                  <thead>
                    <tr>
                      <th>Elapsed</th>
                      <th>Strategy</th>
                      <th>Side</th>
                      <th>Price</th>
                      <th>Tokens</th>
                      <th>Amount</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {allTrades.map((t: any, i: number) => (
                      <tr key={i} className={t.success === false ? "trade-row-fail" : ""}>
                        <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "\u2014"}</td>
                        <td>{t._strategy || "\u2014"}</td>
                        <td>
                          <span className={`trade-side ${t.side.includes("up") ? "side-up" : "side-down"}`}>
                            {t.side === "buy_up" ? "Buy Up" : t.side === "buy_down" ? "Buy Down" : t.side === "sell_up" ? "Sell Up" : t.side === "sell_down" ? "Sell Down" : t.side}
                          </span>
                        </td>
                        <td>${t.price.toFixed(2)}</td>
                        <td>{t.tokens}</td>
                        <td>${t.amount.toFixed(2)}</td>
                        <td>
                          <span className={`trade-status-pill ${t.success === false ? "status-fail" : "status-ok"}`}>
                            {t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM"}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
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
