"use client";

import { useCallback, useEffect, useState } from "react";
import { trpc } from "@/trpc/client";

const DEFAULTS = {
  interval_minutes: 30,
  eval_sessions: 30,
  min_sharpe: 0.2,
  starting_cash: 100,
};

export function OptimizerPanel() {
  const [error, setError] = useState<string | null>(null);
  const [interval, setInterval_] = useState(DEFAULTS.interval_minutes);
  const [evalSessions, setEvalSessions] = useState(DEFAULTS.eval_sessions);
  const [minSharpe, setMinSharpe] = useState(DEFAULTS.min_sharpe);
  const [startingCash, setStartingCash] = useState(DEFAULTS.starting_cash);
  const [configDirty, setConfigDirty] = useState(false);
  const [configLoaded, setConfigLoaded] = useState(false);

  const statusQuery = trpc.optimizer.status.useQuery(undefined, { refetchInterval: 10000 });
  const eventsQuery = trpc.optimizer.events.useQuery(undefined, { refetchInterval: 10000 });
  const configQuery = trpc.optimizer.config.useQuery();

  const startMutation = trpc.optimizer.start.useMutation();
  const stopMutation = trpc.optimizer.stop.useMutation();
  const updateConfigMutation = trpc.optimizer.updateConfig.useMutation();

  const utils = trpc.useUtils();

  const status = statusQuery.data;
  const events = eventsQuery.data ?? [];

  useEffect(() => {
    if (configQuery.data && !configLoaded) {
      const cfg = configQuery.data as any;
      if (cfg.interval_minutes != null) setInterval_(cfg.interval_minutes);
      if (cfg.eval_sessions != null) setEvalSessions(cfg.eval_sessions);
      if (cfg.min_sharpe != null) setMinSharpe(cfg.min_sharpe);
      if (cfg.starting_cash != null) setStartingCash(cfg.starting_cash);
      setConfigLoaded(true);
    }
  }, [configQuery.data, configLoaded]);

  const handleStart = async () => {
    setError(null);
    const res = await startMutation.mutateAsync({
      interval,
      eval_sessions: evalSessions,
      min_sharpe: minSharpe,
      starting_cash: startingCash,
    });
    if (!res.ok) setError(res.error ?? "Failed to start");
    setTimeout(() => utils.optimizer.status.invalidate(), 1000);
  };

  const handleStop = async () => {
    setError(null);
    const res = await stopMutation.mutateAsync();
    if (!(res as any).ok) setError((res as any).error ?? "Failed to stop");
    setTimeout(() => utils.optimizer.status.invalidate(), 1000);
  };

  const handleConfigSave = async () => {
    setError(null);
    const res = await updateConfigMutation.mutateAsync({
      interval_minutes: interval,
      eval_sessions: evalSessions,
      min_sharpe: minSharpe,
      starting_cash: startingCash,
    });
    if (res.ok) {
      setConfigDirty(false);
    } else {
      setError(res.error ?? "Failed to update config");
    }
  };

  if (statusQuery.isLoading) return <p className="text-dim text-sm">Loading optimizer...</p>;

  const isRunning = false; // Offline mode
  const state = "offline";

  return (
    <div>
      <div className="flex items-center gap-3 mb-6">
        <h2 className="text-xl font-semibold">Optimizer</h2>
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-dim" />
          <span className="text-[13px] text-dim">offline</span>
        </div>
        <div className="ml-auto flex gap-2">
          <button className="btn-primary" onClick={handleStart}>Run Sweep</button>
        </div>
      </div>

      {error && (
        <div className="bg-down/15 border border-down rounded-md px-4 py-2.5 mb-5 text-[13px]">
          {error}
        </div>
      )}

      <div className="bg-card rounded-lg p-5 mb-5 border border-border">
        <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">Configuration</h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-4 max-w-[600px]">
          <ConfigField label="Interval" unit="min" value={interval} min={1} onChange={(v) => { setInterval_(v); setConfigDirty(true); }} />
          <ConfigField label="Eval Sessions" value={evalSessions} min={5} onChange={(v) => { setEvalSessions(v); setConfigDirty(true); }} />
          <ConfigField label="Min Sharpe" value={minSharpe} step={0.05} onChange={(v) => { setMinSharpe(v); setConfigDirty(true); }} />
          <ConfigField label="Starting Cash" unit="$" value={startingCash} min={1} onChange={(v) => { setStartingCash(v); setConfigDirty(true); }} />
        </div>
        {configDirty && (
          <button className="btn-primary mt-4" onClick={handleConfigSave}>Save Config</button>
        )}
      </div>

      <div className="bg-card rounded-lg p-5 border border-border">
        <h3 className="text-[13px] font-semibold uppercase tracking-wide text-dim mb-4">
          Event Log
          <span className="opacity-50 font-normal ml-2">({events.length})</span>
        </h3>
        <div className="max-h-[500px] overflow-y-auto">
          {events.length === 0 ? (
            <div className="text-dim text-[13px] py-2">No events yet. Run a sweep to begin.</div>
          ) : (
            events.map((ev: any, i: number) => (
              <div key={i} className="flex gap-3 py-1.5 border-b border-border text-xs items-baseline last:border-b-0">
                <span className="text-dim shrink-0 font-mono text-[11px]">
                  {ev.timestamp ? new Date(ev.timestamp).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : ""}
                </span>
                <span className="font-semibold uppercase text-[10px] shrink-0 min-w-[70px] text-accent">
                  {ev.type}
                </span>
                <span className="opacity-80 overflow-hidden text-ellipsis whitespace-nowrap">
                  {ev.strategy ? `${ev.market ?? ""}/${ev.strategy}` : ""}
                </span>
              </div>
            ))
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
