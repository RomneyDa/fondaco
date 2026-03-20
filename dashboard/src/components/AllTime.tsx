"use client";

import { useMemo } from "react";
import Link from "next/link";
import { trpc } from "@/trpc/client";
import { Plot } from "./Plot";
import { fmtDateOnly, fmtTimeOnly } from "@/utils/formatTime";

const LAYOUT_BASE: Partial<Plotly.Layout> = {
  paper_bgcolor: "transparent",
  plot_bgcolor: "transparent",
  font: { color: "#e6edf3", size: 12 },
  margin: { l: 60, r: 60, t: 30, b: 45 },
  xaxis: {
    gridcolor: "#30363d",
    zerolinecolor: "#30363d",
    type: "date",
  },
  yaxis: {
    gridcolor: "#30363d",
    zerolinecolor: "#30363d",
  },
  legend: { orientation: "h" as const, x: 0, y: 1.12 },
};

const PLOT_CFG: Partial<Plotly.Config> = { responsive: true, displayModeBar: false };

export function AllTime({ prefix }: { prefix: string }) {
  const dataQuery = trpc.sessions.allTime.useQuery({ prefix });
  const data = dataQuery.data as any;
  const loading = dataQuery.isLoading;

  const stats = useMemo(() => {
    if (!data || !data.sessions) return null;
    const sessions = data.sessions;
    const ups = sessions.filter((s: any) => s.outcome === "Up").length;
    const downs = sessions.filter((s: any) => s.outcome === "Down").length;
    const unresolved = sessions.filter((s: any) => !s.outcome).length;
    const first = sessions[0]?.window_start;
    const last = sessions[sessions.length - 1]?.window_start;
    const btcPrices = sessions.map((s: any) => s.initial_btc).filter((p: any): p is number => p != null);
    const btcMin = btcPrices.length > 0 ? Math.min(...btcPrices) : null;
    const btcMax = btcPrices.length > 0 ? Math.max(...btcPrices) : null;
    return { ups, downs, unresolved, first, last, btcMin, btcMax };
  }, [data]);

  const btcTrace = useMemo(() => {
    if (!data?.samples) return null;
    const x: string[] = [];
    const y: (number | null)[] = [];
    let prev: number | null = null;
    for (const s of data.samples) {
      if (s.btc !== prev || x.length === 0) {
        x.push(s.t);
        y.push(s.btc);
        prev = s.btc;
      }
    }
    const last = data.samples[data.samples.length - 1];
    if (last && (x[x.length - 1] !== last.t)) {
      x.push(last.t);
      y.push(last.btc);
    }
    return { x, y };
  }, [data]);

  const outcomeTrace = useMemo(() => {
    if (!data?.sessions) return null;
    const x: string[] = [];
    const colors: string[] = [];
    const text: string[] = [];
    const y: number[] = [];
    for (const s of data.sessions) {
      x.push(s.window_start);
      y.push(1);
      colors.push(s.outcome === "Up" ? "#3fb950" : s.outcome === "Down" ? "#f85149" : "#484f58");
      text.push(`${s.outcome ?? "Unresolved"} | ${fmtDateOnly(s.window_start)} ${fmtTimeOnly(s.window_start)}`);
    }
    return { x, y, colors, text };
  }, [data]);

  const dailyStats = useMemo(() => {
    if (!data?.sessions) return [];
    const byDay = new Map<string, { ups: number; downs: number; total: number; date: string }>();
    for (const s of data.sessions) {
      const d = new Date(s.window_start);
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      if (!byDay.has(key)) byDay.set(key, { ups: 0, downs: 0, total: 0, date: key });
      const entry = byDay.get(key)!;
      entry.total++;
      if (s.outcome === "Up") entry.ups++;
      else if (s.outcome === "Down") entry.downs++;
    }
    return Array.from(byDay.values()).sort((a, b) => a.date.localeCompare(b.date));
  }, [data]);

  if (loading) {
    return (
      <div className="flex items-center gap-3 py-12">
        <div className="w-5 h-5 border-2 border-accent border-t-transparent rounded-full animate-spin" />
        <span className="text-dim text-sm">Loading all-time data (this may take a moment)...</span>
      </div>
    );
  }

  if (!data || !stats) {
    return <p className="text-dim text-sm">No data available.</p>;
  }

  return (
    <div>
      <div className="mb-5">
        <Link href={`/sessions/${prefix}`} className="text-accent no-underline text-[13px] hover:underline">
          &larr; {prefix} Sessions
        </Link>
      </div>

      <h2 className="text-xl font-semibold mb-1">All-Time View</h2>
      <p className="text-dim text-sm mb-5">{prefix} &middot; {data.total_sessions?.toLocaleString()} sessions &middot; {data.downsampled_points?.toLocaleString()} chart points</p>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(160px,1fr))] gap-3 mb-6">
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Sessions</div>
          <div className="text-base font-semibold">{data.total_sessions?.toLocaleString()}</div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Up / Down</div>
          <div className="text-base font-semibold">
            <span className="text-up">{stats.ups}</span>{" / "}<span className="text-down">{stats.downs}</span>
            {stats.unresolved > 0 && <span className="text-dim"> ({stats.unresolved} pending)</span>}
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Up Rate</div>
          <div className="text-base font-semibold">
            {stats.ups + stats.downs > 0 ? ((stats.ups / (stats.ups + stats.downs)) * 100).toFixed(1) + "%" : "\u2014"}
          </div>
        </div>
        <div className="bg-card border border-border rounded-lg px-4 py-3">
          <div className="text-[11px] uppercase tracking-wide text-dim mb-1.5">Date Range</div>
          <div className="text-sm font-semibold">{fmtDateOnly(stats.first)} &ndash; {fmtDateOnly(stats.last)}</div>
        </div>
      </div>

      {btcTrace && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">BTC Price (All Sessions)</h3>
          <Plot
            className="chart-wrap"
            data={[{
              x: btcTrace.x,
              y: btcTrace.y,
              type: "scattergl",
              mode: "lines",
              name: "BTC/USD",
              line: { color: "#f7931a", width: 1 },
              connectgaps: false,
            }]}
            layout={{
              ...LAYOUT_BASE,
              height: 350,
              yaxis: { ...LAYOUT_BASE.yaxis, title: { text: "BTC/USD", standoff: 8 }, tickformat: ",.0f" },
              xaxis: { ...LAYOUT_BASE.xaxis, rangeslider: { visible: true } },
            }}
            config={PLOT_CFG}
          />
        </div>
      )}

      {outcomeTrace && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">Session Outcomes</h3>
          <Plot
            className="chart-wrap"
            data={[{
              x: outcomeTrace.x,
              y: outcomeTrace.y,
              type: "bar",
              name: "Outcome",
              marker: { color: outcomeTrace.colors },
              text: outcomeTrace.text,
              hoverinfo: "text",
            }]}
            layout={{
              ...LAYOUT_BASE,
              height: 120,
              yaxis: { visible: false },
              xaxis: { ...LAYOUT_BASE.xaxis, type: "date" },
              margin: { l: 60, r: 60, t: 10, b: 45 },
              showlegend: false,
              bargap: 0,
            }}
            config={PLOT_CFG}
          />
        </div>
      )}

      {dailyStats.length > 0 && (
        <div className="mb-6">
          <h3 className="text-sm font-semibold mb-3">Daily Breakdown ({dailyStats.length} days)</h3>
          <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto max-h-[480px] overflow-y-auto">
            <table className="data-table">
              <thead>
                <tr><th>Date</th><th>Sessions</th><th>Up</th><th>Down</th><th>Up%</th></tr>
              </thead>
              <tbody>
                {[...dailyStats].reverse().map((d) => (
                  <tr key={d.date}>
                    <td className="font-mono text-xs">{d.date}</td>
                    <td>{d.total}</td>
                    <td className="text-up">{d.ups}</td>
                    <td className="text-down">{d.downs}</td>
                    <td>{d.ups + d.downs > 0 ? ((d.ups / (d.ups + d.downs)) * 100).toFixed(1) + "%" : "\u2014"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="mb-6">
        <h3 className="text-sm font-semibold mb-3">All Sessions ({data.sessions?.length})</h3>
        <div className="bg-card border border-border rounded-lg overflow-hidden overflow-x-auto max-h-[600px] overflow-y-auto">
          <table className="data-table">
            <thead>
              <tr><th>Time</th><th>Outcome</th><th>BTC Open</th><th>BTC Close</th><th>Move</th><th>Samples</th></tr>
            </thead>
            <tbody>
              {[...(data.sessions || [])].reverse().map((s: any) => {
                const move = s.initial_btc != null && s.final_btc != null
                  ? ((s.final_btc - s.initial_btc) / s.initial_btc) * 10000
                  : null;
                return (
                  <tr key={s.file}>
                    <td className="font-mono text-xs">
                      <Link href={`/sessions/${prefix}/${s.file}`} className="no-underline hover:text-accent transition-colors">
                        <span className="text-dim hover:text-accent mr-1.5 text-[11px]">{fmtDateOnly(s.window_start)}</span>
                        {fmtTimeOnly(s.window_start)}
                      </Link>
                    </td>
                    <td>
                      <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                        s.outcome === "Up" ? "bg-up/15 text-up" : s.outcome === "Down" ? "bg-down/15 text-down" : "bg-[rgba(136,132,216,0.15)] text-dim"
                      }`}>{s.outcome ?? "Pending"}</span>
                    </td>
                    <td className="font-mono text-xs tabular-nums">{s.initial_btc != null ? `$${s.initial_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "\u2014"}</td>
                    <td className="font-mono text-xs tabular-nums">{s.final_btc != null ? `$${s.final_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : "\u2014"}</td>
                    <td className="font-mono text-xs tabular-nums">
                      {move != null ? (
                        <span className={move > 0 ? "text-up" : move < 0 ? "text-down" : "text-dim"}>
                          {move >= 0 ? "+" : ""}{(move / 100).toFixed(2)}%
                        </span>
                      ) : "\u2014"}
                    </td>
                    <td className="text-dim text-xs">{s.sample_count?.toLocaleString()}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
