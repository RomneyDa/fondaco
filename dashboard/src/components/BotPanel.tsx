import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { fetchBotStatus, fetchSlugs, fetchStrategies, startBot, startMarket, stopMarket, tradeBotOn, tradeBotOff, stopBot, killBot, setBotCash, reloadBot, setAutoExit } from "../api";
import type { SlugInfo } from "../api";
import type { BotStatus, MarketStatus } from "../types";
import { TradingLog } from "./TradingLog";

interface Props {
  botStatus: BotStatus | null;
  onRefresh: () => void;
  tradeRefresh: number;
  livePrefixes?: string[];
}

export function BotPanel({ botStatus, onRefresh, tradeRefresh, livePrefixes = [] }: Props) {
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);

  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markets = botStatus?.markets ?? [];
  const isRunning = botStatus?.running ?? false;

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

  const [strategies, setStrategies] = useState<string[]>([]);
  useEffect(() => {
    let cancelled = false;
    function load() {
      fetchStrategies().then((s) => {
        if (cancelled) return;
        if (s.length > 0) {
          setStrategies(s);
        } else {
          setTimeout(load, 3000);
        }
      });
    }
    load();
    return () => { cancelled = true; };
  }, []);

  const [slugs, setSlugs] = useState<SlugInfo[]>([]);
  const [selectedSlug, setSelectedSlug] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetchSlugs().then((s) => {
      if (cancelled) return;
      setSlugs(s);
      if (s.length > 0 && !selectedSlug) {
        setSelectedSlug(s[0].slug_prefix);
      }
    });
    return () => { cancelled = true; };
  }, []);

  function flashMessage(msg: string) {
    setMessage(msg);
    setTimeout(() => setMessage(null), 5000);
  }

  async function handleStartApp() {
    setLoading(true);
    setStarting(true);
    const res = await startBot({ restore: true });
    if (!res.ok) {
      flashMessage(res.error || "Failed to start");
      setStarting(false);
    } else {
      flashMessage(`App started (PID ${res.pid})`);
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        onRefresh();
        if (attempts >= 10) {
          clearInterval(poll);
          setStarting(false);
        }
      }, 1000);
      refreshTimerRef.current = poll as unknown as ReturnType<typeof setTimeout>;
    }
    setLoading(false);
  }

  async function handleMarketOn() {
    const slug = slugs.find((s) => s.slug_prefix === selectedSlug);
    if (!selectedSlug) { flashMessage("Select a market"); return; }
    setLoading(true);
    const res = await startMarket({
      slug_prefix: selectedSlug,
      window: slug?.window_duration_seconds,
    });
    flashMessage(res.ok ? `Market started: ${res.slug_key || selectedSlug}` : res.error || "Failed");
    setLoading(false);
    refreshTimerRef.current = setTimeout(onRefresh, 2000);
  }

  async function handleMarketOff(slug_prefix: string) {
    setLoading(true);
    const res = await stopMarket({ slug_prefix });
    flashMessage(res.ok ? `Market stopped: ${slug_prefix}` : res.error || "Failed");
    setLoading(false);
    refreshTimerRef.current = setTimeout(onRefresh, 2000);
  }

  async function handleTradeOn(slugPrefix: string, strategyName: string, opts: { cash: number; sessionCap?: number; sessionCashRatio?: { ratio: number; max: number }; dryRun: boolean }) {
    setLoading(true);
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

  async function handleTradeOff(slug_prefix: string, strategyName?: string) {
    setLoading(true);
    const res = await tradeBotOff(slug_prefix, strategyName);
    flashMessage(res.ok
      ? (strategyName ? `Stopped: ${strategyName}` : "All strategies stopped")
      : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleReload(slug_prefix: string, strategyName: string) {
    setLoading(true);
    const res = await reloadBot(slug_prefix, strategyName);
    flashMessage(res.ok ? `Reloaded: ${strategyName}` : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleStop() {
    setLoading(true);
    const res = await stopBot();
    flashMessage(res.ok ? "Stop requested" : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  async function handleKill() {
    setLoading(true);
    const res = await killBot();
    flashMessage(res.ok ? "Kill signal sent — stopping after current sessions" : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">Trading</h2>

      {/* Status cards */}
      {(() => {
        const marketSlugs = new Set(markets.map((m) => m.slug_key));
        const streamOnly = livePrefixes.filter((p) => !marketSlugs.has(p));
        return (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-6">
            <div className="bg-card border border-border rounded-lg px-4 py-3">
              <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">App</div>
              <div className="text-base font-semibold flex items-center gap-2">
                <span className={`inline-block w-2 h-2 rounded-full ${
                  isRunning
                    ? markets.length > 0 ? "bg-up animate-pulse-dot" : "bg-dim"
                    : starting ? "bg-btc" : "bg-down"
                }`} />
                {botStatus === null
                  ? "Loading..."
                  : isRunning
                    ? markets.length > 0 ? "Running" : "Idle"
                    : starting ? "Starting..." : "Offline"}
              </div>
              {isRunning && markets.length === 0 && (
                <div className="text-[11px] text-dim mt-1">No markets registered</div>
              )}
            </div>
            <div className="bg-card border border-border rounded-lg px-4 py-3">
              <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Markets</div>
              {markets.length > 0 ? (
                <div className="text-base font-semibold">{markets.length} active</div>
              ) : streamOnly.length > 0 ? (
                <>
                  <div className="text-base font-semibold flex items-center gap-2">
                    <span className="inline-block w-2 h-2 rounded-full bg-btc animate-pulse-dot" />
                    {streamOnly.length} collecting
                  </div>
                  <div className="text-[11px] text-dim mt-1">
                    {streamOnly.join(", ")}
                  </div>
                </>
              ) : (
                <div className="text-base font-semibold text-dim">None</div>
              )}
            </div>
            {isRunning && (
              <div className="bg-card border border-border rounded-lg px-4 py-3">
                <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Strategies</div>
                <div className="text-base font-semibold">
                  {markets.reduce((n, m) => n + (m.strategies?.length ?? 0), 0)}
                </div>
              </div>
            )}
          </div>
        );
      })()}

      {isRunning && (
        <>
          <div className="space-y-4">
            {markets.map((m) => (
              <MarketSection
                key={m.slug_key}
                market={m}
                loading={loading}
                strategies={strategies}
                onTradeOn={handleTradeOn}
                onTradeOff={handleTradeOff}
                onReload={handleReload}
                onMarketOff={handleMarketOff}
                onToggleAutoExit={async (slug, strat, enabled) => {
                  const res = await setAutoExit(slug, enabled, strat);
                  if (!res.ok) flashMessage(res.error || "Failed");
                  onRefresh();
                }}
                flashMessage={flashMessage}
              />
            ))}
          </div>

          {/* Add Market */}
          {(() => {
            // slug_prefix has trailing dash (e.g. "btc-updown-5m-"), slug_key does not
            const activeKeys = new Set(markets.map((m) => m.slug_key));
            const liveKeys = new Set(livePrefixes);
            const availableSlugs = slugs.filter((s) => !activeKeys.has(s.slug_prefix.replace(/-$/, "")));
            const streamingSlugs = availableSlugs.filter((s) => liveKeys.has(s.slug_prefix.replace(/-$/, "")));
            const freshSlugs = availableSlugs.filter((s) => !liveKeys.has(s.slug_prefix.replace(/-$/, "")));
            return (
              <div className="mt-6">
                <div className="bg-card border border-border rounded-lg p-5 max-w-[360px]">
                  <h3 className="text-sm font-semibold mb-3">Add Market</h3>
                  {availableSlugs.length === 0 ? (
                    <p className="text-dim text-[13px]">All markets are already active.</p>
                  ) : (
                    <>
                      <label className="flex flex-col gap-1.5 text-[13px] text-dim mb-4">
                        Market
                        <select value={selectedSlug} onChange={(e) => setSelectedSlug(e.target.value)}>
                          <option value="">Select...</option>
                          {streamingSlugs.length > 0 && (
                            <optgroup label="Collecting (collector running)">
                              {streamingSlugs.map((s) => (
                                <option key={s.slug_prefix} value={s.slug_prefix}>
                                  {s.asset} {s.window_duration_seconds / 60}m
                                </option>
                              ))}
                            </optgroup>
                          )}
                          {freshSlugs.length > 0 && (
                            <optgroup label={streamingSlugs.length > 0 ? "Not collecting" : "Available"}>
                              {freshSlugs.map((s) => (
                                <option key={s.slug_prefix} value={s.slug_prefix}>
                                  {s.asset} {s.window_duration_seconds / 60}m
                                </option>
                              ))}
                            </optgroup>
                          )}
                        </select>
                      </label>
                      <button
                        className="btn-primary w-full"
                        onClick={handleMarketOn}
                        disabled={loading || !selectedSlug}
                      >
                        Start Market
                      </button>
                    </>
                  )}
                </div>
              </div>
            );
          })()}

          {/* App-level actions */}
          <div className="flex gap-2.5 mt-6">
            <button
              className="btn-warning"
              onClick={handleKill}
              disabled={loading}
              title="Stop all markets after their current sessions finish"
            >
              Stop After Sessions
            </button>
            <button
              className="btn-danger"
              onClick={handleStop}
              disabled={loading}
              title="Immediately stop the app and all markets"
            >
              Stop Now
            </button>
          </div>
        </>
      )}

      {!isRunning && botStatus !== null && (
        <div>
          <div className="bg-card border border-border rounded-lg p-5 max-w-[400px]">
            {starting ? (
              <p className="text-dim text-sm">Starting app...</p>
            ) : (
              <>
                <p className="text-dim text-sm mb-4">
                  Start the app and restore previous markets &amp; strategies.
                </p>
                <button
                  className="btn-primary w-full"
                  onClick={handleStartApp}
                  disabled={loading}
                >
                  Start App
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {message && (
        <div className="mt-4 px-4 py-2.5 bg-accent/10 border border-accent/20 rounded-md text-[13px] text-accent">
          {message}
        </div>
      )}

      <TradingLog refreshTrigger={tradeRefresh} />
    </div>
  );
}

function MarketSection({
  market,
  loading,
  strategies: allStrategies,
  onTradeOn,
  onTradeOff,
  onReload,
  onMarketOff,
  onToggleAutoExit,
  flashMessage,
}: {
  market: MarketStatus;
  loading: boolean;
  strategies: string[];
  onTradeOn: (slug: string, strategy: string, opts: { cash: number; sessionCap?: number; sessionCashRatio?: { ratio: number; max: number }; dryRun: boolean }) => void;
  onTradeOff: (slug: string, strategy?: string) => void;
  onReload: (slug: string, strategy: string) => void;
  onMarketOff: (slug: string) => void;
  onToggleAutoExit: (slug: string, strategy: string, enabled: boolean) => void;
  flashMessage: (msg: string) => void;
}) {
  const activeStrategies = market.strategies ?? [];
  const activeNames = new Set(activeStrategies.map((s) => s.name));
  const available = allStrategies.filter((s) => !activeNames.has(s));

  const [selected, setSelected] = useState("");
  const [cash, setCash] = useState("10");
  const [sessionCap, setSessionCap] = useState("25%:3");

  return (
    <div className="p-4 bg-card border border-border rounded-lg">
      <div className={`flex justify-between items-center ${activeStrategies.length > 0 ? "mb-3" : ""}`}>
        <div className="flex items-center gap-2.5">
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${market.running ? "bg-up animate-pulse-dot" : "bg-down"}`} />
          <Link to={`/live/${market.slug_key}`} className="text-[15px] font-semibold text-txt no-underline hover:text-accent transition-colors">
            {market.slug_key}
          </Link>
          <span className="text-dim text-xs">
            {market.session_count} sessions
            {(market.tick_clients ?? 0) > 0 && ` · ${market.tick_clients} clients`}
          </span>
          {market.stop_after_session && (
            <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-down/15 text-down">STOPPING</span>
          )}
        </div>
        <button
          className="btn-danger btn-sm"
          onClick={() => onMarketOff(market.slug_prefix)}
          disabled={loading}
        >
          Stop Market
        </button>
      </div>

      {activeStrategies.length > 0 && (
        <div className="flex flex-col gap-2">
          {activeStrategies.map((s) => {
            const alive = s.alive !== false;
            return (
              <div key={s.name} className="flex items-center gap-3 px-3.5 py-2.5 bg-bg rounded-md border border-border">
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
                {(market.market_type ?? "updown") === "updown" && (
                  <label className="flex items-center gap-1.5 text-[11px] cursor-pointer text-dim">
                    <input
                      type="checkbox"
                      checked={!!s.auto_exit}
                      onChange={(e) => onToggleAutoExit(market.slug_prefix, s.name, e.target.checked)}
                      className="w-[13px] h-[13px] accent-accent"
                    />
                    Auto-exit
                  </label>
                )}
                {s.pid != null && (
                  <span className="text-dim text-[11px]">PID {s.pid}</span>
                )}
                <div className="flex gap-1.5">
                  <button
                    className="btn-accent btn-sm"
                    onClick={() => onReload(market.slug_prefix, s.name)}
                    disabled={loading}
                  >
                    Reload
                  </button>
                  <button
                    className="btn-danger btn-sm"
                    onClick={() => onTradeOff(market.slug_prefix, s.name)}
                    disabled={loading}
                  >
                    Kill
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {available.length > 0 && (
        <div className="mt-3 p-3 bg-bg rounded-md border border-dashed border-border">
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
              <input
                type="number" value={cash} onChange={(e) => setCash(e.target.value)}
                min="1" step="1"
                className="text-[13px] w-20"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-[10px] font-semibold uppercase tracking-tight text-dim">Session limit</span>
              <input
                type="text" value={sessionCap} onChange={(e) => setSessionCap(e.target.value)}
                placeholder="5 or 25%:3"
                className="text-[13px] w-24"
              />
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
                onTradeOn(market.slug_prefix, selected, {
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
    </div>
  );
}
