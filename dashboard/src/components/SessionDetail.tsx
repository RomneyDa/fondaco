import { useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { fetchSession, fetchSessionTrading, archiveSession } from "../api";
import type { BotTrade, SessionData } from "../types";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";
import { StrategyTable } from "./StrategyTable";
import type { StrategyRow } from "./StrategyTable";

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

  if (!session) return <p className="text-dim text-sm">Loading...</p>;

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
  const sessionPnl = strategyEntries.reduce((sum: number, s: any) => sum + (s.pnl ?? 0), 0);
  const hasPnl = strategyEntries.some((s: any) => s.pnl != null);

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

  const outcomeLabel = session.outcome || "Pending";
  const outcomeClass = (session.outcome || "").toLowerCase();
  const initBtc = session.initial_btc_price_usd;
  const finalBtc = session.final_btc_price_usd;
  const btcChange = initBtc != null && finalBtc != null ? finalBtc - initBtc : null;

  return (
    <div className="max-w-[1200px]">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1.5 text-[13px] mb-4">
        <Link to={`/sessions/${prefix}`} className="text-accent no-underline font-medium hover:underline">
          {prefix}
        </Link>
        <span className="text-dim opacity-50">/</span>
        <span className="text-dim font-mono text-xs">{file?.replace(".json", "")}</span>
      </div>

      {/* Header */}
      <div className="flex items-start justify-between gap-4 mb-6 pb-5 border-b border-border">
        <div className="flex-1 min-w-0">
          <h2 className="text-[22px] font-bold mb-2 leading-snug">
            {session.metadata?.event_title || file?.replace(".json", "")}
          </h2>
          <div className="flex items-center gap-2">
            <span className={`inline-block px-3 py-0.5 rounded-xl text-xs font-bold uppercase tracking-wide ${
              outcomeClass === "up" ? "bg-up/15 text-up" :
              outcomeClass === "down" ? "bg-down/15 text-down" :
              "bg-dim/15 text-dim"
            }`}>
              {outcomeLabel}
              {btcChange != null && (
                <span> ${Math.abs(btcChange).toFixed(2)}</span>
              )}
            </span>
            {initBtc != null && finalBtc != null && (
              <span className="text-[13px] text-dim font-mono tabular-nums">
                ${initBtc.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                {" → "}
                ${finalBtc.toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
            )}
            {anyDryRun && <span className="inline-block px-2 py-0.5 rounded text-[11px] font-semibold bg-accent/15 text-accent ml-2">DRY RUN</span>}
            {(session.error_count ?? 0) > 0 && (
              <span className="text-xs text-dim opacity-50 ml-auto">
                <span className="text-down opacity-80 ml-1.5">{session.error_count} err</span>
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className={`flex flex-col items-end px-4 py-2 rounded-[10px] ${
            sessionPnl > 0 ? "bg-up/10" : sessionPnl < 0 ? "bg-down/10" : ""
          }`}>
            <span className="text-[11px] font-semibold uppercase tracking-wide text-dim">PnL</span>
            <span className={`text-[22px] font-bold tabular-nums ${
              sessionPnl > 0 ? "text-up" : sessionPnl < 0 ? "text-down" : ""
            }`}>
              {sessionPnl >= 0 ? "+$" : "-$"}{Math.abs(sessionPnl).toFixed(2)}
            </span>
          </div>
          <button
            className="!p-2 bg-card border border-border text-dim inline-flex items-center justify-center leading-none hover:border-down hover:text-down"
            onClick={handleArchive}
            disabled={archiving}
            title="Archive"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      </div>

      {/* Chart */}
      <div className="mb-6">
        <SessionCharts
          session={session}
          trades={allTrades.length > 0 ? allTrades : undefined}
          isDryRun={anyDryRun}
        />
      </div>

      {/* Strategy performance table */}
      {strategyEntries.length > 0 && (() => {
        const rows: StrategyRow[] = strategyEntries.map((s: any) => {
          const tradeList = Array.isArray(s.trades) ? s.trades : [];
          const trades = tradeList.length;
          const fees = tradeList.reduce((sum: number, t: any) => sum + (t.fee ?? 0), 0);
          return {
            name: s.strategy || s.name || "Strategy",
            dry_run: s.dry_run,
            trades,
            fees,
            up: 0,
            down: 0,
            posValue: 0,
            pnl: s.pnl ?? 0,
            hasPnl: s.pnl != null,
            finalCash: s.final_cash ?? null,
          };
        });
        return (
          <div className="mb-6">
            <StrategyTable rows={rows} showFinalCash />
          </div>
        );
      })()}

      {/* Trades */}
      {allTrades.length > 0 && (
        <div className="mb-6">
          <TradeTable trades={allTrades} />
        </div>
      )}
    </div>
  );
}
