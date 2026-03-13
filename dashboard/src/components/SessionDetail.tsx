import { useEffect, useState, useMemo } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import { fetchSession, fetchSessionTrading, fetchSessionStats, archiveSession } from "../api";
import type { SessionStat } from "../api";
import type { BotTrade, SessionData, PriceSample } from "../types";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";
import { StrategyTable } from "./StrategyTable";
import type { StrategyRow } from "./StrategyTable";

/** Compute derived stats from price samples */
function computeSampleStats(samples: PriceSample[]) {
  if (samples.length === 0) return null;

  const btcPrices = samples.map((s) => s.btc_price_usd).filter((v): v is number => v != null);
  const upMids = samples.map((s) => s.up_token_midpoint).filter((v): v is number => v != null);
  const downMids = samples.map((s) => s.down_token_midpoint).filter((v): v is number => v != null);

  // Spread calculations
  const upSpreads: number[] = [];
  const downSpreads: number[] = [];
  for (const s of samples) {
    if (s.up_token_best_ask != null && s.up_token_best_bid != null) {
      upSpreads.push(s.up_token_best_ask - s.up_token_best_bid);
    }
    if (s.down_token_best_ask != null && s.down_token_best_bid != null) {
      downSpreads.push(s.down_token_best_ask - s.down_token_best_bid);
    }
  }

  const avg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : null;

  // Tick rate
  const totalElapsed = samples[samples.length - 1].elapsed_seconds - samples[0].elapsed_seconds;
  const tickRate = totalElapsed > 0 ? samples.length / totalElapsed : null;

  return {
    btcHigh: btcPrices.length > 0 ? Math.max(...btcPrices) : null,
    btcLow: btcPrices.length > 0 ? Math.min(...btcPrices) : null,
    btcRange: btcPrices.length >= 2 ? Math.max(...btcPrices) - Math.min(...btcPrices) : null,
    upMidFinal: upMids.length > 0 ? upMids[upMids.length - 1] : null,
    downMidFinal: downMids.length > 0 ? downMids[downMids.length - 1] : null,
    avgUpSpread: avg(upSpreads),
    avgDownSpread: avg(downSpreads),
    tickRate,
    totalElapsed,
    sampleCount: samples.length,
  };
}


function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex justify-between items-center py-1.5">
      <span className="text-[12px] text-dim">{label}</span>
      <span className={`text-[12px] ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

export function SessionDetail() {
  const { prefix, file } = useParams<{ prefix: string; file: string }>();
  const navigate = useNavigate();
  const [session, setSession] = useState<SessionData | null>(null);
  const [trading, setTrading] = useState<any | null>(null);
  const [archiving, setArchiving] = useState(false);
  const [stat, setStat] = useState<SessionStat | null>(null);
  const [showMeta, setShowMeta] = useState(false);

  useEffect(() => {
    if (prefix && file) {
      fetchSession(prefix, file).then(setSession);
      fetchSessionTrading(prefix, file).then(setTrading);
    }
  }, [prefix, file]);

  useEffect(() => {
    if (prefix && session?.metadata?.window_start_utc) {
      fetchSessionStats(prefix, { session: session.metadata.window_start_utc }).then((stats) => {
        if (stats.length > 0) setStat(stats[0]);
      });
    }
  }, [prefix, session?.metadata?.window_start_utc]);

  const sampleStats = useMemo(
    () => (session ? computeSampleStats(session.samples || []) : null),
    [session]
  );

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
  const realPnl = strategyEntries.filter((s) => !s.dry_run).reduce((sum: number, s: any) => sum + (s.pnl ?? 0), 0);
  const dryPnl = strategyEntries.filter((s) => s.dry_run).reduce((sum: number, s: any) => sum + (s.pnl ?? 0), 0);
  const sessionPnl = realPnl;
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
  const btcChangePct = initBtc != null && finalBtc != null && initBtc > 0
    ? ((finalBtc - initBtc) / initBtc) * 100
    : null;

  const meta = session.metadata;

  // Total fees across all strategies
  const totalFees = allTrades.reduce((sum, t) => sum + (t.fee ?? 0), 0);

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
      <div className="flex items-start justify-between gap-4 mb-2">
        <div className="flex-1 min-w-0">
          <h2 className="text-[22px] font-bold mb-2 leading-snug">
            {meta?.event_title || file?.replace(".json", "")}
          </h2>
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`inline-block px-3 py-0.5 rounded-xl text-xs font-bold uppercase tracking-wide ${
              outcomeClass === "up" ? "bg-up/15 text-up" :
              outcomeClass === "down" ? "bg-down/15 text-down" :
              "bg-dim/15 text-dim"
            }`}>
              {outcomeLabel}{btcChange != null ? ` $${Math.abs(btcChange).toFixed(2)}` : ""}
            </span>
            {btcChange != null && (
              <span className="text-[13px] font-mono tabular-nums text-dim opacity-60">
                ${initBtc!.toLocaleString(undefined, { maximumFractionDigits: 2 })} → ${finalBtc!.toLocaleString(undefined, { maximumFractionDigits: 2 })}
              </span>
            )}
            {anyDryRun && <span className="inline-block px-2 py-0.5 rounded text-[11px] font-semibold bg-accent/15 text-accent">DRY RUN</span>}
          </div>
          {(session.error_count ?? 0) > 0 && (
            <div className="mt-2 text-[12px] text-down opacity-80">{session.error_count} error{session.error_count !== 1 ? "s" : ""}</div>
          )}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {hasPnl && (
            <div className={`flex flex-col items-end px-4 py-2 rounded-[10px] ${
              sessionPnl > 0 ? "bg-up/10" : sessionPnl < 0 ? "bg-down/10" : ""
            }`}>
              <span className="text-[11px] font-semibold uppercase tracking-wide text-dim">PnL</span>
              <span className={`text-[22px] font-bold tabular-nums ${
                sessionPnl > 0 ? "text-up" : sessionPnl < 0 ? "text-down" : ""
              }`}>
                {sessionPnl >= 0 ? "+$" : "-$"}{Math.abs(sessionPnl).toFixed(2)}
              </span>
              {anyDryRun && dryPnl !== 0 && (
                <span className="text-[12px] tabular-nums text-accent opacity-70 mt-0.5">
                  {dryPnl >= 0 ? "+" : ""}{dryPnl.toFixed(2)} dry
                </span>
              )}
            </div>
          )}
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

      {/* Stats */}
      <div className="flex items-center gap-4 mb-6 text-[13px]">
        {sampleStats?.btcHigh != null && sampleStats?.btcLow != null && (
          <span className="font-mono tabular-nums text-dim">
            Range ${sampleStats.btcRange!.toFixed(2)}
            <span className="opacity-60 ml-1">
              (${sampleStats.btcLow.toLocaleString(undefined, { maximumFractionDigits: 0 })} — ${sampleStats.btcHigh.toLocaleString(undefined, { maximumFractionDigits: 0 })})
            </span>
          </span>
        )}
        {stat && (
          <span className={`font-mono tabular-nums ${stat.return_bps > 0 ? "text-up" : stat.return_bps < 0 ? "text-down" : "text-dim"}`}>
            {stat.return_bps >= 0 ? "+" : ""}{(stat.return_bps / 100).toFixed(2)}%
          </span>
        )}
        {allTrades.length > 0 && (
          <span className="text-dim">
            {allTrades.length} trade{allTrades.length !== 1 ? "s" : ""}
            {totalFees > 0 && <span className="opacity-60 ml-1">(${totalFees.toFixed(2)} fees)</span>}
          </span>
        )}
      </div>

      {/* Chart */}
      <div className="mb-6">
        <SessionCharts
          session={session}
          trades={allTrades.length > 0 ? allTrades : undefined}
          isDryRun={anyDryRun}
          tickRate={sampleStats?.tickRate}
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

      {/* Strategy Params */}
      {strategyEntries.some((s: any) => s.params && Object.keys(s.params).length > 0) && (
        <div className="mb-6 bg-card border border-border rounded-lg overflow-hidden">
          <div className="px-4 py-3.5 border-b border-border">
            <h3 className="text-sm font-semibold">Strategy Parameters</h3>
          </div>
          <div className="p-4 grid gap-4" style={{ gridTemplateColumns: `repeat(${Math.min(strategyEntries.length, 3)}, 1fr)` }}>
            {strategyEntries.filter((s: any) => s.params && Object.keys(s.params).length > 0).map((s: any) => (
              <div key={s.strategy || s.name}>
                {strategyEntries.length > 1 && (
                  <div className="text-[12px] font-semibold text-accent mb-2">{s.strategy || s.name}</div>
                )}
                <div className="space-y-0.5">
                  {Object.entries(s.params).map(([key, val]) => (
                    <div key={key} className="flex justify-between items-center py-1">
                      <span className="text-[12px] text-dim">{key}</span>
                      <span className="text-[12px] font-mono tabular-nums">
                        {typeof val === "number"
                          ? Number.isInteger(val) ? String(val) : (val as number).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
                          : val === null ? "null"
                          : val === true ? "true"
                          : val === false ? "false"
                          : String(val)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trades */}
      <div className="mb-6">
        <TradeTable trades={allTrades} />
      </div>

      {/* Errors */}
      {session.errors && session.errors.length > 0 && (
        <div className="mb-6 bg-card border border-border rounded-lg overflow-hidden">
          <div className="px-4 py-3.5 border-b border-border flex justify-between items-center">
            <h3 className="text-sm font-semibold text-down">Errors ({session.errors.length})</h3>
          </div>
          <div className="max-h-[200px] overflow-y-auto">
            {session.errors.map((err, i) => (
              <div key={i} className="px-4 py-2 border-b border-border last:border-b-0 text-[12px] font-mono text-dim">
                {err}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Metadata (collapsible) */}
      <div className="mb-6 bg-card border border-border rounded-lg overflow-hidden">
        <button
          className="w-full px-4 py-3 flex justify-between items-center text-left !rounded-none !bg-transparent border-none"
          onClick={() => setShowMeta(!showMeta)}
        >
          <span className="text-sm font-semibold text-dim">Session Metadata</span>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className={`text-dim transition-transform ${showMeta ? "rotate-180" : ""}`}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
        {showMeta && (
          <div className="px-4 pb-3 border-t border-border pt-3">
            <div className="grid grid-cols-2 gap-x-8 gap-y-0">
              <InfoRow label="Event ID" value={meta?.event_id || "—"} mono />
              <InfoRow label="Event Slug" value={meta?.event_slug || "—"} mono />
              <InfoRow label="Condition ID" value={meta?.condition_id ? `${meta.condition_id.slice(0, 10)}...${meta.condition_id.slice(-6)}` : "—"} mono />
              <InfoRow label="Resolution Source" value={meta?.resolution_source || "—"} />
              <InfoRow label="Up Token ID" value={meta?.up_token_id ? `${meta.up_token_id.slice(0, 10)}...${meta.up_token_id.slice(-6)}` : "—"} mono />
              <InfoRow label="Down Token ID" value={meta?.down_token_id ? `${meta.down_token_id.slice(0, 10)}...${meta.down_token_id.slice(-6)}` : "—"} mono />
              <InfoRow label="Window Start" value={meta?.window_start_utc || "—"} mono />
              <InfoRow label="Window End" value={meta?.window_end_utc || "—"} mono />
              <InfoRow label="Collection Start" value={session.collection_started_utc || "—"} mono />
              <InfoRow label="Collection End" value={session.collection_ended_utc || "—"} mono />
              {session.outcome_prices && session.outcome_prices.length > 0 && (
                <InfoRow label="Outcome Prices" value={session.outcome_prices.join(", ")} mono />
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
