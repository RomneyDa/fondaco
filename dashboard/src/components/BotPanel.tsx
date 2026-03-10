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
}

export function BotPanel({ botStatus, onRefresh, tradeRefresh }: Props) {
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [starting, setStarting] = useState(false);

  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const markets = botStatus?.markets ?? [];
  const isRunning = botStatus?.running ?? false;

  // Clear "starting" once the app reports running
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

  // --- Strategies (dynamic, retry until loaded) ---
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
    setStarting(true);
    const res = await startBot({ restore: true });
    if (!res.ok) {
      flashMessage(res.error || "Failed to start");
      setStarting(false);
    } else {
      flashMessage(`App started (PID ${res.pid})`);
      // Poll aggressively until the app is confirmed running
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        onRefresh();
        if (attempts >= 10) {
          clearInterval(poll);
          setStarting(false);
        }
      }, 1000);
      // Clear interval once we detect running (via the useEffect above)
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
      {/* Status header */}
      <div className="bot-status-section">
        <div className="meta-grid">
          <div className="meta-card">
            <div className="meta-label">Status</div>
            <div className="meta-value">
              <span className={`bot-status-dot ${isRunning ? "running" : starting ? "starting" : "stopped"}`} />
              {botStatus === null ? "Loading..." : isRunning ? "Running" : starting ? "Starting..." : "Offline"}
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

          {/* Add Market */}
          <div style={{ marginTop: 20 }}>
            <div className="bot-form" style={{ maxWidth: 360 }}>
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
            {starting ? (
              <p className="subtitle">Starting app...</p>
            ) : (
              <>
                <p className="subtitle" style={{ marginBottom: 12 }}>
                  Start the app and restore previous markets &amp; strategies.
                </p>
                <button className="bot-btn bot-btn-start" onClick={handleStartApp} disabled={loading}>
                  Start App
                </button>
              </>
            )}
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
    <div style={{ marginTop: 16, padding: 16, background: "var(--card-bg)", border: "1px solid var(--border)", borderRadius: 8 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: activeStrategies.length > 0 ? 12 : 0 }}>
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
          <Link to={`/live/${market.slug_key}`} style={{ fontSize: 15, fontWeight: 600, color: "var(--text)", textDecoration: "none" }}>
            {market.slug_key}
          </Link>
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

      {activeStrategies.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {activeStrategies.map((s) => {
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
                  {s.session_cash_ratio != null && (
                    <span style={{ opacity: 0.6 }}> ({Math.round(s.session_cash_ratio.ratio * 100)}% up to ${s.session_cash_ratio.max.toFixed(0)})</span>
                  )}
                  {s.session_cash_ratio == null && s.session_cap != null && (
                    <span style={{ opacity: 0.6 }}> (${s.session_cap.toFixed(0)}/s)</span>
                  )}
                </span>
                {(market.market_type ?? "updown") === "updown" && (
                  <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, cursor: "pointer", color: "var(--text-dim)" }}>
                    <input
                      type="checkbox"
                      checked={!!s.auto_exit}
                      onChange={(e) => onToggleAutoExit(market.slug_prefix, s.name, e.target.checked)}
                      style={{ width: 13, height: 13, accentColor: "var(--accent)" }}
                    />
                    Auto-exit
                  </label>
                )}
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
      )}

      {available.length > 0 && (
        <div className="add-strategy-form">
          <div className="add-strategy-row">
            <label className="add-strategy-field add-strategy-field--strategy">
              <span className="add-strategy-label">Strategy</span>
              <select value={selected} onChange={(e) => setSelected(e.target.value)}>
                <option value="">Select...</option>
                {available.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className="add-strategy-field">
              <span className="add-strategy-label">Cash</span>
              <input
                type="number" value={cash} onChange={(e) => setCash(e.target.value)}
                min="1" step="1"
              />
            </label>
            <label className="add-strategy-field">
              <span className="add-strategy-label">Session limit</span>
              <input
                type="text" value={sessionCap} onChange={(e) => setSessionCap(e.target.value)}
                placeholder="5 or 25%:3"
              />
            </label>
            <button
              className="bot-btn add-strategy-btn"
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
