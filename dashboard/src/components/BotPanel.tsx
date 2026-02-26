import { useEffect, useState } from "react";
import { fetchBotStatus, fetchSlugs, startBot, tradeBotOn, tradeBotOff, stopBot, setBotCash } from "../api";
import type { SlugInfo } from "../api";
import type { BotStatus } from "../types";
import { TradingLog } from "./TradingLog";

const STRATEGIES: Record<string, string> = {
  AlwaysBuyUp: "Immediately buys Up tokens at the first tick and holds until resolution. Simple benchmark \u2014 profits whenever BTC ends higher.",
  AlwaysBuyDown: "Immediately buys Down tokens at the first tick and holds until resolution. Simple benchmark \u2014 profits whenever BTC ends lower.",
  MomentumEntry: "Watches BTC price early in the window. If BTC moves more than a threshold (in basis points), buys in that direction. Only enters during the first portion of the window to catch early trends.",
  MidpointMomentum: "Tracks token midpoint trends over recent ticks. If a token\u2019s midpoint is rising with enough momentum above a threshold, buys that side. Trend-following on market sentiment.",
  LateSniper: "Waits until late in the window when the outcome is more predictable, then buys the likely winner. Only enters when the token midpoint shows high confidence and the price isn\u2019t too expensive. Trades accuracy for lower profit per bet.",
  LateSniperTP: "LateSniper variant with take-profit. Same late entry, but sells early if the midpoint rises above a take-profit threshold (default 15%) rather than holding to resolution. Locks in gains on winners.",
  FadeTheMarket: "Mean reversion contrarian play. If a token midpoint spikes to an extreme early in the window, bets against it \u2014 expecting the market overreacted and the price will revert.",
  BargainSniper: "Buys cheap tokens early in the session when they\u2019re in a target price range, then manages with stop-loss and take-profit. Overrides take-profit when the market gap is wide enough to let winners ride.",
  BuyTrend: "Checks recent session history for a BTC price trend. If enough sessions confirm a directional move exceeding a threshold, buys in that direction at the start of the session.",
  EmaCrossover: "Classic dual EMA crossover on BTC price. When the fast EMA crosses above the slow, buys Up; when it crosses below, sells and buys Down. Can flip positions multiple times per session.",
  OlsSlope: "Fits a rolling linear regression to recent BTC prices. Enters when the slope (trend strength in bps/tick) exceeds a threshold, exits when slope reverses. Can re-enter on new trends.",
  Scalper: "Mean reversion on token midpoints. Buys when a token\u2019s midpoint dips below its rolling average, then exits on a profit target or stop-loss. Can make multiple quick round-trips per session.",
  TrailingStopMomentum: "Enters on BTC momentum, then manages the position with a trailing stop. Uses a fixed stop initially, switches to trailing after minimum profit is reached. Can re-enter after a cooldown period.",
};

const STRATEGY_DEFAULTS: Record<string, string> = {
  AlwaysBuyUp: "bet_fraction=1.0",
  AlwaysBuyDown: "bet_fraction=1.0",
  MomentumEntry: "entry_window=0.2,price_threshold_bps=25.0,bet_fraction=0.5",
  MidpointMomentum: "lookback_ticks=5,midpoint_threshold=0.55,momentum_threshold=0.03,entry_deadline=0.6,bet_fraction=0.5",
  LateSniper: "min_elapsed=0.7,confidence_threshold=0.70,max_price=0.85,bet_fraction=1.0",
  LateSniperTP: "min_elapsed=0.7,confidence_threshold=0.68,max_price=0.9,stop_loss_pct=0.1,take_profit_pct=0.15",
  FadeTheMarket: "overreaction_threshold=0.70,max_elapsed=0.3,bet_fraction=0.5",
  BargainSniper: "min_price=0.28,max_price=0.36,entry_window=0.20,stop_loss_pct=0.20,take_profit_pct=0.30",
  BuyTrend: "min_sessions=5,lookback_minutes=60,min_change_bps=5.0,min_direction_pct=0.75",
  EmaCrossover: "fast_span=25,slow_span=150,bet_fraction=0.3,max_trades=10",
  OlsSlope: "window_size=50,slope_threshold_bps=0.02,exit_slope_bps=0.0,bet_fraction=0.4,max_trades=10",
  Scalper: "lookback=50,entry_deviation=0.02,profit_target=0.02,stop_loss=0.05,bet_fraction=0.3,max_trades=12",
  TrailingStopMomentum: "momentum_bps=5,trail_pct=0.03,min_profit=0.02,fixed_stop=0.04,cooldown_ticks=200,bet_fraction=0.4,max_trades=8",
};

interface Props {
  botStatus: BotStatus | null;
  onRefresh: () => void;
  tradeRefresh: number;
}

export function BotPanel({ botStatus, onRefresh, tradeRefresh }: Props) {
  const [strategy, setStrategy] = useState("LateSniper");
  const [params, setParams] = useState(STRATEGY_DEFAULTS["LateSniper"] ?? "");
  const [cash, setCash] = useState("100");
  const [dryRun, setDryRun] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [editingCash, setEditingCash] = useState(false);
  const [cashInput, setCashInput] = useState("");

  const strategies = botStatus?.strategies ?? [];
  const isRunning = botStatus?.running ?? false;
  const isTrading = strategies.length > 0;

  async function handleTradeOn() {
    setLoading(true);
    setMessage(null);
    const parsedParams: Record<string, string> = {};
    if (params.trim()) {
      for (const pair of params.split(",")) {
        const [k, v] = pair.split("=").map((s) => s.trim());
        if (k && v) parsedParams[k] = v;
      }
    }
    const res = await tradeBotOn({
      strategy,
      params: parsedParams,
      cash: parseFloat(cash) || 100,
      dry_run: dryRun,
    });
    if (res.ok) {
      setMessage("Trading enabled");
    } else {
      setMessage(res.error || "Failed");
    }
    setLoading(false);
    onRefresh();
  }

  async function handleTradeOff(strategyName?: string) {
    setLoading(true);
    setMessage(null);
    const res = await tradeBotOff(strategyName);
    if (res.ok) {
      setMessage(strategyName ? `${strategyName} removed` : "Trading disabled");
    } else {
      setMessage(res.error || "Failed");
    }
    setLoading(false);
    onRefresh();
  }

  function startEditCash() {
    // For single strategy, edit that one; for multiple, don't use inline edit
    if (strategies.length === 1) {
      setCashInput(strategies[0].cash.toFixed(2));
      setEditingCash(true);
    }
  }

  async function handleSetCash(strategyName?: string) {
    const val = parseFloat(cashInput);
    if (isNaN(val) || val <= 0) {
      setMessage("Invalid cash amount");
      return;
    }
    setLoading(true);
    setMessage(null);
    const res = await setBotCash(val, strategyName);
    if (res.ok) {
      setMessage(`Cash updated to $${val.toFixed(2)}`);
    } else {
      setMessage(res.error || "Failed to update cash");
    }
    setEditingCash(false);
    setLoading(false);
    onRefresh();
  }

  async function handleStop() {
    setLoading(true);
    setMessage(null);
    const res = await stopBot();
    setMessage(res.ok ? "Stop requested" : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  // --- Start app form state ---
  const [slugs, setSlugs] = useState<SlugInfo[]>([]);
  const [selectedSlug, setSelectedSlug] = useState("");

  useEffect(() => {
    if (!isRunning) {
      fetchSlugs().then((s) => {
        setSlugs(s);
        if (s.length > 0 && !selectedSlug) {
          setSelectedSlug(s[0].slug_prefix);
        }
      });
    }
  }, [isRunning]);

  async function handleStartApp() {
    const slug = slugs.find((s) => s.slug_prefix === selectedSlug);
    if (!selectedSlug) {
      setMessage("Select a market");
      return;
    }
    setLoading(true);
    setMessage(null);
    const res = await startBot({
      slug_prefix: selectedSlug,
      window: slug?.window_duration_seconds,
    });
    if (res.ok) {
      setMessage("App started (PID " + res.pid + ")");
    } else {
      setMessage(res.error || "Failed to start");
    }
    setLoading(false);
    // Give the app a moment to write app.json, then refresh status
    setTimeout(onRefresh, 2500);
  }

  const totalCash = strategies.reduce((sum, s) => sum + s.cash, 0);

  return (
    <div>
      <h2>Bot Control</h2>

      <div className="bot-status-section">
        <div className="meta-grid">
          <div className="meta-card">
            <div className="meta-label">Status</div>
            <div className="meta-value">
              <span className={`bot-status-dot ${isRunning ? "running" : "stopped"}`} />
              {isRunning ? "Running" : "Stopped"}
            </div>
          </div>
          {isRunning && (
            <>
              <div className="meta-card">
                <div className="meta-label">Trading</div>
                <div className="meta-value">
                  {isTrading ? (
                    <>
                      {strategies.length === 1
                        ? strategies[0].name
                        : `${strategies.length} strategies`}
                      {strategies.length === 1 && strategies[0].dry_run && (
                        <span className="dry-run-tag">DRY RUN</span>
                      )}
                    </>
                  ) : (
                    <span className="subtitle">Off</span>
                  )}
                </div>
              </div>
              <div
                className={`meta-card${isTrading && strategies.length === 1 ? " meta-card-editable" : ""}`}
                onClick={() => !editingCash && isTrading && strategies.length === 1 && startEditCash()}
              >
                <div className="meta-label">
                  Cash
                  {isTrading && strategies.length === 1 && !editingCash && (
                    <span className="edit-hint">click to edit</span>
                  )}
                </div>
                {editingCash ? (
                  <div className="cash-edit">
                    <input
                      type="number"
                      value={cashInput}
                      onChange={(e) => setCashInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleSetCash(strategies.length === 1 ? strategies[0].name : undefined);
                        if (e.key === "Escape") setEditingCash(false);
                      }}
                      autoFocus
                      min="0"
                      step="0.01"
                      onClick={(e) => e.stopPropagation()}
                    />
                    <div className="cash-edit-btns">
                      <button
                        className="cash-btn save"
                        onClick={(e) => { e.stopPropagation(); handleSetCash(strategies.length === 1 ? strategies[0].name : undefined); }}
                        disabled={loading}
                      >Save</button>
                      <button className="cash-btn cancel" onClick={(e) => { e.stopPropagation(); setEditingCash(false); }}>Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div className="meta-value">
                    {isTrading ? (
                      <>
                        ${totalCash.toFixed(2)}
                        {strategies.length === 1 && strategies[0].session_cap != null && (
                          <span className="subtitle" style={{ marginLeft: 4, fontSize: 11 }}>
                            (${strategies[0].session_cap!.toFixed(0)}/session)
                          </span>
                        )}
                        {strategies.length === 1 && strategies[0].per_session_cash != null && !strategies[0].session_cap && (
                          <span className="subtitle" style={{ marginLeft: 4, fontSize: 11 }}>
                            (reset ${strategies[0].per_session_cash!.toFixed(0)}/session)
                          </span>
                        )}
                      </>
                    ) : "\u2014"}
                  </div>
                )}
              </div>
              <div className="meta-card">
                <div className="meta-label">Sessions</div>
                <div className="meta-value">{botStatus?.session_count ?? 0}</div>
              </div>
              <div className="meta-card">
                <div className="meta-label">Slug</div>
                <div className="meta-value" style={{ fontSize: 13 }}>
                  {botStatus?.slug_prefix || "\u2014"}
                </div>
              </div>
            </>
          )}
        </div>

        {isTrading && (
          <div className="strategies-list" style={{ marginTop: 12 }}>
            {strategies.map((s) => (
              <div key={s.name} className="meta-grid" style={{ marginBottom: 4 }}>
                <div className="meta-card" style={{ flex: 2 }}>
                  <div className="meta-label">Strategy</div>
                  <div className="meta-value">
                    {s.name}
                    {s.dry_run && <span className="dry-run-tag">DRY RUN</span>}
                  </div>
                </div>
                <div className="meta-card">
                  <div className="meta-label">Cash</div>
                  <div className="meta-value">
                    ${s.cash.toFixed(2)}
                    {s.session_cap != null && (
                      <span className="subtitle" style={{ marginLeft: 4, fontSize: 11 }}>
                        (${s.session_cap.toFixed(0)}/session)
                      </span>
                    )}
                    {s.per_session_cash != null && !s.session_cap && (
                      <span className="subtitle" style={{ marginLeft: 4, fontSize: 11 }}>
                        (reset ${s.per_session_cash.toFixed(0)}/session)
                      </span>
                    )}
                  </div>
                </div>
                <div className="meta-card" style={{ flex: 0 }}>
                  <button
                    className="bot-btn bot-btn-stop-trading"
                    style={{ fontSize: 11, padding: "2px 8px", marginTop: 4 }}
                    onClick={() => handleTradeOff(s.name)}
                    disabled={loading}
                  >
                    Remove
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {isRunning && (
        <div className="bot-controls">
          <div className="bot-form">
            <h3>{isTrading ? "Add Strategy" : "Start Trading"}</h3>
            <div className="bot-form-row">
              <label>
                Strategy
                <select value={strategy} onChange={(e) => { setStrategy(e.target.value); setParams(STRATEGY_DEFAULTS[e.target.value] ?? ""); }}>
                  {Object.keys(STRATEGIES).map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              {STRATEGIES[strategy] && (
                <p className="strategy-description">{STRATEGIES[strategy]}</p>
              )}
            </div>
            <div className="bot-form-row">
              <label>
                Params <span className="subtitle">(key=val,key=val)</span>
                <input
                  type="text"
                  value={params}
                  onChange={(e) => setParams(e.target.value)}
                  placeholder="min_elapsed=0.7"
                />
              </label>
            </div>
            <div className="bot-form-row">
              <label>
                Cash ($)
                <input
                  type="number"
                  value={cash}
                  onChange={(e) => setCash(e.target.value)}
                  min="1"
                  step="1"
                />
              </label>
            </div>
            <div className="bot-form-row">
              <label className="bot-checkbox">
                <input
                  type="checkbox"
                  checked={dryRun}
                  onChange={(e) => setDryRun(e.target.checked)}
                />
                Dry Run (simulated orders)
              </label>
            </div>
            <button
              className="bot-btn bot-btn-start"
              onClick={handleTradeOn}
              disabled={loading}
            >
              {isTrading ? "Add Strategy" : "Start Trading"}
            </button>
          </div>

          <div className="bot-actions">
            {isTrading && (
              <button
                className="bot-btn bot-btn-stop-trading"
                onClick={() => handleTradeOff()}
                disabled={loading}
              >
                {strategies.length > 1 ? "Stop All Trading" : "Stop Trading"}
              </button>
            )}
            <button
              className="bot-btn bot-btn-stop"
              onClick={handleStop}
              disabled={loading}
            >
              Stop App
            </button>
          </div>
        </div>
      )}

      {!isRunning && (
        <div className="bot-controls">
          <div className="bot-form">
            <h3>Start App</h3>
            <div className="bot-form-row">
              <label>
                Market
                <select
                  value={selectedSlug}
                  onChange={(e) => setSelectedSlug(e.target.value)}
                >
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
              onClick={handleStartApp}
              disabled={loading || !selectedSlug}
            >
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
