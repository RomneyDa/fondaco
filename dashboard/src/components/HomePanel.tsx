"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { trpc } from "@/trpc/client";
import type { BotStatus, MarketStatus, StrategySlotStatus } from "@/types";

interface MarketView {
  slugPrefix: string;
  slugKey: string;
  asset: string;
  windowSeconds: number;
  isCollecting: boolean;
  isRegistered: boolean;
  marketStatus: MarketStatus | null;
  strategies: StrategySlotStatus[];
}

function toSlugKey(slugPrefix: string): string {
  return slugPrefix.replace(/-$/, "");
}

function buildMarketList(
  slugs: Array<{ slug_prefix: string; window_duration_seconds: number; asset: string }>,
  livePrefixes: string[],
  registeredMarkets: MarketStatus[]
): MarketView[] {
  const liveSet = new Set(livePrefixes);
  const registeredMap = new Map<string, MarketStatus>();
  for (const m of registeredMarkets) registeredMap.set(m.slug_key, m);

  const seen = new Set<string>();
  const result: MarketView[] = [];

  for (const s of slugs) {
    const key = toSlugKey(s.slug_prefix);
    seen.add(key);
    const ms = registeredMap.get(key) ?? null;
    result.push({
      slugPrefix: s.slug_prefix,
      slugKey: key,
      asset: s.asset,
      windowSeconds: s.window_duration_seconds,
      isCollecting: liveSet.has(key),
      isRegistered: ms != null,
      marketStatus: ms,
      strategies: ms?.strategies ?? [],
    });
  }

  for (const m of registeredMarkets) {
    if (!seen.has(m.slug_key)) {
      seen.add(m.slug_key);
      result.push({
        slugPrefix: m.slug_prefix,
        slugKey: m.slug_key,
        asset: m.slug_key.split("-")[0].toUpperCase(),
        windowSeconds: 300,
        isCollecting: liveSet.has(m.slug_key),
        isRegistered: true,
        marketStatus: m,
        strategies: m.strategies ?? [],
      });
    }
  }
  for (const p of livePrefixes) {
    if (!seen.has(p)) {
      seen.add(p);
      const parts = p.match(/^(\w+)-updown-(\d+)m$/);
      result.push({
        slugPrefix: p + "-",
        slugKey: p,
        asset: parts ? parts[1].toUpperCase() : p,
        windowSeconds: parts ? parseInt(parts[2]) * 60 : 300,
        isCollecting: true,
        isRegistered: false,
        marketStatus: null,
        strategies: [],
      });
    }
  }

  result.sort((a, b) => {
    const score = (m: MarketView) =>
      m.isCollecting && m.isRegistered ? 0 : m.isCollecting ? 1 : 2;
    return score(a) - score(b);
  });

  return result;
}

export function HomePanel() {
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const botStatusQuery = trpc.bot.status.useQuery(undefined, { refetchInterval: 5000 });
  const botStatus = (botStatusQuery.data ?? null) as BotStatus | null;
  const markets = Array.isArray(botStatus?.markets) ? botStatus.markets : [];
  const isRunning = botStatus?.running ?? false;

  const livePrefixesQuery = trpc.live.prefixes.useQuery(undefined, { refetchInterval: 10000 });
  const livePrefixes = livePrefixesQuery.data ?? [];

  const slugsQuery = trpc.portfolio.slugs.useQuery();
  const slugs = slugsQuery.data ?? [];

  const botStartMutation = trpc.bot.start.useMutation();
  const botStopMutation = trpc.bot.stop.useMutation();
  const collectorStartMutation = trpc.collector.start.useMutation();
  const collectorStopMutation = trpc.collector.stop.useMutation();
  const collectorRestartMutation = trpc.collector.restart.useMutation();

  const utils = trpc.useUtils();

  useEffect(() => {
    if (isRunning && starting) setStarting(false);
  }, [isRunning, starting]);

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current) {
        clearTimeout(refreshTimerRef.current);
        clearInterval(refreshTimerRef.current);
      }
    };
  }, []);

  function flashMessage(msg: string) {
    setMessage(msg);
    setTimeout(() => setMessage(null), 5000);
  }

  function delayedRefresh(ms = 2000) {
    refreshTimerRef.current = setTimeout(() => utils.bot.status.invalidate(), ms);
  }

  async function handleStartApp() {
    setLoading(true);
    setStarting(true);
    const res = await botStartMutation.mutateAsync({ restore: true });
    if (!res.ok) {
      flashMessage(res.error || "Failed to start");
      setStarting(false);
    } else {
      flashMessage(`Orchestrator started (PID ${(res as any).pid})`);
      let attempts = 0;
      const poll = setInterval(() => {
        attempts++;
        utils.bot.status.invalidate();
        if (attempts >= 10) { clearInterval(poll); setStarting(false); }
      }, 1000);
      refreshTimerRef.current = poll as unknown as ReturnType<typeof setTimeout>;
    }
    setLoading(false);
  }

  async function handleStopApp() {
    setLoading(true);
    const res = await botStopMutation.mutateAsync();
    flashMessage(res.ok ? "Orchestrator stopped" : res.error || "Failed");
    setLoading(false);
    utils.bot.status.invalidate();
  }

  async function handleStartCollector(slugPrefix: string, windowSeconds: number) {
    setLoading(true);
    const res = await collectorStartMutation.mutateAsync({ slug_prefix: slugPrefix, window: windowSeconds });
    flashMessage(res.ok ? `Collector started: ${toSlugKey(slugPrefix)}` : res.error || "Failed");
    setLoading(false);
    delayedRefresh();
  }

  async function handleStopCollector(slugPrefix: string, afterSession?: boolean) {
    setLoading(true);
    const res = await collectorStopMutation.mutateAsync({ slug_prefix: slugPrefix, after_session: afterSession });
    flashMessage(
      res.ok
        ? afterSession
          ? `Collector will stop after session: ${toSlugKey(slugPrefix)}`
          : `Collector stopped: ${toSlugKey(slugPrefix)}`
        : res.error || "Failed"
    );
    setLoading(false);
    delayedRefresh();
  }

  async function handleRestartCollector(slugPrefix: string) {
    setLoading(true);
    const res = await collectorRestartMutation.mutateAsync({ slug_prefix: slugPrefix, rebuild: true });
    flashMessage(
      res.ok
        ? `Rebuild & restart queued: ${toSlugKey(slugPrefix)} (${(res as any).status})`
        : (res as any).error || "Failed"
    );
    setLoading(false);
  }

  const marketViews = buildMarketList(slugs, livePrefixes, markets);
  const totalStrategies = markets.reduce((n, m) => n + (m.strategies?.length ?? 0), 0);

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">Orchestrator</h2>
      <div className="bg-card border border-border rounded-lg px-5 py-4 mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className={`inline-block w-3 h-3 rounded-full ${
            isRunning ? "bg-up animate-pulse-dot" : starting ? "bg-btc" : "bg-dim"
          }`} />
          <div>
            <div className={`text-[15px] font-semibold ${isRunning ? "text-up" : "text-txt"}`}>
              {botStatus === null ? "..." : isRunning ? "Orchestrator running" : starting ? "Starting..." : "Orchestrator stopped"}
            </div>
            {isRunning && (totalStrategies > 0 || botStatus?.clob_bridge) && (
              <div className="text-dim text-xs mt-0.5 flex items-center gap-2">
                {totalStrategies > 0 && (
                  <span>{totalStrategies} strateg{totalStrategies !== 1 ? "ies" : "y"} active</span>
                )}
                {totalStrategies > 0 && botStatus?.clob_bridge && <span className="text-border">·</span>}
                {botStatus?.clob_bridge && (
                  <span className="flex items-center gap-1">
                    <span className={`w-1.5 h-1.5 rounded-full ${botStatus.clob_bridge.healthy ? "bg-up" : "bg-down"}`} />
                    <span className={botStatus.clob_bridge.healthy ? "" : "text-down"}>
                      clob-bridge {botStatus.clob_bridge.healthy ? "" : "down"}
                    </span>
                  </span>
                )}
              </div>
            )}
          </div>
        </div>
        {isRunning ? (
          <button className="btn-danger" onClick={handleStopApp} disabled={loading}>
            Stop Orchestrator
          </button>
        ) : botStatus !== null && !starting ? (
          <button className="btn-primary" onClick={handleStartApp} disabled={loading}>
            Start Orchestrator
          </button>
        ) : null}
      </div>

      <h2 className="text-lg font-semibold mb-4">Markets</h2>
      <div className="space-y-4">
        {marketViews.map((mv) => (
          <MarketCard
            key={mv.slugKey}
            mv={mv}
            loading={loading}
            onStartCollector={() => handleStartCollector(mv.slugPrefix, mv.windowSeconds)}
            onStopCollector={(after) => handleStopCollector(mv.slugPrefix, after)}
            onRestartCollector={() => handleRestartCollector(mv.slugPrefix)}
          />
        ))}
        {marketViews.length === 0 && (
          <p className="text-dim text-sm">No known markets. Add entries to src/markets.rs.</p>
        )}
      </div>

      {message && (
        <div className="mt-4 px-4 py-2.5 bg-accent/10 border border-accent/20 rounded-md text-[13px] text-accent">
          {message}
        </div>
      )}
    </div>
  );
}

function MarketCard({
  mv,
  loading,
  onStartCollector,
  onStopCollector,
  onRestartCollector,
}: {
  mv: MarketView;
  loading: boolean;
  onStartCollector: () => void;
  onStopCollector: (afterSession?: boolean) => void;
  onRestartCollector: () => void;
}) {
  const activeStrategies = mv.strategies;
  const bookSourceQuery = trpc.collector.bookSource.useQuery(
    { slug_prefix: mv.slugPrefix },
    { enabled: mv.isCollecting, refetchInterval: 5000 },
  );
  const toggleBookSourceMutation = trpc.collector.toggleBookSource.useMutation({
    onSuccess: () => {
      setTimeout(() => bookSourceQuery.refetch(), 500);
    },
  });
  const bookSource = bookSourceQuery.data?.book_source ?? "rest";

  const dotColor = mv.isCollecting
    ? activeStrategies.length > 0 ? "bg-up animate-pulse-dot" : "bg-btc animate-pulse-dot"
    : "bg-dim";

  const statusLabel = mv.isCollecting
    ? activeStrategies.length > 0
      ? `Collecting \u00B7 ${activeStrategies.length} strateg${activeStrategies.length !== 1 ? "ies" : "y"}`
      : "Collecting"
    : "Inactive";

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2.5">
          <span className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${dotColor}`} />
          {mv.isCollecting ? (
            <Link href={`/live/${mv.slugKey}`} className="text-[15px] font-semibold text-txt no-underline hover:text-accent transition-colors">
              {mv.asset} {mv.windowSeconds / 60}m
            </Link>
          ) : (
            <span className="text-[15px] font-semibold">{mv.asset} {mv.windowSeconds / 60}m</span>
          )}
          <span className="text-dim text-xs">{statusLabel}</span>
          {mv.marketStatus?.session_count != null && mv.marketStatus.session_count > 0 && (
            <span className="text-dim text-xs">{mv.marketStatus.session_count} sessions</span>
          )}
          {mv.marketStatus?.stop_after_session && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-down/15 text-down">STOPPING</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {mv.isCollecting ? (
            <>
              <button
                className="btn-sm"
                style={{
                  background: bookSource === "ws" ? "var(--up)" : "var(--dim)",
                  color: "var(--bg)",
                  minWidth: 70,
                  fontSize: 11,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                }}
                onClick={() => toggleBookSourceMutation.mutate({ slug_prefix: mv.slugPrefix })}
                disabled={toggleBookSourceMutation.isPending}
                title={`Book source: ${bookSource.toUpperCase()}. Click to toggle.`}
              >
                Book: {bookSource}
              </button>
              <button
                className="btn-sm"
                style={{ background: "var(--accent)", color: "var(--bg)" }}
                onClick={onRestartCollector}
                disabled={loading}
                title="Rebuild Rust binary and restart collector at next window boundary"
              >
                Rebuild &amp; Restart
              </button>
              <button
                className="btn-warning btn-sm"
                onClick={() => onStopCollector(true)}
                disabled={loading}
                title="Stop after current session"
              >
                Stop After Session
              </button>
              <button
                className="btn-danger btn-sm"
                onClick={() => onStopCollector(false)}
                disabled={loading}
                title="Stop collector now"
              >
                Stop
              </button>
            </>
          ) : (
            <button
              className="btn-primary btn-sm"
              onClick={onStartCollector}
              disabled={loading}
            >
              Start Collecting
            </button>
          )}
        </div>
      </div>

      {activeStrategies.length > 0 && (
        <div className="px-4 py-2.5 border-t border-border">
          <div className="flex flex-wrap gap-2">
            {activeStrategies.map((s) => (
              <Link key={s.name} href={`/sessions/${mv.slugKey}`} className="inline-flex items-center gap-1.5 text-[12px] text-dim no-underline hover:text-txt">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${s.alive === false ? "bg-down" : "bg-up"}`} />
                {s.name}
                <span className="font-mono">${(s.bankroll ?? s.cash ?? 0).toFixed(0)}</span>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
