import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { fetchSession, fetchSessionTrading, archiveSession } from "../api";
import type { BotTrade, SessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";

export function SessionDetail() {
  const { prefix, file } = useParams<{ prefix: string; file: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<SessionData | null>(null);
  const [trading, setTrading] = useState<any | null>(null);
  const [archiving, setArchiving] = useState(false);

  useEffect(() => {
    if (prefix && file) {
      fetchSession(prefix, file).then(setSession);
      fetchSessionTrading(prefix, file).then(setTrading);
    }
  }, [prefix, file]);

  if (!session) return <p className="subtitle">Loading...</p>;

  // Collect all trades across strategies (handles both legacy single and multi-worker format)
  const allTrades: BotTrade[] = [];
  const strategyEntries: any[] = [];

  if (trading) {
    if (trading.strategies && Array.isArray(trading.strategies)) {
      for (const s of trading.strategies) {
        strategyEntries.push(s);
        if (Array.isArray(s.trades)) {
          for (const t of s.trades) {
            allTrades.push({ ...t, _strategy: s.strategy || s.name });
          }
        }
      }
    } else if (trading.trades && Array.isArray(trading.trades)) {
      strategyEntries.push(trading);
      for (const t of trading.trades) {
        allTrades.push({ ...t, _strategy: trading.strategy });
      }
    }
    allTrades.sort((a, b) => (a.elapsed_seconds ?? 0) - (b.elapsed_seconds ?? 0));
  }

  const anyDryRun = strategyEntries.some((s) => s.dry_run);

  const handleArchive = async () => {
    if (!prefix || !file || archiving) return;
    if (!window.confirm("Archive this session?")) return;
    setArchiving(true);
    const result = await archiveSession(prefix, file);
    setArchiving(false);
    if (result.ok) {
      navigate(`/sessions/${prefix}`);
    } else {
      alert("Failed to archive: " + (result.error || "unknown error"));
    }
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <h2 style={{ flex: 1 }}>{session.metadata?.event_title || file?.replace(".json", "")}</h2>
        <button
          className="archive-btn"
          onClick={handleArchive}
          disabled={archiving}
        >
          {archiving ? "Archiving..." : "Archive"}
        </button>
      </div>

      {strategyEntries.length > 0 && (
        <div className="trading-summary">
          <div className="meta-grid">
            {strategyEntries.map((s, i) => (
              <div className="meta-card" key={i}>
                <div className="meta-label">
                  {strategyEntries.length > 1 ? s.strategy || s.name : "Strategy"}
                </div>
                <div className="meta-value">
                  {strategyEntries.length === 1 ? (s.strategy || s.name || "\u2014") : ""}
                  {s.dry_run && <span className="dry-run-tag">DRY RUN</span>}
                  {s.pnl != null && (
                    <span className={s.pnl >= 0 ? "up" : "down"} style={{ marginLeft: 8 }}>
                      PnL: ${s.pnl.toFixed(2)}
                    </span>
                  )}
                  {s.final_cash != null && (
                    <span className="subtitle" style={{ marginLeft: 8 }}>
                      Cash: ${s.final_cash.toFixed(2)}
                    </span>
                  )}
                </div>
              </div>
            ))}
            {allTrades.length > 0 && (
              <div className="meta-card">
                <div className="meta-label">Total Trades</div>
                <div className="meta-value">{allTrades.length}</div>
              </div>
            )}
          </div>
        </div>
      )}

      <MetaGrid session={session} />
      <SessionCharts
        session={session}
        trades={allTrades.length > 0 ? allTrades : undefined}
        isDryRun={anyDryRun}
      />
      <TradeTable trades={allTrades} />
    </div>
  );
}
