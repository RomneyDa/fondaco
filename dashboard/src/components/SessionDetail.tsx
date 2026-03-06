import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchSession, fetchSessionTrading } from "../api";
import type { BotTrade, SessionData, TradingInfo } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";

export function SessionDetail() {
  const { prefix, file } = useParams<{ prefix: string; file: string }>();
  const [session, setSession] = useState<SessionData | null>(null);
  const [trading, setTrading] = useState<any | null>(null);

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

  return (
    <div>
      <h2>{session.metadata?.event_title || file?.replace(".json", "")}</h2>

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
    </div>
  );
}
