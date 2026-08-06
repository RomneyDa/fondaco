"use client";

import Link from "next/link";
import { useState } from "react";
import { trpc } from "@/trpc/client";

function fmtPct(v: number | null | undefined) {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function phaseColor(phase: string | null) {
  switch (phase) {
    case "hot":
      return "#f85149";
    case "idle_inplay":
      return "#3fb950";
    case "pregame":
      return "#d29922";
    case "ended":
      return "#8b949e";
    default:
      return "#8b949e";
  }
}

/**
 * Edge between the model and the market on the home leg.
 *
 * Shown per game because it is the one number that says "look here" — a large
 * gap is either an opportunity or a broken model, and both are worth seeing.
 */
function edgeOf(modelProbs: number[], _reference: number | null) {
  if (!modelProbs || modelProbs.length === 0) return null;
  return modelProbs[0];
}

export function SportsGames({ league }: { league: string }) {
  const [showResolved, setShowResolved] = useState(false);
  const games = trpc.sports.games.useQuery(
    { league, limit: 200 },
    { refetchInterval: 5000 }
  );
  const latency = trpc.sports.latency.useQuery({ league });
  const pnl = trpc.sports.pnl.useQuery({ league }, { refetchInterval: 10000 });

  const rows = (games.data ?? []).filter((g: any) =>
    showResolved ? true : !g.resolved
  );

  const summary = latency.data?.summary;

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-accent">{league.toUpperCase()}</h1>
        <label className="flex items-center gap-2 text-xs text-dim cursor-pointer">
          <input
            type="checkbox"
            checked={showResolved}
            onChange={(e) => setShowResolved(e.target.checked)}
          />
          show resolved
        </label>
      </div>

      {/* Feed latency — the measurement that decides whether reaction edge exists. */}
      {summary && summary.events > 0 && (
        <div className="rounded-lg border border-border p-4">
          <div className="text-sm font-semibold mb-2">Feed latency</div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
            <Stat label="events" value={String(summary.events)} />
            <Stat
              label="median feed → book"
              value={
                summary.median_feed_to_book_s != null
                  ? `${summary.median_feed_to_book_s.toFixed(1)}s`
                  : "—"
              }
            />
            <Stat
              label="median feed → ESPN"
              value={
                summary.median_feed_to_espn_s != null
                  ? `${summary.median_feed_to_espn_s.toFixed(1)}s`
                  : "—"
              }
            />
            <Stat
              label="book moved first"
              value={fmtPct(summary.fraction_book_first)}
              warn={summary.fraction_book_first > 0.5 && summary.events >= 20}
            />
          </div>
          {summary.fraction_book_first > 0.5 && summary.events >= 20 && (
            <div className="mt-3 text-xs" style={{ color: "#f85149" }}>
              The book moved before the feed on most events. There may be no
              event-reaction edge in this league — strategies would have to come
              from model disagreement instead.
            </div>
          )}
        </div>
      )}

      {pnl.data && (
        <div className="rounded-lg border border-border p-4 flex gap-8 text-sm">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-dim">live pnl</div>
            <div
              className="font-mono"
              style={{ color: pnl.data.overall >= 0 ? "#3fb950" : "#f85149" }}
            >
              ${pnl.data.overall.toFixed(2)}
            </div>
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wider text-dim">dry run</div>
            <div className="font-mono text-dim">${pnl.data.dry_run.toFixed(2)}</div>
          </div>
        </div>
      )}

      <div className="rounded-lg border border-border overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-dim border-b border-border">
              <Th>game</Th>
              <Th>phase</Th>
              <Th>score</Th>
              <Th>clock</Th>
              <Th>model (home)</Th>
              <Th>ESPN</Th>
              <Th>samples</Th>
              <Th>outcome</Th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={8} className="px-4 py-6 text-center text-dim">
                  No games. Start a collector:{" "}
                  <code>./target/release/sports-collector --league {league}</code>
                </td>
              </tr>
            )}
            {rows.map((g: any) => (
              <tr key={g.dir} className="border-b border-border/50 hover:bg-accent/5">
                <td className="px-4 py-2">
                  <Link
                    href={`/sports/${league}/${encodeURIComponent(g.dir)}`}
                    className="text-accent hover:underline"
                  >
                    {g.away_team} @ {g.home_team}
                  </Link>
                  <div className="text-[10px] text-dim">{g.start_time_utc?.slice(0, 16)}</div>
                </td>
                <td className="px-4 py-2">
                  <span
                    className="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                    style={{
                      color: phaseColor(g.phase),
                      background: `${phaseColor(g.phase)}20`,
                    }}
                  >
                    {g.phase ?? "—"}
                  </span>
                </td>
                <td className="px-4 py-2 font-mono">{g.score ?? "—"}</td>
                <td className="px-4 py-2 font-mono text-dim">
                  {g.period} {g.clock}
                </td>
                <td className="px-4 py-2 font-mono">{fmtPct(edgeOf(g.model_probs, null))}</td>
                <td className="px-4 py-2 font-mono text-dim">
                  {fmtPct(g.reference_home_prob)}
                </td>
                <td className="px-4 py-2 font-mono text-dim">{g.samples}</td>
                <td className="px-4 py-2">
                  {g.outcome ? (
                    <span className="text-accent">{g.outcome}</span>
                  ) : (
                    <span className="text-dim">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="px-4 py-2 text-left font-medium uppercase tracking-wider text-[10px]">
      {children}
    </th>
  );
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-dim">{label}</div>
      <div className="font-mono" style={{ color: warn ? "#f85149" : undefined }}>
        {value}
      </div>
    </div>
  );
}
