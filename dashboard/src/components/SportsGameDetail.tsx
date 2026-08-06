"use client";

import Link from "next/link";
import { useMemo } from "react";
import { trpc } from "@/trpc/client";

/**
 * Per-game detail. The chart is the most useful screen in the sports path:
 * model probability against market midpoint over the game timeline, with score
 * events marked. A persistent gap between the two lines is either an
 * opportunity or a broken model, and both are visible at a glance.
 *
 * Drawn as inline SVG rather than pulling in a chart library — the shape is
 * simple and the dashboard has no charting dependency for this.
 */

const W = 1000;
const H = 320;
// `t` reserves a band above the plot for score-event labels, so they never
// overlap the probability lines.
const PAD = { l: 44, r: 16, t: 26, b: 28 };

interface Point {
  t: number;
  mids: (number | null)[];
  model: number[];
  reference: number | null;
  home_score: number;
  away_score: number;
  under_review: boolean;
}

export function SportsGameDetail({ league, dir }: { league: string; dir: string }) {
  const q = trpc.sports.game.useQuery({ league, dir }, { refetchInterval: 5000 });

  const data = q.data;
  const samples: Point[] = (data?.samples ?? []) as Point[];

  const { xOf, yOf, scoreEvents, tMax } = useMemo(() => {
    const tMax = samples.length ? Math.max(...samples.map((s) => s.t), 1) : 1;
    const xOf = (t: number) => PAD.l + (t / tMax) * (W - PAD.l - PAD.r);
    // Probability axis is always [0, 1]; no autoscaling, because the absolute
    // level is what matters here.
    const yOf = (p: number) => PAD.t + (1 - p) * (H - PAD.t - PAD.b);

    const scoreEvents: { t: number; label: string }[] = [];
    let prev: Point | null = null;
    for (const s of samples) {
      if (
        prev &&
        (s.home_score !== prev.home_score || s.away_score !== prev.away_score)
      ) {
        scoreEvents.push({ t: s.t, label: `${s.home_score}-${s.away_score}` });
      }
      prev = s;
    }
    return { xOf, yOf, scoreEvents, tMax };
  }, [samples]);

  if (q.isLoading) return <div className="p-6 text-dim">Loading…</div>;
  if (!data) return <div className="p-6 text-dim">Game not found.</div>;

  const meta = data.meta as any;
  const legs = (data.legs ?? []) as any[];

  const line = (values: (number | null)[]) => {
    let d = "";
    let pen = false;
    values.forEach((v, i) => {
      if (v == null || !Number.isFinite(v)) {
        pen = false;
        return;
      }
      const cmd = pen ? "L" : "M";
      d += `${cmd}${xOf(samples[i].t).toFixed(1)},${yOf(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  };

  const marketHome = samples.map((s) => s.mids?.[0] ?? null);
  const modelHome = samples.map((s) => (s.model?.length ? s.model[0] : null));
  const referenceHome = samples.map((s) => s.reference ?? null);

  // ESPN publishes no soccer win probability, so the series is legitimately
  // absent there. Advertising it in the legend would read as missing data
  // rather than not-applicable.
  const hasReference = referenceHome.some((v) => v != null);

  return (
    <div className="p-6 space-y-6">
      <div>
        <Link href={`/sports/${league}`} className="text-xs text-dim hover:text-accent">
          ← {league.toUpperCase()}
        </Link>
        <h1 className="text-xl font-semibold text-accent mt-1">
          {meta.group?.away_team} @ {meta.group?.home_team}
        </h1>
        <div className="text-xs text-dim">
          {meta.group?.start_time_utc} · gameId {meta.group?.game_id ?? "—"} ·{" "}
          {data.total_samples} samples
          {data.downsample_step > 1 && ` (showing every ${data.downsample_step})`}
          {meta.outcome_leg_key && ` · outcome: ${meta.outcome_leg_key}`}
        </div>
      </div>

      <div className="rounded-lg border border-border p-4">
        <div className="flex items-center gap-5 text-[11px] mb-2">
          <Legend color="#58a6ff" label="market midpoint (home)" />
          <Legend color="#d29922" label="model (home)" />
          {hasReference && <Legend color="#bc8cff" label="ESPN reference" dashed />}
          <Legend color="#f85149" label="score event" />
        </div>

        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ maxHeight: 360 }}>
          {/* Probability gridlines */}
          {[0, 0.25, 0.5, 0.75, 1].map((p) => (
            <g key={p}>
              <line
                x1={PAD.l}
                x2={W - PAD.r}
                y1={yOf(p)}
                y2={yOf(p)}
                stroke="#30363d"
                strokeWidth={p === 0.5 ? 1 : 0.5}
                strokeDasharray={p === 0.5 ? "4 3" : undefined}
              />
              <text x={8} y={yOf(p) + 4} fill="#8b949e" fontSize={10}>
                {(p * 100).toFixed(0)}%
              </text>
            </g>
          ))}

          {/* Score events. Labels sit in a reserved band above the plot so
              they never collide with the probability lines, and flip to the
              left of their marker near the right edge so they are not clipped. */}
          {scoreEvents.map((e, i) => {
            const x = xOf(e.t);
            const nearRight = x > W - PAD.r - 40;
            return (
              <g key={i}>
                <line
                  x1={x}
                  x2={x}
                  y1={PAD.t}
                  y2={H - PAD.b}
                  stroke="#f85149"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                  opacity={0.7}
                />
                <text
                  x={nearRight ? x - 3 : x + 3}
                  y={PAD.t - 4}
                  textAnchor={nearRight ? "end" : "start"}
                  fill="#f85149"
                  fontSize={9}
                  fontFamily="monospace"
                >
                  {e.label}
                </text>
              </g>
            );
          })}

          <path d={line(marketHome)} fill="none" stroke="#58a6ff" strokeWidth={1.6} />
          <path d={line(modelHome)} fill="none" stroke="#d29922" strokeWidth={1.6} />
          <path
            d={line(referenceHome)}
            fill="none"
            stroke="#bc8cff"
            strokeWidth={1.4}
            strokeDasharray="4 3"
          />

          {/* Time axis */}
          <line
            x1={PAD.l}
            x2={W - PAD.r}
            y1={H - PAD.b}
            y2={H - PAD.b}
            stroke="#30363d"
          />
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <text
              key={f}
              x={xOf(f * tMax)}
              y={H - PAD.b + 14}
              fill="#8b949e"
              fontSize={10}
              textAnchor="middle"
            >
              {Math.round((f * tMax) / 60)}m
            </text>
          ))}
        </svg>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <Panel title={`Legs (${legs.length})`}>
          <table className="w-full text-xs">
            <tbody>
              {legs.map((l: any, i: number) => (
                <tr key={i} className="border-b border-border/40">
                  <td className="py-1 pr-3 font-mono text-dim">{i}</td>
                  <td className="py-1 pr-3">{l.key}</td>
                  <td className="py-1 pr-3 text-dim">{l.label}</td>
                  <td className="py-1 text-dim">{l.kind}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title={`Trades (${data.trades.length})`}>
          {data.trades.length === 0 ? (
            <div className="text-xs text-dim">No trades.</div>
          ) : (
            <table className="w-full text-xs">
              <tbody>
                {data.trades.map((t: any, i: number) => (
                  <tr key={i} className="border-b border-border/40">
                    <td className="py-1 pr-3">{t.side}</td>
                    <td className="py-1 pr-3 font-mono">{t.price}</td>
                    <td className="py-1 font-mono text-dim">{t.tokens}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      {/* The decision log is the sports path's most useful debugging artefact:
          sports makes few decisions per game, so every one can be recorded
          along with the gate that blocked it. */}
      <Panel title={`Decisions (${data.decisions.length})`}>
        {data.decisions.length === 0 ? (
          <div className="text-xs text-dim">
            No decision records. Run a bot worker to populate this.
          </div>
        ) : (
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="text-dim sticky top-0 bg-sidebar">
                <tr>
                  <th className="text-left py-1 font-medium">t</th>
                  <th className="text-left py-1 font-medium">gate</th>
                  <th className="text-left py-1 font-medium">leg</th>
                  <th className="text-left py-1 font-medium">model</th>
                  <th className="text-left py-1 font-medium">market</th>
                  <th className="text-left py-1 font-medium">edge</th>
                  <th className="text-left py-1 font-medium">note</th>
                </tr>
              </thead>
              <tbody>
                {data.decisions.map((d: any, i: number) => (
                  <tr key={i} className="border-b border-border/30">
                    <td className="py-1 pr-3 font-mono text-dim">
                      {d.elapsed_seconds != null ? `${Math.round(d.elapsed_seconds)}s` : "—"}
                    </td>
                    <td className="py-1 pr-3">
                      <span
                        style={{ color: d.gate === "traded" ? "#3fb950" : "#8b949e" }}
                      >
                        {d.gate}
                      </span>
                    </td>
                    <td className="py-1 pr-3 font-mono text-dim">{d.leg ?? "—"}</td>
                    <td className="py-1 pr-3 font-mono">
                      {d.model_prob != null ? d.model_prob.toFixed(3) : "—"}
                    </td>
                    <td className="py-1 pr-3 font-mono">
                      {d.market_mid != null ? d.market_mid.toFixed(3) : "—"}
                    </td>
                    <td className="py-1 pr-3 font-mono">
                      {d.edge != null ? d.edge.toFixed(3) : "—"}
                    </td>
                    <td className="py-1 text-dim">{d.note ?? ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="text-sm font-semibold mb-2">{title}</div>
      {children}
    </div>
  );
}

function Legend({ color, label, dashed }: { color: string; label: string; dashed?: boolean }) {
  return (
    <span className="flex items-center gap-1.5 text-dim">
      <svg width="18" height="4">
        <line
          x1="0"
          y1="2"
          x2="18"
          y2="2"
          stroke={color}
          strokeWidth="2"
          strokeDasharray={dashed ? "4 3" : undefined}
        />
      </svg>
      {label}
    </span>
  );
}
