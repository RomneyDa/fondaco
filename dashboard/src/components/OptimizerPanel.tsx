import { useCallback, useEffect, useState } from "react";
import {
  fetchOptimizerStatus,
  fetchOptimizerEvents,
  fetchOptimizerConfig,
  startOptimizer,
  stopOptimizer,
  updateOptimizerConfig,
} from "../api";
import type { OptimizerStatus, OptimizerEvent, OptimizerResult } from "../api";

const DEFAULTS = {
  interval_minutes: 30,
  lookback: 30,
  min_sharpe: 0.2,
  starting_cash: 100,
};

export function OptimizerPanel() {
  const [status, setStatus] = useState<OptimizerStatus | null>(null);
  const [events, setEvents] = useState<OptimizerEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [interval, setInterval_] = useState(DEFAULTS.interval_minutes);
  const [lookback, setLookback] = useState(DEFAULTS.lookback);
  const [minSharpe, setMinSharpe] = useState(DEFAULTS.min_sharpe);
  const [startingCash, setStartingCash] = useState(DEFAULTS.starting_cash);
  const [configDirty, setConfigDirty] = useState(false);
  const [configLoaded, setConfigLoaded] = useState(false);

  const refresh = useCallback(async () => {
    const [s, e] = await Promise.all([
      fetchOptimizerStatus(),
      fetchOptimizerEvents(),
    ]);
    if (s) {
      setStatus(s);
      if (!configLoaded) {
        if (s.interval_minutes) setInterval_(s.interval_minutes);
        if (s.lookback) setLookback(s.lookback);
        if (s.min_sharpe != null) setMinSharpe(s.min_sharpe);
      }
    }
    setEvents(e);
    setLoading(false);
  }, [configLoaded]);

  useEffect(() => {
    fetchOptimizerConfig().then((cfg) => {
      if (cfg.interval_minutes != null) setInterval_(cfg.interval_minutes);
      if (cfg.lookback != null) setLookback(cfg.lookback);
      if (cfg.min_sharpe != null) setMinSharpe(cfg.min_sharpe);
      if (cfg.starting_cash != null) setStartingCash(cfg.starting_cash);
      setConfigLoaded(true);
    });
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 10000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const handleStart = async () => {
    setError(null);
    const res = await startOptimizer({
      interval,
      lookback,
      min_sharpe: minSharpe,
      starting_cash: startingCash,
    });
    if (!res.ok) setError(res.error ?? "Failed to start");
    setTimeout(refresh, 1000);
  };

  const handleStop = async () => {
    setError(null);
    const res = await stopOptimizer();
    if (!res.ok) setError(res.error ?? "Failed to stop");
    setTimeout(refresh, 1000);
  };

  const handleConfigSave = async () => {
    setError(null);
    const res = await updateOptimizerConfig({
      interval_minutes: interval,
      lookback,
      min_sharpe: minSharpe,
      starting_cash: startingCash,
    });
    if (res.ok) {
      setConfigDirty(false);
    } else {
      setError(res.error ?? "Failed to update config");
    }
  };

  const isRunning = status?.running ?? false;
  const state = status?.state ?? "unknown";

  const stateColor =
    state === "optimizing"
      ? "bg-accent"
      : state === "scanning"
        ? "bg-btc"
        : isRunning
          ? "bg-up"
          : "bg-dim";

  const statePulse = state === "optimizing" ? "animate-pulse-dot" : "";

  if (loading) return <p className="text-dim text-sm">Loading optimizer...</p>;

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <h2 className="text-xl font-semibold">Optimizer</h2>
        <div className="flex items-center gap-2">
          <span className={`inline-block w-2 h-2 rounded-full ${stateColor} ${statePulse}`} />
          <span className="text-[13px] text-dim">
            {isRunning ? state : "stopped"}
          </span>
          {status?.pid && (
            <span className="text-[11px] text-dim">PID {status.pid}</span>
          )}
        </div>
        <div className="ml-auto flex gap-2">
          {!isRunning ? (
            <button className="btn-primary" onClick={handleStart}>Start</button>
          ) : (
            <button className="btn-danger" onClick={handleStop}>Stop</button>
          )}
        </div>
      </div>

      {error && (
        <div className="bg-down/15 border border-down rounded-md px-4 py-2.5 mb-5 text-[13px]">
          {error}
        </div>
      )}

      {/* Config */}
      <div className="bg-card rounded-lg p-5 mb-5 border border-border">
        <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">Configuration</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 max-w-[600px]">
          <ConfigField
            label="Interval"
            unit="min"
            value={interval}
            min={1}
            onChange={(v) => { setInterval_(v); setConfigDirty(true); }}
          />
          <ConfigField
            label="Lookback"
            unit="sessions"
            value={lookback}
            min={5}
            onChange={(v) => { setLookback(v); setConfigDirty(true); }}
          />
          <ConfigField
            label="Min Sharpe"
            value={minSharpe}
            step={0.05}
            onChange={(v) => { setMinSharpe(v); setConfigDirty(true); }}
          />
          <ConfigField
            label="Starting Cash"
            unit="$"
            value={startingCash}
            min={1}
            onChange={(v) => { setStartingCash(v); setConfigDirty(true); }}
          />
        </div>
        {configDirty && (
          <button className="btn-primary mt-4" onClick={handleConfigSave}>
            Save Config
          </button>
        )}
      </div>

      {/* Status details */}
      {isRunning && status && (
        <div className="bg-card rounded-lg p-5 mb-5 border border-border">
          <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">Status</h3>
          <div className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[13px]">
            {status.current_strategy && (
              <>
                <span className="text-dim">Optimizing</span>
                <span>{status.current_market}/{status.current_strategy}</span>
              </>
            )}
            {status.last_run && (
              <>
                <span className="text-dim">Last run</span>
                <span>{new Date(status.last_run).toLocaleString()}</span>
              </>
            )}
            {status.next_run && (
              <>
                <span className="text-dim">Next run</span>
                <span>{new Date(status.next_run).toLocaleString()}</span>
              </>
            )}
            {status.last_duration_seconds != null && (
              <>
                <span className="text-dim">Duration</span>
                <span>{status.last_duration_seconds.toFixed(1)}s</span>
              </>
            )}
            {status.strategies_optimized != null && (
              <>
                <span className="text-dim">Strategies</span>
                <span>{status.strategies_optimized}</span>
              </>
            )}
          </div>
        </div>
      )}

      {/* Results */}
      {status?.last_results && status.last_results.length > 0 && (
        <div className="bg-card rounded-lg p-5 mb-5 border border-border">
          <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">Last Results</h3>
          <div className="overflow-x-auto">
            <table className="data-table strategy-tbl text-xs">
              <thead>
                <tr>
                  <th>Market</th>
                  <th>Strategy</th>
                  <th>Action</th>
                  <th className="text-right">Sharpe</th>
                  <th className="text-right">Baseline</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {status.last_results.map((r, i) => (
                  <ResultRow key={i} result={r} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Event Log */}
      <div className="bg-card rounded-lg p-5 border border-border">
        <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">
          Event Log
          <span className="opacity-50 font-normal ml-2">({events.length})</span>
        </h3>
        <div className="max-h-[500px] overflow-y-auto">
          {events.length === 0 ? (
            <div className="text-dim text-[13px] py-2">
              No events yet. Start the optimizer to begin.
            </div>
          ) : (
            events.map((ev, i) => <EventRow key={i} event={ev} />)
          )}
        </div>
      </div>
    </div>
  );
}

function ConfigField({ label, unit, value, min, step, onChange }: {
  label: string;
  unit?: string;
  value: number;
  min?: number;
  step?: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[10px] font-semibold uppercase tracking-tight text-dim">{label}</label>
      <div className="flex items-center gap-1.5">
        <input
          type="number"
          value={value}
          min={min}
          step={step}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full"
        />
        {unit && <span className="text-[10px] text-dim shrink-0">{unit}</span>}
      </div>
    </div>
  );
}

function StatusRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <span className="text-dim">{label}</span>
      <span>{value}</span>
    </>
  );
}

function ResultRow({ result }: { result: OptimizerResult }) {
  const actionColor =
    result.action === "update"
      ? "text-up"
      : result.action === "disable"
        ? "text-down"
        : result.action === "keep"
          ? "text-dim"
          : "text-btc";

  const sharpe = result.new_sharpe ?? result.best_sharpe ?? result.sharpe ?? 0;
  const baselineSharpe = result.baseline_sharpe ?? 0;

  let details = "";
  if (result.action === "update" && result.new_params) {
    const params = Object.entries(result.new_params)
      .map(([k, v]) => `${k}=${typeof v === "number" ? v.toFixed(3) : v}`)
      .join(", ");
    details = params;
    if (result.new_ratio) {
      details += ` | ratio=${result.new_ratio.ratio}:${result.new_ratio.max}`;
    }
  } else if (result.action === "disable") {
    details = result.reason ?? "";
  } else if (result.action === "skip") {
    details = result.reason ?? "";
  }

  return (
    <tr>
      <td>{result.market}</td>
      <td>{result.strategy}</td>
      <td>
        <span className={`${actionColor} font-semibold uppercase text-[11px]`}>
          {result.action}
        </span>
      </td>
      <td className="text-right mono">{sharpe.toFixed(3)}</td>
      <td className="text-right mono text-dim">{baselineSharpe.toFixed(3)}</td>
      <td className="text-[11px] opacity-70 max-w-[300px] overflow-hidden text-ellipsis whitespace-nowrap">
        {details}
      </td>
    </tr>
  );
}

function EventRow({ event }: { event: OptimizerEvent }) {
  const typeColors: Record<string, string> = {
    started: "text-up",
    stopped: "text-down",
    cycle_start: "text-accent",
    cycle_end: "text-accent",
    optimize_start: "text-btc",
    baseline: "text-txt",
    grid_search: "text-txt",
    grid_result: "text-accent",
    update: "text-up",
    disable: "text-down",
    keep: "text-dim",
    skip: "text-btc",
  };

  const color = typeColors[event.type] ?? "text-txt";
  const time = new Date(event.timestamp).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  const fmtNum = (v: number, d = 3) => (typeof v === "number" ? v.toFixed(d) : String(v));
  const fmtParams = (params: Record<string, unknown>) =>
    Object.entries(params)
      .map(([k, v]) => `${k}=${typeof v === "number" ? fmtNum(v) : v}`)
      .join(", ");

  const strat = event.strategy ? `${event.market ?? ""}/${event.strategy}` : "";

  let detail = "";
  if (event.type === "optimize_start") {
    detail = `${strat}`;
    if (event.current_params && Object.keys(event.current_params).length > 0) {
      detail += ` | current: ${fmtParams(event.current_params)}`;
    }
  } else if (event.type === "baseline") {
    detail = `${strat} | sharpe=${fmtNum(event.sharpe ?? 0)} pnl=$${fmtNum(event.pnl ?? 0, 2)} win=${fmtNum(event.win_rate ?? 0, 1)}% (${event.sessions_used ?? 0} sessions)`;
  } else if (event.type === "grid_search") {
    detail = `${strat} | ${event.variants ?? 0} variants`;
    if (event.grid && Object.keys(event.grid).length > 0) {
      const dims = Object.entries(event.grid)
        .map(([k, v]) => `${k}(${Array.isArray(v) ? v.length : "?"})`).join(", ");
      detail += ` | ${dims}`;
    }
  } else if (event.type === "grid_result") {
    detail = `${strat} | best sharpe=${fmtNum(event.best_sharpe ?? 0)} vs baseline=${fmtNum(event.baseline_sharpe ?? 0)}`;
    if (event.best_params) detail += ` | ${event.best_params}`;
    if (event.top_variants?.length) {
      const others = event.top_variants.slice(1);
      if (others.length) {
        detail += ` | #2: ${fmtNum(others[0].sharpe)}`;
        if (others.length > 1) detail += ` #3: ${fmtNum(others[1].sharpe)}`;
      }
    }
  } else if (event.type === "update" && event.new_sharpe != null) {
    detail = `${strat} sharpe: ${fmtNum(event.baseline_sharpe ?? 0)} → ${fmtNum(event.new_sharpe)}`;
    if (event.new_params) detail += ` | ${fmtParams(event.new_params)}`;
  } else if (event.type === "disable") {
    detail = `${strat} (${event.reason ?? "below_threshold"})`;
  } else if (event.type === "cycle_end") {
    detail = `${event.strategies_optimized ?? 0} strategies in ${fmtNum(event.duration_seconds ?? 0, 1)}s`;
  } else if (event.type === "keep") {
    detail = `${strat} (sharpe=${fmtNum(event.best_sharpe ?? event.baseline_sharpe ?? 0)})`;
  } else if (event.type === "skip") {
    detail = `${strat} (${event.reason ?? "skipped"})`;
  } else if (strat) {
    detail = strat;
  }

  return (
    <div className="flex gap-3 py-1.5 border-b border-border text-xs items-baseline last:border-b-0">
      <span className="text-dim shrink-0 font-mono text-[11px]">
        {time}
      </span>
      <span className={`${color} font-semibold uppercase text-[10px] shrink-0 min-w-[70px]`}>
        {event.type}
      </span>
      <span className="opacity-80 overflow-hidden text-ellipsis whitespace-nowrap">
        {detail}
      </span>
    </div>
  );
}
