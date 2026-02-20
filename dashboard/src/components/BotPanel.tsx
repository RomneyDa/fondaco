import { useState } from "react";
import { fetchBotStatus, tradeBotOn, tradeBotOff, stopBot, setBotCash } from "../api";
import type { BotStatus } from "../types";

const STRATEGIES = [
  "AlwaysBuyUp",
  "AlwaysBuyDown",
  "MomentumEntry",
  "MidpointMomentum",
  "LateSniper",
  "FadeTheMarket",
  "EmaCrossover",
  "OlsSlope",
  "Scalper",
  "TrailingStopMomentum",
];

interface Props {
  botStatus: BotStatus | null;
  onRefresh: () => void;
}

export function BotPanel({ botStatus, onRefresh }: Props) {
  const [strategy, setStrategy] = useState("LateSniper");
  const [params, setParams] = useState("min_elapsed=0.7");
  const [cash, setCash] = useState("100");
  const [dryRun, setDryRun] = useState(true);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [editingCash, setEditingCash] = useState(false);
  const [cashInput, setCashInput] = useState("");

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

  async function handleTradeOff() {
    setLoading(true);
    setMessage(null);
    const res = await tradeBotOff();
    setMessage(res.ok ? "Trading disabled" : res.error || "Failed");
    setLoading(false);
    onRefresh();
  }

  function startEditCash() {
    setCashInput(botStatus?.cash?.toFixed(2) ?? "");
    setEditingCash(true);
  }

  async function handleSetCash() {
    const val = parseFloat(cashInput);
    if (isNaN(val) || val <= 0) {
      setMessage("Invalid cash amount");
      return;
    }
    setLoading(true);
    setMessage(null);
    const res = await setBotCash(val);
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

  const isRunning = botStatus?.running ?? false;
  const isTrading = botStatus?.trading_enabled ?? false;

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
                      {botStatus?.strategy || "—"}
                      {botStatus?.dry_run && (
                        <span className="dry-run-tag">DRY RUN</span>
                      )}
                    </>
                  ) : (
                    <span className="subtitle">Off</span>
                  )}
                </div>
              </div>
              <div className="meta-card meta-card-editable" onClick={() => !editingCash && isTrading && startEditCash()}>
                <div className="meta-label">Cash {isTrading && !editingCash && <span className="edit-hint">click to edit</span>}</div>
                {editingCash ? (
                  <div className="cash-edit">
                    <input
                      type="number"
                      value={cashInput}
                      onChange={(e) => setCashInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") handleSetCash();
                        if (e.key === "Escape") setEditingCash(false);
                      }}
                      autoFocus
                      min="0"
                      step="0.01"
                      onClick={(e) => e.stopPropagation()}
                    />
                    <div className="cash-edit-btns">
                      <button className="cash-btn save" onClick={(e) => { e.stopPropagation(); handleSetCash(); }} disabled={loading}>Save</button>
                      <button className="cash-btn cancel" onClick={(e) => { e.stopPropagation(); setEditingCash(false); }}>Cancel</button>
                    </div>
                  </div>
                ) : (
                  <div className="meta-value">
                    {botStatus?.cash != null ? `$${botStatus.cash.toFixed(2)}` : "—"}
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
                  {botStatus?.slug_prefix || "—"}
                </div>
              </div>
            </>
          )}
        </div>
      </div>

      {isRunning && (
        <div className="bot-controls">
          <div className="bot-form">
            <h3>Start Trading</h3>
            <div className="bot-form-row">
              <label>
                Strategy
                <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
                  {STRATEGIES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
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
              {isTrading ? "Update Strategy" : "Start Trading"}
            </button>
          </div>

          <div className="bot-actions">
            {isTrading && (
              <button
                className="bot-btn bot-btn-stop-trading"
                onClick={handleTradeOff}
                disabled={loading}
              >
                Stop Trading
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
        <div className="bot-offline-msg">
          <p className="subtitle">
            App is not running. Start it from the CLI:
          </p>
          <code>uv run python -m app start btc-updown-5m-</code>
        </div>
      )}

      {message && <div className="bot-message">{message}</div>}
    </div>
  );
}
