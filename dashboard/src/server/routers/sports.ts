import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import { MARKETS_DIR, safePath } from "../helpers";

/**
 * Sports data lives under data/markets/<league>/games/<game-dir>/ rather than
 * the crypto layout of sessions/<timestamp>.json, because a game is long-lived
 * and its samples are appended as JSONL rather than rewritten as one blob.
 */

function gamesDir(league: string): string | null {
  return safePath(MARKETS_DIR, league, "games");
}

function readJson<T>(file: string): T | null {
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8")) as T;
  } catch {
    return null;
  }
}

/** Read a JSONL file, skipping malformed lines (a crash can truncate the last one). */
function readJsonl<T>(file: string, limit?: number): T[] {
  try {
    const lines = fs.readFileSync(file, "utf-8").split("\n").filter((l) => l.trim());
    const slice = limit != null && lines.length > limit ? lines.slice(-limit) : lines;
    const out: T[] = [];
    for (const line of slice) {
      try {
        out.push(JSON.parse(line) as T);
      } catch {
        // truncated / partial write — skip rather than failing the whole read
      }
    }
    return out;
  } catch {
    return [];
  }
}

export const sportsRouter = router({
  /** Leagues that have any collected data. */
  leagues: procedure.query(() => {
    try {
      if (!fs.existsSync(MARKETS_DIR)) return [] as string[];
      return fs
        .readdirSync(MARKETS_DIR, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => e.name)
        .filter((name) => fs.existsSync(path.join(MARKETS_DIR, name, "games")))
        .sort();
    } catch {
      return [] as string[];
    }
  }),

  /** All games for a league, newest first. */
  games: procedure
    .input(z.object({ league: z.string(), limit: z.number().optional() }))
    .query(({ input }) => {
      const dir = gamesDir(input.league);
      if (!dir || !fs.existsSync(dir)) return [];

      let names = fs
        .readdirSync(dir, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => e.name)
        .sort()
        .reverse();

      if (input.limit) names = names.slice(0, input.limit);

      return names
        .map((name) => {
          const gd = path.join(dir, name);
          const meta = readJson<any>(path.join(gd, "meta.json"));
          const state = readJson<any>(path.join(gd, "state.json"));
          if (!meta) return null;
          return {
            dir: name,
            home_team: meta.group?.home_team ?? "",
            away_team: meta.group?.away_team ?? "",
            start_time_utc: meta.group?.start_time_utc ?? "",
            game_id: meta.group?.game_id ?? null,
            event_title: meta.group?.event_title ?? "",
            sport: meta.sport ?? null,
            outcome: meta.outcome_leg_key ?? null,
            final_state: meta.final_state ?? null,
            // Live header, present while the collector is running.
            phase: state?.phase ?? null,
            score: state?.score ?? null,
            period: state?.period ?? null,
            clock: state?.clock ?? null,
            samples: state?.samples ?? 0,
            model_probs: state?.model_probs ?? [],
            reference_home_prob: state?.reference_home_prob ?? null,
            updated_utc: state?.updated_utc ?? null,
            resolved: meta.outcome_leg_key != null,
          };
        })
        .filter(Boolean);
    }),

  /**
   * Full detail for one game: metadata plus the sample series.
   *
   * Samples are downsampled to keep the payload reasonable — a hot burst can
   * produce thousands of rows and the chart cannot show them all anyway.
   */
  game: procedure
    .input(
      z.object({
        league: z.string(),
        dir: z.string(),
        maxPoints: z.number().default(1500),
      })
    )
    .query(({ input }) => {
      const base = gamesDir(input.league);
      if (!base) return null;
      const gd = safePath(base, input.dir);
      if (!gd || !fs.existsSync(gd)) return null;

      const meta = readJson<any>(path.join(gd, "meta.json"));
      if (!meta) return null;

      const all = readJsonl<any>(path.join(gd, "samples.jsonl"));
      const step = Math.max(1, Math.ceil(all.length / input.maxPoints));
      const samples = all.filter((_, i) => i % step === 0);

      const legs: any[] = meta.group?.legs ?? [];

      return {
        meta,
        legs,
        total_samples: all.length,
        downsample_step: step,
        samples: samples.map((s) => ({
          t: s.elapsed_seconds,
          timestamp: s.timestamp_utc,
          home_score: s.state?.home_score ?? 0,
          away_score: s.state?.away_score ?? 0,
          period: s.state?.period ?? "",
          clock: s.state?.clock ?? "",
          status: s.state?.status ?? "",
          under_review: s.state?.under_review ?? false,
          // Market midpoint per leg, parallel to `legs`.
          mids: (s.books ?? []).map((b: any) => b?.midpoint ?? null),
          model: s.model_probs ?? [],
          reference: s.reference_home_prob ?? null,
        })),
        trades: readJsonl<any>(path.join(gd, "trades.jsonl")),
        decisions: readJsonl<any>(path.join(gd, "decisions.jsonl"), 500),
      };
    }),

  /**
   * Feed-latency summary — the measurement that decides whether an
   * event-reaction edge exists at all.
   */
  latency: procedure
    .input(z.object({ league: z.string() }))
    .query(({ input }) => {
      const file = safePath(MARKETS_DIR, input.league, "latency.jsonl");
      if (!file || !fs.existsSync(file)) {
        return { events: 0, records: [], summary: null };
      }
      const records = readJsonl<any>(file);

      const withBook = records.filter((r) => r.feed_to_book_s != null);
      const deltas = withBook.map((r) => r.feed_to_book_s).sort((a, b) => a - b);
      const espn = records
        .filter((r) => r.feed_to_espn_s != null)
        .map((r) => r.feed_to_espn_s)
        .sort((a, b) => a - b);

      const median = (arr: number[]) =>
        arr.length === 0
          ? null
          : arr.length % 2 === 1
            ? arr[(arr.length - 1) / 2]
            : (arr[arr.length / 2 - 1] + arr[arr.length / 2]) / 2;

      const bookFirst = withBook.filter((r) => r.feed_to_book_s <= 0).length;

      return {
        events: records.length,
        records: records.slice(-200),
        summary: {
          events: records.length,
          with_book_move: withBook.length,
          never_moved: records.filter((r) => r.book_never_moved).length,
          median_feed_to_book_s: median(deltas),
          median_feed_to_espn_s: median(espn),
          // The number that decides whether reaction edge exists: if the book
          // usually moves before the feed reports, there is nothing to react to.
          fraction_book_first: withBook.length ? bookFirst / withBook.length : 0,
        },
      };
    }),

  /** Bot activity for a league. */
  botActivity: procedure
    .input(z.object({ league: z.string() }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.league);
      if (!dir) return { trades: [], decisions: [], log: [] };
      return {
        trades: readJsonl<any>(path.join(dir, "sports_bot_trades.jsonl"), 200),
        decisions: readJsonl<any>(path.join(dir, "sports_decisions.jsonl"), 500),
        log: readJsonl<any>(path.join(dir, "sports_bot_log.jsonl"), 200),
      };
    }),

  /** Aggregate PnL for a league, split by dry-run. */
  pnl: procedure
    .input(z.object({ league: z.string() }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.league);
      if (!dir) return { overall: 0, dry_run: 0, by_strategy: {} };

      const entries = readJsonl<any>(path.join(dir, "sports_bot_log.jsonl"));
      let overall = 0;
      let dry = 0;
      const byStrategy: Record<string, { pnl: number; games: number; wins: number }> = {};

      for (const e of entries) {
        const pnl = typeof e.pnl === "number" ? e.pnl : 0;
        if (e.dry_run) {
          dry += pnl;
          continue;
        }
        overall += pnl;
        const s = e.strategy || "unknown";
        if (!byStrategy[s]) byStrategy[s] = { pnl: 0, games: 0, wins: 0 };
        byStrategy[s].pnl += pnl;
        byStrategy[s].games += 1;
        if (pnl > 0) byStrategy[s].wins += 1;
      }

      const round = (n: number) => Math.round(n * 100) / 100;
      for (const k of Object.keys(byStrategy)) {
        byStrategy[k].pnl = round(byStrategy[k].pnl);
      }
      return { overall: round(overall), dry_run: round(dry), by_strategy: byStrategy };
    }),
});
