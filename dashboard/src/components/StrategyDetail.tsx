import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { fetchStrategyStats, fetchSessionStats } from "../api";
import type { StrategyStats, VersionSummary, SessionStat } from "../api";
import { fmtTimeOnly, fmtDateOnly } from "../utils/formatTime";
import { PnlCard } from "./PnlCard";

export function StrategyDetail() {
  const { market, strategy } = useParams<{ market: string; strategy: string }>();
  const [data, setData] = useState<StrategyStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [statsMap, setStatsMap] = useState<Map<string, SessionStat>>(new Map());

  useEffect(() => {
    if (!market || !strategy) return;
    setLoading(true);
    fetchStrategyStats(market, strategy).then((d) => {
      setData(d);
      setLoading(false);
    });
    // Fetch session stats for BTC return data
    fetchSessionStats(market).then((stats) => {
      const m = new Map<string, SessionStat>();
      for (const s of stats) m.set(s.window_start, s);
      setStatsMap(m);
    });
    const interval = setInterval(() => {
      fetchStrategyStats(market!, strategy!).then((d) => {
        if (d) setData(d);
      });
    }, 10000);
    return () => clearInterval(interval);
  }, [market, strategy]);

  if (loading) return <p className="text-dim text-sm">Loading...</p>;
  if (!data) return <p className="text-dim text-sm">No data for {strategy} on {market}.</p>;

  const { stats, sessions, trades, resolutions, version_summary } = data;

  const now = Date.now();
  const cutoff24h = now - 24 * 60 * 60 * 1000;
  const cutoff1h = now - 60 * 60 * 1000;
  const resolvedWithTrades = sessions.filter((s) => s.trades > 0 && !s._active);
  const pnl24h = resolvedWithTrades
    .filter((s) => new Date(s.timestamp).getTime() >= cutoff24h)
    .reduce((sum, s) => sum + s.pnl, 0);
  const pnl1h = resolvedWithTrades
    .filter((s) => new Date(s.timestamp).getTime() >= cutoff1h)
    .reduce((sum, s) => sum + s.pnl, 0);

  const tradedSessions = sessions.filter((s) => s.trades > 0 || s._active);
  let cumPnl = 0;
  const equityCurve = resolvedWithTrades.map((s) => {
    cumPnl += s.pnl;
    return { ...s, cumPnl };
  });

  return (
    <div>
      <div className="mb-5">
        <Link to="/bot" className="text-accent no-underline text-[13px] hover:underline">
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

      {/* Stats cards */}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-6">
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Sharpe</div>
          <div className="text-base font-semibold">{stats.sharpe.toFixed(3)}</div>
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
            <div className="text-base font-semibold text-down">
              -${stats.total_fees.toFixed(2)}
            </div>
          </div>
        )}
        {stats.current_cash != null && (
          <div className="bg-card border border-border rounded-lg px-4 py-3">
            <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Cash</div>
            <div className="text-base font-semibold">${stats.current_cash.toFixed(2)}</div>
          </div>
        )}
      </div>

      {/* Equity curve */}
      {equityCurve.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">Equity Curve (Cumulative PnL)</h3>
          <EquityBar points={equityCurve} />
        </div>
      )}

      {/* Version history */}
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
                  .map(([version, v]) => (
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
                      <span className="text-up">{v.wins}W</span>
                      {" / "}
                      <span className="text-down">{v.losses}L</span>
                    </td>
                    <td>
                      <span className={v.pnl > 0 ? "text-up" : v.pnl < 0 ? "text-down" : ""}>
                        {v.pnl >= 0 ? "+" : ""}{v.pnl.toFixed(2)}
                      </span>
                    </td>
                    <td className="font-mono text-xs">
                      <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(v.first)}</span>
                      {fmtTimeOnly(v.first)}
                    </td>
                    <td className="font-mono text-xs">
                      <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(v.last)}</span>
                      {fmtTimeOnly(v.last)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Session results table */}
      <div className="mb-6">
        <h3 className="text-sm font-semibold mb-3">
          Session Results ({tradedSessions.length})
        </h3>
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
                  <th>BTC</th>
                  <th>Trades</th>
                  <th>PnL</th>
                  <th>Credit</th>
                  <th>Cash</th>
                </tr>
              </thead>
              <tbody>
                {[...tradedSessions].reverse().map((s, i) => {
                  const ws = s._window_start_utc || s.timestamp;
                  const st = statsMap.get(ws);
                  return (
                  <tr key={i}>
                    <td className="font-mono text-xs">
                      <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(s.timestamp)}</span>
                      {fmtTimeOnly(s.timestamp)}
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
                      {s._active && s._tick_index != null && (
                        <ActiveTimer
                          tickIndex={s._tick_index}
                          elapsedSeconds={s._elapsed_seconds ?? null}
                          windowEndUtc={s._window_end_utc ?? null}
                          windowStartUtc={s._window_start_utc ?? null}
                        />
                      )}
                    </td>
                    <td className="font-mono text-xs tabular-nums">
                      {st ? (
                        <span className={st.return_bps > 0 ? "text-up" : st.return_bps < 0 ? "text-down" : "text-dim"}>
                          {st.return_bps >= 0 ? "+" : ""}{(st.return_bps / 100).toFixed(2)}%
                        </span>
                      ) : "\u2014"}
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
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Trade log */}
      <div className="mb-6">
        <h3 className="text-sm font-semibold mb-3">
          Trades ({trades.length})
        </h3>
        {trades.length === 0 ? (
          <p className="text-dim text-sm">No trades recorded.</p>
        ) : (
          <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto max-h-[480px] overflow-y-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Elapsed</th>
                  <th>Side</th>
                  <th>Price</th>
                  <th>Tokens</th>
                  <th>Amount</th>
                  <th>Fee</th>
                  <th>Status</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i} className={t.success === false ? "bg-down/[0.04]" : ""}>
                    <td className="font-mono text-xs">
                      {t.timestamp ? (
                        <>
                          <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(t.timestamp)}</span>
                          {fmtTimeOnly(t.timestamp)}
                        </>
                      ) : t.session_start ? (
                        <>
                          <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(t.session_start)}</span>
                          {fmtTimeOnly(t.session_start)}
                        </>
                      ) : (
                        "\u2014"
                      )}
                    </td>
                    <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "\u2014"}</td>
                    <td>
                      <span className={`font-semibold text-xs ${t.side.includes("up") ? "text-up" : "text-down"}`}>
                        {t.side === "buy_up" ? "Buy Up" : t.side === "buy_down" ? "Buy Down" :
                         t.side === "sell_up" ? "Sell Up" : t.side === "sell_down" ? "Sell Down" : t.side}
                      </span>
                    </td>
                    <td>${t.price.toFixed(2)}</td>
                    <td>{t.tokens}</td>
                    <td>${t.amount.toFixed(2)}</td>
                    <td className="text-dim text-xs">
                      {t.fee != null ? `$${t.fee.toFixed(2)}` : "\u2014"}
                    </td>
                    <td>
                      <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                        t.success === false ? "bg-down/15 text-down" : "bg-up/15 text-up"
                      }`}>
                        {t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM"}
                      </span>
                    </td>
                    <td className="max-w-[240px] overflow-hidden text-ellipsis whitespace-nowrap text-dim text-xs cursor-default" title={t.error || ""}>
                      {t.error || "\u2014"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

/** Tick + time remaining for an active session, interpolated between polls */
function ActiveTimer({
  tickIndex,
  elapsedSeconds,
  windowEndUtc,
}: {
  tickIndex: number;
  elapsedSeconds: number | null;
  windowEndUtc: string | null;
  windowStartUtc: string | null;
}) {
  const [now, setNow] = useState(Date.now());
  const [fetchedAt] = useState(Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Interpolate tick count: ticks arrive ~1/sec, so estimate how many ticks
  // have passed since this data was fetched
  const secSinceFetch = (now - fetchedAt) / 1000;
  const estimatedTick = tickIndex + Math.floor(secSinceFetch);

  // Elapsed: use server value + time since fetch
  let elapsedStr: string | null = null;
  if (elapsedSeconds != null) {
    const totalElapsed = elapsedSeconds + secSinceFetch;
    const m = Math.floor(totalElapsed / 60);
    const s = Math.floor(totalElapsed % 60);
    elapsedStr = `${m}:${s.toString().padStart(2, "0")}`;
  }

  // Time remaining from window end
  let remaining: string | null = null;
  if (windowEndUtc) {
    const endMs = new Date(windowEndUtc).getTime();
    const diffSec = Math.max(0, Math.round((endMs - now) / 1000));
    const m = Math.floor(diffSec / 60);
    const s = diffSec % 60;
    remaining = `${m}:${s.toString().padStart(2, "0")}`;
  }

  return (
    <div className="mt-1 text-[11px] text-dim font-mono leading-tight">
      <span>tick {estimatedTick}</span>
      {elapsedStr && <span className="ml-1.5">({elapsedStr} elapsed)</span>}
      {remaining && (
        <span className="ml-1.5 text-accent">{remaining} left</span>
      )}
    </div>
  );
}

/** Simple horizontal equity bar chart */
function EquityBar({ points }: { points: Array<{ timestamp: string; pnl: number; cumPnl: number }> }) {
  if (points.length === 0) return null;
  const maxAbs = Math.max(...points.map((p) => Math.abs(p.cumPnl)), 0.01);

  return (
    <div className="flex gap-px h-24 bg-card rounded-md px-2 py-1 border border-border">
      {points.map((p, i) => {
        const h = Math.max(Math.abs(p.cumPnl) / maxAbs * 40, 1);
        return (
          <div key={i} className="flex-1 min-w-0.5 max-w-3 flex flex-col" title={`${fmtDateOnly(p.timestamp)}: ${p.pnl >= 0 ? "+" : ""}${p.pnl.toFixed(2)} (cum: ${p.cumPnl >= 0 ? "+" : ""}${p.cumPnl.toFixed(2)})`}>
            <div className="flex-1 flex items-end justify-center">
              {p.cumPnl >= 0 && <div className="w-full rounded-sm opacity-80 bg-up" style={{ height: h }} />}
            </div>
            <div className="flex-1 flex items-start justify-center">
              {p.cumPnl < 0 && <div className="w-full rounded-sm opacity-80 bg-down" style={{ height: h }} />}
            </div>
          </div>
        );
      })}
    </div>
  );
}
