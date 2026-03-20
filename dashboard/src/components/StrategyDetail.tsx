"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { trpc } from "@/trpc/client";
import type { TradeLogEntry } from "@/types";
import { fmtTimeOnly, fmtDateOnly } from "@/utils/formatTime";
import { PnlCard } from "./PnlCard";

export function StrategyDetail({ market, strategy }: { market: string; strategy: string }) {
  const router = useRouter();

  const dataQuery = trpc.strategy.stats.useQuery(
    { market, strategy },
    { refetchInterval: 10000 }
  );

  const data = dataQuery.data;
  const loading = dataQuery.isLoading;

  if (loading) return <p className="text-dim text-sm">Loading...</p>;
  if (!data) return <p className="text-dim text-sm">No data for {strategy} on {market}.</p>;

  const { stats, sessions, trades, resolutions, version_summary } = data;

  const now = Date.now();
  const cutoff24h = now - 24 * 60 * 60 * 1000;
  const cutoff1h = now - 60 * 60 * 1000;
  const resolvedWithTrades = sessions.filter((s: any) => s.trades > 0 && !s._active);
  const pnl24h = resolvedWithTrades
    .filter((s: any) => new Date(s.timestamp).getTime() >= cutoff24h)
    .reduce((sum: number, s: any) => sum + s.pnl, 0);
  const pnl1h = resolvedWithTrades
    .filter((s: any) => new Date(s.timestamp).getTime() >= cutoff1h)
    .reduce((sum: number, s: any) => sum + s.pnl, 0);

  const tradedSessions = sessions.filter((s: any) => s.trades > 0 || s._active);
  let cumPnl = 0;
  const equityCurve = resolvedWithTrades.map((s: any) => {
    cumPnl += s.pnl;
    return { ...s, cumPnl };
  });

  return (
    <div>
      <div className="mb-5">
        <Link href="/home" className="text-accent no-underline text-[13px] hover:underline">
          &larr; Trading
        </Link>
      </div>

      <h2 className="text-xl font-semibold mb-1">
        {strategy}
        {stats.current_version && (
          <span className="text-xs font-medium ml-2 px-2 py-0.5 rounded bg-card border border-border text-dim align-middle">
            v{stats.current_version}
          </span>
        )}
      </h2>
      <p className="text-dim text-sm mb-5">{market}</p>

      <PnlCard overall={stats.total_pnl} last24h={pnl24h} last1h={pnl1h} />

      <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-6">
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Sharpe</div>
          <div className="text-base font-semibold">{stats.sharpe.toFixed(3)}</div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Entry Rate</div>
          <div className="text-base font-semibold">{stats.entry_rate}%</div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Win Rate</div>
          <div className="text-base font-semibold">{stats.win_rate}%</div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Record</div>
          <div className="text-base font-semibold">
            <span className="text-up">{stats.wins}W</span>
            {" / "}
            <span className="text-down">{stats.losses}L</span>
            {" / "}
            <span className="text-dim">{stats.total_sessions - stats.traded_sessions}S</span>
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Sessions</div>
          <div className="text-base font-semibold">{stats.traded_sessions} traded / {stats.total_sessions}</div>
        </div>
        {stats.total_fees > 0 && (
          <div className="bg-card border border-border rounded-lg px-4 py-3">
            <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Fees Paid</div>
            <div className="text-base font-semibold text-down">-${stats.total_fees.toFixed(2)}</div>
          </div>
        )}
        {stats.current_cash != null && (
          <div className="bg-card border border-border rounded-lg px-4 py-3">
            <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Cash</div>
            <div className="text-base font-semibold">${stats.current_cash.toFixed(2)}</div>
          </div>
        )}
      </div>

      {equityCurve.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">Equity Curve (Cumulative PnL)</h3>
          <EquityBar points={equityCurve} market={market} />
        </div>
      )}

      {Object.keys(version_summary).length > 0 && !(Object.keys(version_summary).length === 1 && version_summary["unknown"]) && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">Version History</h3>
          <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto max-h-[480px] overflow-y-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Version</th>
                  <th>Sessions</th>
                  <th>Record</th>
                  <th>PnL</th>
                  <th>First Seen</th>
                  <th>Last Seen</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(version_summary)
                  .sort(([ka], [kb]) => {
                    if (ka === "unknown") return 1;
                    if (kb === "unknown") return -1;
                    return Number(kb) - Number(ka);
                  })
                  .map(([version, v]: [string, any]) => (
                  <tr key={version} className={version === stats.current_version ? "bg-accent/[0.08]" : ""}>
                    <td>
                      <span className={`px-1.5 py-px rounded text-xs ${
                        version === stats.current_version
                          ? "bg-accent text-white border-none"
                          : "bg-card text-dim border border-border"
                      }`}>
                        {version === "unknown" ? "pre-versioning" : `v${version}`}
                      </span>
                    </td>
                    <td>{v.traded} / {v.sessions}</td>
                    <td>
                      <span className="text-up">{v.wins}W</span>{" / "}<span className="text-down">{v.losses}L</span>
                    </td>
                    <td>
                      <span className={v.pnl > 0 ? "text-up" : v.pnl < 0 ? "text-down" : ""}>
                        {v.pnl >= 0 ? "+" : ""}{v.pnl.toFixed(2)}
                      </span>
                    </td>
                    <td className="font-mono text-xs">
                      <SessionLink timestamp={v.first} market={market} />
                    </td>
                    <td className="font-mono text-xs">
                      <SessionLink timestamp={v.last} market={market} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="mb-6">
        <h3 className="text-sm font-semibold mb-3">Session Results ({tradedSessions.length})</h3>
        {tradedSessions.length === 0 ? (
          <p className="text-dim text-sm">No traded sessions yet.</p>
        ) : (
          <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto max-h-[480px] overflow-y-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>#</th>
                  <th>Ver</th>
                  <th>Outcome</th>
                  <th>Trades</th>
                  <th>PnL</th>
                  <th>Credit</th>
                  <th>Cash</th>
                </tr>
              </thead>
              <tbody>
                {[...tradedSessions].reverse().map((s: any, i: number) => (
                  <tr key={i}>
                    <td className="font-mono text-xs">
                      <SessionLink timestamp={s._window_start_utc || s.timestamp} market={market} />
                    </td>
                    <td>{s.session_number != null ? s.session_number : "\u2014"}</td>
                    <td className="text-[11px] text-dim">
                      {s.strategy_version ? `v${s.strategy_version}` : "\u2014"}
                    </td>
                    <td>
                      <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                        s._active ? "bg-accent/20 text-accent animate-pulse" :
                        s.pnl > 0 ? "bg-up/15 text-up" : s.pnl < 0 ? "bg-down/15 text-down" : "bg-[rgba(136,132,216,0.15)] text-dim"
                      }`}>
                        {s._active ? "ACTIVE" : s.outcome}
                      </span>
                    </td>
                    <td>{s.trades}</td>
                    <td>
                      <span className={s.pnl > 0 ? "text-up" : s.pnl < 0 ? "text-down" : ""}>
                        {s.pnl >= 0 ? "+" : ""}{s.pnl.toFixed(2)}
                      </span>
                    </td>
                    <td>{s.credit > 0 ? `+${s.credit.toFixed(2)}` : "\u2014"}</td>
                    <td>{s.cash != null ? `$${s.cash.toFixed(2)}` : "\u2014"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mb-6">
        <h3 className="text-sm font-semibold mb-3">Log ({trades.length + resolutions.length})</h3>
        {trades.length === 0 && resolutions.length === 0 ? (
          <p className="text-dim text-sm">No log entries yet.</p>
        ) : (
          <StrategyLog trades={trades} resolutions={resolutions} />
        )}
      </div>
    </div>
  );
}

function tsToSessionFile(ts: string): string {
  const d = new Date(ts);
  return d.toISOString().replace(/\.\d+Z$/, "Z").replace(/:/g, "-") + ".json";
}

function SessionLink({ timestamp, market }: { timestamp: string; market: string }) {
  const file = tsToSessionFile(timestamp);
  return (
    <Link href={`/sessions/${market}/${file}`} className="group no-underline hover:text-accent transition-colors">
      <span className="text-dim group-hover:text-accent mr-1.5 text-[11px] transition-colors">{fmtDateOnly(timestamp)}</span>
      {fmtTimeOnly(timestamp)}
    </Link>
  );
}

function EquityBar({ points, market }: { points: Array<{ timestamp: string; pnl: number; cumPnl: number; strategy_version?: string; _window_start_utc?: string | null }>; market: string }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const router = useRouter();

  if (points.length === 0) return null;
  const maxAbs = Math.max(...points.map((p) => Math.abs(p.cumPnl)), 0.01);

  const versionBoundaries = new Set<number>();
  for (let i = 1; i < points.length; i++) {
    const prev = points[i - 1].strategy_version;
    const curr = points[i].strategy_version;
    if (prev && curr && prev !== curr) versionBoundaries.add(i);
  }

  const hp = hovered != null ? points[hovered] : null;

  return (
    <div className="relative">
      {hp && (
        <div className="absolute -top-10 left-1/2 -translate-x-1/2 bg-card border border-border rounded px-2.5 py-1 text-xs font-mono whitespace-nowrap z-10 pointer-events-none shadow-md">
          <span className="text-dim mr-1.5">{fmtDateOnly(hp.timestamp)} {fmtTimeOnly(hp.timestamp)}</span>
          <span className={hp.pnl >= 0 ? "text-up" : "text-down"}>
            {hp.pnl >= 0 ? "+" : ""}{hp.pnl.toFixed(2)}
          </span>
          <span className="text-dim mx-1">/</span>
          <span className={hp.cumPnl >= 0 ? "text-up" : "text-down"}>
            cum {hp.cumPnl >= 0 ? "+" : ""}{hp.cumPnl.toFixed(2)}
          </span>
          {hp.strategy_version && <span className="text-dim ml-1.5">v{hp.strategy_version}</span>}
        </div>
      )}
      <div className="flex gap-px h-24 bg-card rounded-md px-2 py-1 border border-border">
        {points.map((p, i) => {
          const h = Math.max(Math.abs(p.cumPnl) / maxAbs * 40, 1);
          const isBoundary = versionBoundaries.has(i);
          const versionLabel = isBoundary ? `v${p.strategy_version}` : "";
          const ts = p._window_start_utc || p.timestamp;
          return (
            <div
              key={i}
              className={`flex-1 min-w-0.5 max-w-3 flex flex-col relative cursor-pointer${isBoundary ? " ml-1" : ""}`}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered(null)}
              onClick={() => router.push(`/sessions/${market}/${tsToSessionFile(ts)}`)}
            >
              {isBoundary && (
                <>
                  <div className="absolute left-[-3px] top-0 bottom-0 w-px bg-accent/50" />
                  <div className="absolute left-[-3px] top-[-2px] text-[8px] text-accent font-semibold whitespace-nowrap leading-none">{versionLabel}</div>
                </>
              )}
              <div className="flex-1 flex items-end justify-center">
                {p.cumPnl >= 0 && <div className={`w-full rounded-sm bg-up transition-opacity ${hovered === i ? "opacity-100" : "opacity-80"}`} style={{ height: h }} />}
              </div>
              <div className="flex-1 flex items-start justify-center">
                {p.cumPnl < 0 && <div className={`w-full rounded-sm bg-down transition-opacity ${hovered === i ? "opacity-100" : "opacity-80"}`} style={{ height: h }} />}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StrategyLog({ trades, resolutions }: { trades: TradeLogEntry[]; resolutions: TradeLogEntry[] }) {
  type LogEntry = { ts: string; kind: "trade" | "resolution"; data: TradeLogEntry };
  const entries: LogEntry[] = [
    ...trades.map((t): LogEntry => ({ ts: t.timestamp || t.session_start || "", kind: "trade", data: t })),
    ...resolutions.map((r): LogEntry => ({ ts: r.timestamp || r.session_start || "", kind: "resolution", data: r })),
  ].sort((a, b) => b.ts.localeCompare(a.ts));

  function fmtTrade(t: TradeLogEntry): string {
    const side = t.side === "buy_up" ? "BUY UP" : t.side === "buy_down" ? "BUY DOWN"
      : t.side === "sell_up" ? "SELL UP" : t.side === "sell_down" ? "SELL DOWN" : t.side.toUpperCase();
    const status = t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM";
    const parts = [side, `${t.tokens} @ $${t.price.toFixed(2)}`, `$${t.amount.toFixed(2)}`];
    if (t.fee != null) parts.push(`fee $${t.fee.toFixed(4)}`);
    if (t.elapsed_seconds != null) parts.push(`${t.elapsed_seconds.toFixed(1)}s`);
    parts.push(`[${status}]`);
    if (t.error) parts.push(t.error);
    return parts.join("  ");
  }

  function fmtResolution(r: TradeLogEntry): string {
    const parts = [`RESOLVE ${r.outcome ?? "?"}`, `pnl ${(r.pnl ?? 0) >= 0 ? "+" : ""}${(r.pnl ?? 0).toFixed(2)}`];
    if (r.credit && r.credit > 0) parts.push(`credit +${r.credit.toFixed(2)}`);
    if (r.trades != null) parts.push(`${r.trades} trades`);
    if (r.starting_cash != null && r.final_cash != null) parts.push(`$${r.starting_cash.toFixed(2)} \u2192 $${r.final_cash.toFixed(2)}`);
    return parts.join("  ");
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden max-h-[480px] overflow-y-auto px-3 py-2">
      {entries.map((e, i) => {
        const isRes = e.kind === "resolution";
        const pnl = e.data.pnl ?? 0;
        return (
          <div key={i} className={`font-mono text-xs leading-relaxed whitespace-nowrap ${isRes ? "border-t border-border/50 pt-1 mt-1" : ""}`}>
            <span className="text-dim">{fmtTimeOnly(e.ts)}</span>
            {"  "}
            {isRes ? (
              <span className={pnl > 0 ? "text-up" : pnl < 0 ? "text-down" : "text-dim"}>
                {fmtResolution(e.data)}
              </span>
            ) : (
              <span className={e.data.success === false ? "text-down" : ""}>
                {fmtTrade(e.data)}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
