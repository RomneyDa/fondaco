"use client";

/** Unified PnL display: all-time, 24h, 1h in a single card. */
export function PnlCard({ overall, last24h, last1h, dryRun }: {
  overall: number;
  last24h: number;
  last1h: number;
  dryRun?: { overall: number; last_24h: number; last_1h: number };
}) {
  return (
    <div className="flex gap-4 mb-5">
      <span className="flex items-baseline gap-1.5">
        <span className="text-xs text-dim font-medium">1h</span>
        <span className={`text-base font-bold tabular-nums ${last1h >= 0 ? "text-up" : "text-down"}`}>
          {last1h >= 0 ? "+" : ""}{last1h.toFixed(2)}
        </span>
      </span>
      <span className="flex items-baseline gap-1.5">
        <span className="text-xs text-dim font-medium">24h</span>
        <span className={`text-base font-bold tabular-nums ${last24h >= 0 ? "text-up" : "text-down"}`}>
          {last24h >= 0 ? "+" : ""}{last24h.toFixed(2)}
        </span>
      </span>
      <span className="flex items-baseline gap-1.5">
        <span className="text-xs text-dim font-medium">All</span>
        <span className={`text-base font-bold tabular-nums ${overall >= 0 ? "text-up" : "text-down"}`}>
          {overall >= 0 ? "+" : ""}{overall.toFixed(2)}
        </span>
      </span>
      {dryRun && (dryRun.overall !== 0 || dryRun.last_24h !== 0 || dryRun.last_1h !== 0) && (
        <span className="flex items-baseline gap-1.5 ml-2 pl-3 border-l border-border">
          <span className="text-[11px] font-semibold text-accent">DRY</span>
          <span className="text-sm tabular-nums text-accent opacity-70">
            {dryRun.overall >= 0 ? "+" : ""}{dryRun.overall.toFixed(2)}
          </span>
        </span>
      )}
    </div>
  );
}
