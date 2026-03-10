import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchLiveSession, archiveSession, fetchSessionList, fetchMarketPnl } from "../api";
import type { MarketPnl } from "../api";
import type { LivePayload } from "../types";
import { isLiveStatus, isSessionData } from "../types";
import { PnlCard } from "./PnlCard";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";
import { StrategyTable } from "./StrategyTable";
import type { StrategyRow } from "./StrategyTable";
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
  const [pnl, setPnl] = useState<MarketPnl | null>(null);

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

  useEffect(() => {
    if (!prefix) return;
    let cancelled = false;
    const load = () => {
      fetchMarketPnl(prefix).then((data) => {
        if (!cancelled && data) setPnl(data);
      });
    };
    load();
    const id = setInterval(load, 30_000);
    return () => { cancelled = true; clearInterval(id); };
  }, [prefix]);

  useEffect(() => {
    if (prefix && liveData[prefix]) {
      setPayload(liveData[prefix]);
      setHadSSE(true);
      setEnded(false);
    }
  }, [prefix, liveData]);

  useEffect(() => {
    if (prefix && hadSSE && payload && !liveData[prefix]) {
      setEnded(true);
    }
  }, [prefix, hadSSE, payload, liveData]);

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

  const badgeClass = ended
    ? "bg-dim/15 text-dim"
    : payload && isSessionData(payload)
      ? "bg-up/15 text-up"
      : "bg-accent/15 text-accent";

  const trading =
    payload && "trading" in payload ? (payload as any).trading : undefined;

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

  const isStatus = payload && isLiveStatus(payload);
  if (isStatus) {
    stickyTradesRef.current = [];
  } else if (currentTrades.length >= stickyTradesRef.current.length) {
    currentTrades.sort((a: any, b: any) => (a.elapsed_seconds ?? 0) - (b.elapsed_seconds ?? 0));
    stickyTradesRef.current = currentTrades;
  } else if (currentTrades.length === 0 && payload && isSessionData(payload)) {
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
      const latest = sessions[0];
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
      <div className="flex items-center gap-3 mb-6">
        <h2 className="text-xl font-semibold">
          Live: {prefix}
          <a
            href={
              payload && isSessionData(payload) && payload.metadata?.event_slug
                ? `https://polymarket.com/event/${payload.metadata.event_slug}`
                : `https://polymarket.com/event/${prefix}`
            }
            target="_blank"
            rel="noopener noreferrer"
            className="ml-2 text-dim align-middle"
            title="Open on Polymarket"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
        </h2>
        <span className={`inline-block px-3 py-1 rounded-xl text-xs font-semibold ${badgeClass}`}>
          {statusLabel}
        </span>
        <div className="flex-1" />
        {(ended || (payload && isSessionData(payload))) && !archived && (
          <button
            className="!p-2 bg-card border border-border text-dim inline-flex items-center justify-center leading-none hover:border-down hover:text-down"
            onClick={handleArchive}
            disabled={archiving}
            title="Archive"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
          </button>
        )}
        {archived && <span className="text-dim text-sm">Archived</span>}
      </div>

      {pnl && (pnl.overall !== 0 || pnl.last_24h !== 0 || pnl.last_1h !== 0) && (
        <PnlCard overall={pnl.overall} last24h={pnl.last_24h} last1h={pnl.last_1h ?? 0} />
      )}

      {payload && isSessionData(payload) && payload.samples?.length > 0 && (() => {
        const s = payload.samples[payload.samples.length - 1];
        const upMid = s.up_token_midpoint;
        const downMid = s.down_token_midpoint;
        const initBtc = payload.initial_btc_price_usd;
        const curBtc = s.btc_price_usd;
        const btcChange = initBtc != null && curBtc != null ? curBtc - initBtc : null;
        return (
          <div className="flex gap-2.5 mb-4">
            {upMid != null && (
              <span className="text-[13px] font-semibold tabular-nums font-mono px-3 py-1 rounded-md bg-up/[0.12] text-up">Up {upMid.toFixed(2)}</span>
            )}
            {downMid != null && (
              <span className="text-[13px] font-semibold tabular-nums font-mono px-3 py-1 rounded-md bg-down/[0.12] text-down">Down {downMid.toFixed(2)}</span>
            )}
            {btcChange != null && (
              <span className={`text-[13px] font-semibold tabular-nums font-mono px-3 py-1 rounded-md ${btcChange >= 0 ? "bg-up/[0.12] text-up" : "bg-down/[0.12] text-down"}`}>
                BTC {btcChange >= 0 ? "+" : ""}{btcChange.toFixed(2)}
              </span>
            )}
            {(payload.error_count ?? 0) > 0 && (
              <span className="ml-auto text-xs text-dim opacity-60">
                <span className="text-down opacity-80 ml-1.5">{payload.error_count} err</span>
              </span>
            )}
          </div>
        );
      })()}

      {payload && isSessionData(payload) && (
        <SessionCharts
          session={payload}
          trades={allTrades.length > 0 ? allTrades : undefined}
          isDryRun={trading?.dry_run}
        />
      )}

      {trading && (() => {
        const strategies: any[] = trading.strategies && trading.strategies.length > 0
          ? trading.strategies
          : trading.strategy
            ? [{ name: trading.strategy, dry_run: trading.dry_run, cash: trading.cash, trades: trading.trades }]
            : [];
        if (strategies.length === 0) return null;

        const lastSample = payload && isSessionData(payload)
          ? payload.samples?.[payload.samples.length - 1]
          : null;
        const upMid = lastSample?.up_token_midpoint ?? 0.5;
        const downMid = lastSample?.down_token_midpoint ?? 0.5;
        const hasSession = lastSample != null;

        const rows: StrategyRow[] = strategies.map((s: any) => {
          const tradeList = Array.isArray(s.trades) ? s.trades : [];
          const trades = tradeList.length || (s.trades ?? 0);
          const fees = tradeList.reduce((sum: number, t: any) => sum + (t.fee ?? 0), 0);
          const up = s.up_tokens ?? 0;
          const down = s.down_tokens ?? 0;
          const posValue = up * upMid + down * downMid;
          const currentCash = s.cash ?? 0;
          const startCash = s.starting_cash ?? s.bankroll ?? currentCash;
          const sessionPnl = hasSession && trades > 0 ? (currentCash + posValue - startCash) : 0;
          return { name: s.name, dry_run: s.dry_run, bankroll: s.bankroll ?? s.cash, session_cash_ratio: s.session_cash_ratio, session_cap: s.session_cap, trades, fees, up, down, posValue, pnl: sessionPnl, hasPnl: hasSession && trades > 0 };
        });

        return <StrategyTable rows={rows} showBankroll showPosition />;
      })()}

      {payload && isSessionData(payload) && (
        <TradeTable trades={allTrades} />
      )}

      {!(payload && isSessionData(payload)) && (
        <div className="flex flex-col items-center justify-center py-16 px-6 text-center">
          <div className="mb-4 opacity-80 animate-pulse-dot">
            {payload && isLiveStatus(payload) && payload.status === "waiting" ? (
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-accent)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
            ) : payload && isLiveStatus(payload) && payload.status === "resolving" ? (
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-btc)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 2v4m0 12v4m-7.07-3.93l2.83-2.83m8.48-8.48l2.83-2.83M2 12h4m12 0h4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83"/></svg>
            ) : (
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--color-dim)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            )}
          </div>
          <div>
            {payload && isLiveStatus(payload) ? (
              payload.status === "waiting" ? (
                <>
                  <div className="text-lg font-semibold text-txt mb-1">Waiting for next window</div>
                  {payload.next_window_utc && (
                    <div className="text-[40px] font-bold tabular-nums font-mono text-accent my-2">{formatCountdown(payload.next_window_utc)}</div>
                  )}
                  <div className="text-[13px] text-dim">{fmtTimeOnly(payload.next_window_utc)}</div>
                </>
              ) : (
                <>
                  <div className="text-lg font-semibold text-txt mb-1">Resolving</div>
                  <div className="text-[13px] text-dim">Waiting for market resolution...</div>
                </>
              )
            ) : (
              <div className="text-lg font-semibold text-txt mb-1">Connecting...</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
