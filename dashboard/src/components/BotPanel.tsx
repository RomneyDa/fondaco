import { useEffect, useRef, useState } from "react";
import { fetchBotStatus, fetchSlugs, fetchStrategies, startBot, startMarket, stopMarket, tradeBotOn, tradeBotOff, stopBot, killBot, setBotCash, reloadBot } from "../api";
import type { SlugInfo } from "../api";
import type { BotStatus, MarketStatus } from "../types";
import { TradingLog } from "./TradingLog";

interface Props {
  botStatus: BotStatus | null;
  onRefresh: () => void;
  tradeRefresh: number;
}

export function BotPanel({ botStatus, onRefresh, tradeRefresh }: Props) {
  const [strategy, setStrategy] = useState("");
  const [params, setParams] = useState("");
  const [cash, setCash] = useState("100");
  const [dryRun, setDryRun] = useState(true);
  const [sessionCap, setSessionCap] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selectedMarket, setSelectedMarket] = useState("");

  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markets = botStatus?.markets ?? [];
  const isRunning = botStatus?.running ?? false;

  useEffect(() => {
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, []);

  // Auto-select first market
  useEffect(() => {
    if (markets.length > 0 && (!selectedMarket || !markets.some(m => m.slug_prefix === selectedMarket))) {
      setSelectedMarket(markets[0].slug_prefix);
    }
  }, [markets, selectedMarket]);

  // --- Strategies (dynamic) ---
  const [strategies, setStrategies] = useState<string[]>([]);
  useEffect(() => {
    fetchStrategies().then((s) => {
      setStrategies(s);
      if (s.length > 0 && !strategy) setStrategy(s[0]);
    });
  }, []);

  // --- Slugs for market-on ---
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
    const res = await startBot({ restore: true });
    flashMessage(res.ok ? `App started (PID ${res.pid})` : res.error || "Failed to start");
    setLoading(false);
    refreshTimerRef.current = setTimeout(onRefresh, 2500);
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

  async function handleTradeOn() {
    if (!selectedMarket) { flashMessage("Select a market first"); return; }
    if (!strategy) { flashMessage("Select a strategy"); return; }
    setLoading(true);
    const parsedParams: Record<string, string> = {};
    if (params.trim()) {
      for (const pair of params.split(",")) {
        const [k, v] = pair.split("=").map((s) => s.trim());
        if (k && v) parsedParams[k] = v;
      }
    }
    const body: any = {
      slug_prefix: selectedMarket,
      strategy,
      params: parsedParams,
      cash: parseFloat(cash) || 100,
      dry_run: dryRun,
    };
    if (sessionCap.trim()) body.session_cap = parseFloat(sessionCap);
    const res = await tradeBotOn(body);
    flashMessage(res.ok ? `Started: ${strategy}` : res.error || "Failed");
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
      {/* Status header */}
      <div className="bot-status-section">
        <div className="meta-grid">
          <div className="meta-card">
            <div className="meta-label">Status</div>
            <div className="meta-value">
              <span className={`bot-status-dot ${isRunning ? "running" : "stopped"}`} />
              {botStatus === null ? "Loading..." : isRunning ? "Running" : "Offline"}
            </div>
          </div>
          {isRunning && (
            <div className="meta-card">
              <div className="meta-label">Markets</div>
              <div className="meta-value">
                {markets.length > 0
                  ? `${markets.length} active`
                  : <span style={{ color: "var(--text-dim)" }}>None</span>}
              </div>
            </div>
          )}
          {isRunning && (
            <div className="meta-card">
              <div className="meta-label">Strategies</div>
              <div className="meta-value">
                {markets.reduce((n, m) => n + (m.strategies?.length ?? 0), 0)}
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Running: show markets, controls */}
      {isRunning && (
        <>
          {/* Per-market status */}
          {markets.map((m) => (
            <MarketSection
              key={m.slug_key}
              market={m}
              loading={loading}
              onTradeOff={handleTradeOff}
              onReload={handleReload}
              onMarketOff={handleMarketOff}
            />
          ))}

          {/* Control forms */}
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginTop: 20 }}>
            {/* Add Market */}
            <div className="bot-form" style={{ flex: "1 1 280px" }}>
              <h3>Add Market</h3>
              <div className="bot-form-row">
                <label>
                  Market
                  <select value={selectedSlug} onChange={(e) => setSelectedSlug(e.target.value)}>
                    {slugs.map((s) => (
                      <option key={s.slug_prefix} value={s.slug_prefix}>
                        {s.asset} {s.window_duration_seconds / 60}m ({s.slug_prefix})
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <button
                className="bot-btn bot-btn-start"
                onClick={handleMarketOn}
                disabled={loading || !selectedSlug}
              >
                Start Market
              </button>
            </div>

            {/* Add Worker */}
            {markets.length > 0 && (
              <div className="bot-form" style={{ flex: "2 1 400px" }}>
                <h3>Add Strategy</h3>
                <div style={{ display: "flex", gap: 12 }}>
                  <div className="bot-form-row" style={{ flex: 1 }}>
                    <label>
                      Market
                      <select value={selectedMarket} onChange={(e) => setSelectedMarket(e.target.value)}>
                        {markets.map((m) => (
                          <option key={m.slug_key} value={m.slug_prefix}>{m.slug_key}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="bot-form-row" style={{ flex: 1 }}>
                    <label>
                      Strategy
                      <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                        {strategies.map((s) => (
                          <option key={s} value={s}>{s}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                </div>
                <div className="bot-form-row">
                  <label>
                    Params <span className="subtitle">(key=val,key=val)</span>
                    <input
                      type="text"
                      value={params}
                      onChange={(e) => setParams(e.target.value)}
                      placeholder="min_elapsed=0.7,threshold=0.5"
                    />
                  </label>
                </div>
                <div style={{ display: "flex", gap: 12 }}>
                  <div className="bot-form-row" style={{ flex: 1 }}>
                    <label>
                      Cash ($)
                      <input type="number" value={cash} onChange={(e) => setCash(e.target.value)} min="1" step="1" />
                    </label>
                  </div>
                  <div className="bot-form-row" style={{ flex: 1 }}>
                    <label>
                      Session Cap ($) <span className="subtitle">optional</span>
                      <input type="number" value={sessionCap} onChange={(e) => setSessionCap(e.target.value)} min="1" step="1" placeholder="none" />
                    </label>
                  </div>
                  <div className="bot-form-row" style={{ flex: 1, display: "flex", alignItems: "flex-end" }}>
                    <label className="bot-checkbox">
                      <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
                      Dry Run
                    </label>
                  </div>
                </div>
                <button className="bot-btn bot-btn-start" onClick={handleTradeOn} disabled={loading}>
                  Add Strategy
                </button>
              </div>
            )}
          </div>

          {/* App-level actions */}
          <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
            <button
              className="bot-btn"
              style={{ background: "var(--btc)", color: "#fff" }}
              onClick={handleKill}
              disabled={loading}
              title="Stop all markets after their current sessions finish"
            >
              Stop After Sessions
            </button>
            <button
              className="bot-btn bot-btn-stop"
              onClick={handleStop}
              disabled={loading}
              title="Immediately stop the app and all markets"
            >
              Stop Now
            </button>
          </div>
        </>
      )}

      {/* Offline: show start button */}
      {!isRunning && botStatus !== null && (
        <div className="bot-controls">
          <div className="bot-form">
            <p className="subtitle" style={{ marginBottom: 12 }}>
              Start the app and restore previous markets &amp; strategies.
            </p>
            <button className="bot-btn bot-btn-start" onClick={handleStartApp} disabled={loading}>
              Start App
            </button>
          </div>
        </div>
      )}

      {message && <div className="bot-message">{message}</div>}

      <TradingLog refreshTrigger={tradeRefresh} />
    </div>
  );
}

function MarketSection({
  market,
  loading,
  onTradeOff,
  onReload,
  onMarketOff,
}: {
  market: MarketStatus;
  loading: boolean;
  onTradeOff: (slug: string, strategy?: string) => void;
  onReload: (slug: string, strategy: string) => void;
  onMarketOff: (slug: string) => void;
}) {
  const strategies = market.strategies ?? [];

  return (
    <div style={{ marginTop: 16, padding: 16, background: "var(--card-bg)", border: "1px solid var(--border)", borderRadius: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: strategies.length > 0 ? 12 : 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              display: "inline-block",
              width: 10,
              height: 10,
              borderRadius: "50%",
              background: market.running ? "var(--green)" : "var(--red)",
              animation: market.running ? "pulse 2s infinite" : "none",
            }}
          />
          <strong style={{ fontSize: 15 }}>{market.slug_key}</strong>
          <span style={{ color: "var(--text-dim)", fontSize: 12 }}>
            {market.session_count} sessions
            {(market.tick_clients ?? 0) > 0 && ` · ${market.tick_clients} clients`}
          </span>
          {market.stop_after_session && (
            <span style={{
              padding: "2px 6px", borderRadius: 4, fontSize: 10, fontWeight: 600,
              background: "rgba(248,81,73,0.15)", color: "var(--red)"
            }}>STOPPING</span>
          )}
        </div>
        <button
          className="bot-btn bot-btn-stop-trading"
          style={{ fontSize: 11, padding: "3px 10px" }}
          onClick={() => onMarketOff(market.slug_prefix)}
          disabled={loading}
        >
          Stop Market
        </button>
      </div>

      {strategies.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {strategies.map((s) => {
            const alive = s.alive !== false;
            return (
              <div key={s.name} style={{
                display: "flex", alignItems: "center", gap: 12, padding: "8px 12px",
                background: "var(--bg)", borderRadius: 6, border: "1px solid var(--border)"
              }}>
                <span style={{
                  width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                  background: alive ? "var(--green)" : "var(--red)",
                }} />
                <span style={{ fontWeight: 600, fontSize: 14, minWidth: 120 }}>{s.name}</span>
                {s.dry_run && <span className="dry-run-tag">DRY</span>}
                {!alive && <span style={{
                  padding: "1px 6px", borderRadius: 4, fontSize: 10, fontWeight: 600,
                  background: "rgba(248,81,73,0.15)", color: "var(--red)"
                }}>DEAD</span>}
                <span style={{ color: "var(--text-dim)", fontSize: 13, marginLeft: "auto" }}>
                  ${(s.bankroll ?? s.cash).toFixed(2)}
                  {s.session_cap != null && (
                    <span style={{ opacity: 0.6 }}> (${s.session_cap.toFixed(0)}/s)</span>
                  )}
                </span>
                {s.pid != null && (
                  <span style={{ color: "var(--text-dim)", fontSize: 11 }}>PID {s.pid}</span>
                )}
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    className="bot-btn"
                    style={{ fontSize: 11, padding: "2px 8px", background: "var(--accent)", color: "#fff" }}
                    onClick={() => onReload(market.slug_prefix, s.name)}
                    disabled={loading}
                  >
                    Reload
                  </button>
                  <button
                    className="bot-btn bot-btn-stop-trading"
                    style={{ fontSize: 11, padding: "2px 8px" }}
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
      ) : (
        <div style={{ color: "var(--text-dim)", fontSize: 13, fontStyle: "italic" }}>
          No strategies — add one below
        </div>
      )}
    </div>
  );
}
