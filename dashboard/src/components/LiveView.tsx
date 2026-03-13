import { useEffect, useRef, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  fetchLiveSession, archiveSession, fetchSessionList, fetchMarketPnl,
  fetchStrategies, fetchSlugs, tradeBotOn, tradeBotOff, reloadBot, setAutoExit, startMarket,
} from "../api";
import type { MarketPnl, SlugInfo } from "../api";
import type { BotStatus, LivePayload, MarketStatus } from "../types";
import { isLiveStatus, isSessionData } from "../types";
import { PnlCard } from "./PnlCard";
import { SessionCharts } from "./SessionCharts";
import { TradeTable } from "./TradeTable";
import { StrategyTable } from "./StrategyTable";
import type { StrategyRow } from "./StrategyTable";
import { fmtTimeOnly } from "../utils/formatTime";

interface Props {
  liveData: Record<string, LivePayload>;
  botStatus: BotStatus | null;
  onRefresh: () => void;
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

export function LiveView({ liveData, botStatus, onRefresh }: Props) {
  const { prefix } = useParams<{ prefix: string }>();
  const navigate = useNavigate();
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
  const currentPending: any[] = [];
  if (trading?.strategies && Array.isArray(trading.strategies)) {
    for (const s of trading.strategies) {
      if (Array.isArray(s.trades)) {
        for (const t of s.trades) {
          currentTrades.push({ ...t, _strategy: s.name });
        }
      }
      if (Array.isArray(s.pending_orders)) {
        for (const o of s.pending_orders) {
          currentPending.push({ ...o, _strategy: s.name });
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
        navigate(`/sessions/${prefix}`);
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
        <h2 className="text-xl font-semibold">Live: {prefix}</h2>
        <a
          href={
            payload && isSessionData(payload) && payload.metadata?.event_slug
              ? `https://polymarket.com/event/${payload.metadata.event_slug}`
              : `https://polymarket.com/event/${prefix}`
          }
          target="_blank"
          rel="noopener noreferrer"
          className="text-dim hover:text-accent transition-colors"
          title="Open on Polymarket"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
        </a>
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

      {prefix && (
        <StrategyManager
          prefix={prefix}
          market={botStatus?.markets?.find((m) => m.slug_key === prefix) ?? null}
          onRefresh={onRefresh}
          appRunning={botStatus?.running ?? false}
        />
      )}

      {pnl && (pnl.overall !== 0 || pnl.last_24h !== 0 || pnl.last_1h !== 0) && (
        <PnlCard overall={pnl.overall} last24h={pnl.last_24h} last1h={pnl.last_1h ?? 0} dryRun={pnl.dry_run} />
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
        <TradeTable trades={allTrades} pendingOrders={currentPending} />
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

function StrategyManager({
  prefix,
  market,
  onRefresh,
  appRunning,
}: {
  prefix: string;
  market: MarketStatus | null;
  onRefresh: () => void;
  appRunning: boolean;
}) {
  const [strategies, setStrategies] = useState<string[]>([]);
  const [slugs, setSlugs] = useState<SlugInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [selected, setSelected] = useState("");
  const [cash, setCash] = useState("10");
  const [sessionCap, setSessionCap] = useState("25%:3");

  useEffect(() => {
    let cancelled = false;
    function load() {
      fetchStrategies().then((s) => {
        if (cancelled) return;
        if (s.length > 0) setStrategies(s);
        else setTimeout(load, 3000);
      });
    }
    load();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetchSlugs().then((s) => { if (!cancelled) setSlugs(s); });
    return () => { cancelled = true; };
  }, []);

  function flashMessage(msg: string) {
    setMessage(msg);
    setTimeout(() => setMessage(null), 5000);
  }

  const activeStrategies = market?.strategies ?? [];
  const activeNames = new Set(activeStrategies.map((s) => s.name));
  const available = strategies.filter((s) => !activeNames.has(s));

  const slugInfo = slugs.find((s) => s.slug_prefix.replace(/-$/, "") === prefix);
  const slugPrefix = slugInfo?.slug_prefix ?? prefix + "-";
  const windowSeconds = slugInfo?.window_duration_seconds ?? 300;

  async function handleTradeOn(strategyName: string, opts: { cash: number; sessionCap?: number; sessionCashRatio?: { ratio: number; max: number }; dryRun: boolean }) {
    setLoading(true);
    if (!market) {
      const mrRes = await startMarket({ slug_prefix: slugPrefix, window: windowSeconds });
      if (!mrRes.ok) {
        flashMessage(mrRes.error || "Failed to register market");
        setLoading(false);
        return;
      }
    }
    const body: any = {
      slug_prefix: slugPrefix,
      strategy: strategyName,
      cash: opts.cash,
      dry_run: opts.dryRun,
    };
    if (opts.sessionCashRatio != null) body.session_cash_ratio = opts.sessionCashRatio;
    else if (opts.sessionCap != null) body.session_cap = opts.sessionCap;
    const res = await tradeBotOn(body);
    flashMessage(res.ok ? `Started: ${strategyName}` : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleTradeOff(strategyName?: string) {
    setLoading(true);
    const res = await tradeBotOff(slugPrefix, strategyName);
    flashMessage(res.ok ? (strategyName ? `Stopped: ${strategyName}` : "All strategies stopped") : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleReload(strategyName: string) {
    setLoading(true);
    const res = await reloadBot(slugPrefix, strategyName);
    flashMessage(res.ok ? `Reloaded: ${strategyName}` : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleToggleAutoExit(strategyName: string, enabled: boolean) {
    const res = await setAutoExit(slugPrefix, enabled, strategyName);
    if (!res.ok) flashMessage(res.error || "Failed");
    onRefresh();
  }

  if (!appRunning) {
    return (
      <div className="mb-6 p-3 bg-bg rounded-md border border-dashed border-border text-[13px] text-dim">
        Enable trading to add strategies to this market.
      </div>
    );
  }

  if (activeStrategies.length === 0 && available.length === 0) return null;

  return (
    <div className="mb-6">
      {activeStrategies.length > 0 && (
        <div className="flex flex-col gap-2 mb-3">
          {activeStrategies.map((s) => {
            const alive = s.alive !== false;
            return (
              <div key={s.name} className="flex items-center gap-3 px-3.5 py-2.5 bg-card rounded-md border border-border">
                <span className={`w-2 h-2 rounded-full shrink-0 ${alive ? "bg-up" : "bg-down"}`} />
                <span className="font-semibold text-sm min-w-[120px]">{s.name}</span>
                {s.dry_run && <span className="inline-block px-2 py-0.5 rounded text-[11px] font-semibold bg-accent/15 text-accent">DRY</span>}
                {!alive && <span className="px-1.5 py-px rounded text-[10px] font-semibold bg-down/15 text-down">DEAD</span>}
                <span className="text-dim text-[13px] ml-auto font-mono tabular-nums">
                  ${(s.bankroll ?? s.cash).toFixed(2)}
                  {s.session_cash_ratio != null && (
                    <span className="opacity-60"> ({Math.round(s.session_cash_ratio.ratio * 100)}% up to ${s.session_cash_ratio.max.toFixed(0)})</span>
                  )}
                  {s.session_cash_ratio == null && s.session_cap != null && (
                    <span className="opacity-60"> (${s.session_cap.toFixed(0)}/s)</span>
                  )}
                </span>
                <label className="flex items-center gap-1.5 text-[11px] cursor-pointer text-dim">
                  <input
                    type="checkbox"
                    checked={!!s.auto_exit}
                    onChange={(e) => handleToggleAutoExit(s.name, e.target.checked)}
                    className="w-[13px] h-[13px] accent-accent"
                  />
                  Auto-exit
                </label>
                {s.pid != null && <span className="text-dim text-[11px]">PID {s.pid}</span>}
                <div className="flex gap-1.5">
                  <button className="btn-accent btn-sm" onClick={() => handleReload(s.name)} disabled={loading}>Reload</button>
                  <button className="btn-danger btn-sm" onClick={() => handleTradeOff(s.name)} disabled={loading}>Kill</button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {available.length > 0 && (
        <div className="p-3 bg-bg rounded-md border border-dashed border-border mb-3">
          <div className="flex items-end gap-2.5">
            <label className="flex flex-col gap-1 flex-1 min-w-0">
              <span className="text-[10px] font-semibold uppercase tracking-tight text-dim">Strategy</span>
              <select className="!text-[13px] !py-1.5 !px-2.5 w-full" value={selected} onChange={(e) => setSelected(e.target.value)}>
                <option value="">Select...</option>
                {available.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-tight text-dim">Cash</span>
              <input type="number" value={cash} onChange={(e) => setCash(e.target.value)} min="1" step="1" className="text-[13px] w-20" />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-tight text-dim">Session limit</span>
              <input type="text" value={sessionCap} onChange={(e) => setSessionCap(e.target.value)} placeholder="5 or 25%:3" className="text-[13px] w-24" />
            </label>
            <button
              className="btn-primary btn-sm shrink-0"
              disabled={loading || !selected}
              onClick={() => {
                if (!selected) return;
                const capStr = sessionCap.trim();
                let sessionCapVal: number | undefined;
                let sessionCashRatioVal: { ratio: number; max: number } | undefined;
                if (capStr && capStr.includes("%")) {
                  const parts = capStr.replace("%", "").split(":");
                  sessionCashRatioVal = { ratio: parseFloat(parts[0]) / 100, max: parseFloat(parts[1]) };
                } else if (capStr) {
                  sessionCapVal = parseFloat(capStr);
                }
                handleTradeOn(selected, {
                  cash: parseFloat(cash) || 10,
                  sessionCap: sessionCapVal,
                  sessionCashRatio: sessionCashRatioVal,
                  dryRun: false,
                });
                setSelected("");
              }}
            >
              Add
            </button>
          </div>
        </div>
      )}

      {message && (
        <div className="mb-3 px-4 py-2.5 bg-accent/10 border border-accent/20 rounded-md text-[13px] text-accent">
          {message}
        </div>
      )}
    </div>
  );
}
