import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchSession, fetchSessionTrading } from "../api";
import type { SessionData, TradingInfo } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";

export function SessionDetail() {
  const { prefix, file } = useParams<{ prefix: string; file: string }>();
  const [session, setSession] = useState<SessionData | null>(null);
  const [trading, setTrading] = useState<TradingInfo | null>(null);

  useEffect(() => {
    if (prefix && file) {
      fetchSession(prefix, file).then(setSession);
      fetchSessionTrading(prefix, file).then(setTrading);
    }
  }, [prefix, file]);

  if (!session) return <p className="subtitle">Loading...</p>;

  return (
    <div>
      <h2>{session.metadata?.event_title || file?.replace(".json", "")}</h2>

      {trading && (
        <div className="trading-summary">
          <div className="meta-grid">
            <div className="meta-card">
              <div className="meta-label">Strategy</div>
              <div className="meta-value">
                {trading.strategy}
                {trading.dry_run && (
                  <span className="dry-run-tag">DRY RUN</span>
                )}
              </div>
            </div>
            {trading.trades && trading.trades.length > 0 && (
              <div className="meta-card">
                <div className="meta-label">Trades</div>
                <div className="meta-value">{trading.trades.length}</div>
              </div>
            )}
            {"pnl" in trading && (
              <div className="meta-card">
                <div className="meta-label">PnL</div>
                <div
                  className={`meta-value ${
                    (trading as any).pnl >= 0 ? "up" : "down"
                  }`}
                >
                  ${((trading as any).pnl as number).toFixed(2)}
                </div>
              </div>
            )}
            {"final_cash" in trading && (
              <div className="meta-card">
                <div className="meta-label">Final Cash</div>
                <div className="meta-value">
                  ${((trading as any).final_cash as number).toFixed(2)}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      <MetaGrid session={session} />
      <SessionCharts
        session={session}
        trades={trading?.trades}
        isDryRun={trading?.dry_run}
      />
    </div>
  );
}
