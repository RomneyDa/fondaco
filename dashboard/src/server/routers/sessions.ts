import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import { MARKETS_DIR, safePath, listMarketPrefixes, marketTradesFile } from "../helpers";

interface AllTimeCache {
  fileCount: number;
  generatedAt: number;
  data: object;
}

const allTimeCache = new Map<string, AllTimeCache>();

export const sessionsRouter = router({
  prefixes: procedure.query(() => {
    return listMarketPrefixes();
  }),

  list: procedure
    .input(z.object({ prefix: z.string() }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.prefix, "sessions");
      if (!dir || !fs.existsSync(dir)) return [];
      return fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".json") && !f.includes(".trading.") && !f.includes(".summary."))
        .sort()
        .reverse();
    }),

  summaries: procedure
    .input(z.object({ prefix: z.string(), limit: z.number().optional() }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.prefix, "sessions");
      if (!dir || !fs.existsSync(dir)) return [];
      const files = fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".json") && !f.includes(".trading.") && !f.includes(".summary."))
        .sort()
        .reverse();

      const limit = Math.min(input.limit ?? 20, 100);
      return files.slice(0, limit).map((file) => {
        let outcome: string | null = null;
        try {
          const filepath = path.join(dir, file);
          const fd = fs.openSync(filepath, "r");
          const buf = Buffer.alloc(1024);
          fs.readSync(fd, buf, 0, 1024, 0);
          fs.closeSync(fd);
          const header = buf.toString("utf-8");
          const match = header.match(/"outcome":\s*"([^"]+)"/);
          if (match) outcome = match[1];
        } catch {}
        return { file, outcome };
      });
    }),

  stats: procedure
    .input(z.object({
      prefix: z.string(),
      last: z.number().optional(),
      before: z.string().optional(),
      after: z.string().optional(),
      session: z.string().optional(),
    }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.prefix, "sessions");
      if (!dir || !fs.existsSync(dir)) return [];
      const files = fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".json") && !f.includes(".trading.") && !f.includes(".summary."))
        .sort()
        .reverse();

      const last = input.last ?? 50;
      return files
        .slice(0, last)
        .map((file) => {
          try {
            const filepath = path.join(dir, file);
            const data = JSON.parse(fs.readFileSync(filepath, "utf-8"));
            if (input.after && data.window_start_utc && data.window_start_utc <= input.after) {
              return null;
            }
            return {
              file,
              outcome: data.outcome || null,
              window_start_utc: data.window_start_utc || null,
              initial_btc_price: data.initial_btc_price || null,
              final_btc_price: data.final_btc_price || null,
              num_samples: data.samples?.length || 0,
            };
          } catch { return null; }
        })
        .filter(Boolean);
    }),

  get: procedure
    .input(z.object({ prefix: z.string(), file: z.string() }))
    .query(({ input }) => {
      const filepath = safePath(MARKETS_DIR, input.prefix, "sessions", input.file);
      if (!filepath || !fs.existsSync(filepath)) return null;
      return JSON.parse(fs.readFileSync(filepath, "utf-8"));
    }),

  trading: procedure
    .input(z.object({ prefix: z.string(), file: z.string() }))
    .query(({ input }) => {
      const dir = safePath(MARKETS_DIR, input.prefix, "sessions");
      if (!dir) return null;

      // Try legacy single sidecar first
      const legacyFile = input.file + ".trading.json";
      const legacyPath = path.join(dir, legacyFile);
      if (fs.existsSync(legacyPath)) {
        return JSON.parse(fs.readFileSync(legacyPath, "utf-8"));
      }

      // Look for per-worker sidecars
      const prefix = input.file + ".trading.";
      const workerSidecars = fs.existsSync(dir)
        ? fs.readdirSync(dir).filter(
            (f) => f.startsWith(prefix) && f.endsWith(".json") && f !== legacyFile
          )
        : [];

      if (workerSidecars.length === 0) {
        // Fallback: reconstruct from bot_trades.jsonl
        const sessionName = input.file.replace(".json", "");
        const tradesFile = marketTradesFile(input.prefix);
        if (fs.existsSync(tradesFile)) {
          try {
            const lines = fs.readFileSync(tradesFile, "utf-8").trim().split("\n");
            const sessionTs = sessionName.replace(/T(\d{2})-(\d{2})-(\d{2})Z?$/, "T$1:$2:$3Z");
            const windowStart = new Date(sessionTs);
            const windowEnd = new Date(windowStart.getTime() + 5 * 60 * 1000);
            const byStrategy: Record<string, any[]> = {};
            const resolutionsByStrategy: Record<string, any> = {};

            for (const line of lines) {
              if (!line) continue;
              try {
                const entry = JSON.parse(line);
                if (entry.type === "resolution" && entry.strategy) {
                  const ts = new Date(entry.timestamp);
                  if (ts >= windowStart && ts < new Date(windowEnd.getTime() + 60000)) {
                    resolutionsByStrategy[entry.strategy] = entry;
                  }
                  continue;
                }
                if (!entry.timestamp || !entry.strategy) continue;
                if (entry.order_type && entry.fee == null && entry.success !== true) continue;
                const ts = new Date(entry.timestamp);
                if (ts >= windowStart && ts < windowEnd) {
                  if (!byStrategy[entry.strategy]) byStrategy[entry.strategy] = [];
                  byStrategy[entry.strategy].push(entry);
                }
              } catch {}
            }

            // Deduplicate by order_id
            for (const name of Object.keys(byStrategy)) {
              const seen = new Map<string, number>();
              const trades = byStrategy[name];
              const deduped: any[] = [];
              for (const t of trades) {
                const key = t.order_id || `${t.timestamp}-${t.side}`;
                const existing = seen.get(key);
                if (existing != null) {
                  if (t.fee != null && deduped[existing].fee == null) {
                    deduped[existing] = t;
                  }
                } else {
                  seen.set(key, deduped.length);
                  deduped.push(t);
                }
              }
              byStrategy[name] = deduped;
            }

            const strategyNames = Object.keys(byStrategy);
            if (strategyNames.length > 0) {
              const logFile = path.join(MARKETS_DIR, input.prefix, "bot_log.jsonl");
              const logByStrategy: Record<string, any> = {};
              if (fs.existsSync(logFile)) {
                const logLines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
                for (const ll of logLines) {
                  if (!ll) continue;
                  try {
                    const le = JSON.parse(ll);
                    if (le.strategy && le.timestamp) {
                      const leTs = new Date(le.timestamp);
                      if (Math.abs(leTs.getTime() - windowEnd.getTime()) < 60000) {
                        logByStrategy[le.strategy] = le;
                      }
                    }
                  } catch {}
                }
              }

              let outcome: string | null = null;
              const sessionFile = path.join(dir, input.file);
              if (fs.existsSync(sessionFile)) {
                try {
                  const sd = JSON.parse(fs.readFileSync(sessionFile, "utf-8"));
                  outcome = sd.outcome || null;
                } catch {}
              }

              const strategies = strategyNames.map((name) => {
                const logEntry = logByStrategy[name];
                const stratTrades = byStrategy[name];
                let tradePnl: number | null = null;
                if (logEntry?.pnl != null && logEntry.trades > 0) {
                  tradePnl = logEntry.pnl;
                } else if (stratTrades.length > 0) {
                  let spent = 0, received = 0, fees = 0;
                  let upTokens = 0, downTokens = 0;
                  for (const t of stratTrades) {
                    if (t.success === false) continue;
                    const amt = t.amount ?? 0;
                    const tokens = t.tokens ?? 0;
                    const fee = t.fee ?? 0;
                    if (t.side === "buy_up") { spent += amt; fees += fee; upTokens += tokens; }
                    else if (t.side === "buy_down") { spent += amt; fees += fee; downTokens += tokens; }
                    else if (t.side === "sell_up") { received += amt; fees += fee; upTokens -= tokens; }
                    else if (t.side === "sell_down") { received += amt; fees += fee; downTokens -= tokens; }
                  }
                  let resolutionCredit = 0;
                  if (outcome === "Up") resolutionCredit = Math.max(0, upTokens);
                  else if (outcome === "Down") resolutionCredit = Math.max(0, downTokens);
                  tradePnl = Math.round((received - spent - fees + resolutionCredit) * 10000) / 10000;
                }
                return {
                  strategy: name,
                  name,
                  trades: stratTrades,
                  pnl: tradePnl,
                  starting_cash: logEntry ? (logEntry.cash - (logEntry.pnl ?? 0)) : null,
                  final_cash: logEntry?.cash ?? null,
                  outcome: outcome ?? logEntry?.outcome ?? null,
                  dry_run: resolutionsByStrategy[name]?.dry_run ?? logEntry?.dry_run ?? false,
                };
              });
              return { strategies };
            }
          } catch {}
        }
        return null;
      }

      // Merge per-worker sidecars
      const strategies: any[] = [];
      let firstData: any = null;
      for (const sidecar of workerSidecars) {
        try {
          const data = JSON.parse(fs.readFileSync(path.join(dir, sidecar), "utf-8"));
          strategies.push(data);
          if (!firstData) firstData = data;
        } catch {}
      }
      return { ...(firstData || {}), strategies };
    }),

  allTime: procedure
    .input(z.object({ prefix: z.string(), interval: z.number().optional() }))
    .query(({ input }) => {
      const prefix = input.prefix;
      const dir = safePath(MARKETS_DIR, prefix, "sessions");
      if (!dir || !fs.existsSync(dir)) {
        return { sessions: [], samples: [], total_sessions: 0, total_samples: 0, downsampled_points: 0 };
      }

      const files = fs
        .readdirSync(dir)
        .filter((f) => f.endsWith(".json") && !f.includes(".trading.") && !f.includes(".summary."))
        .sort();

      const cached = allTimeCache.get(prefix);
      if (cached && cached.fileCount === files.length && Date.now() - cached.generatedAt < 60_000) {
        return cached.data;
      }

      const downsampleIntervalS = input.interval ?? 10;

      const sessionRows: any[] = [];
      const allPoints: any[] = [];
      let totalSamples = 0;

      for (const file of files) {
        try {
          const filepath = path.join(dir, file);
          const raw = fs.readFileSync(filepath, "utf-8");
          const session = JSON.parse(raw);
          const samples = session.samples || [];
          totalSamples += samples.length;

          sessionRows.push({
            file,
            window_start: session.metadata?.window_start_utc || session.collection_started_utc || "",
            window_end: session.metadata?.window_end_utc || session.collection_ended_utc || "",
            outcome: session.outcome || null,
            initial_btc: session.initial_btc_price_usd ?? null,
            final_btc: session.final_btc_price_usd ?? null,
            sample_count: samples.length,
          });

          let lastPickedElapsed = -Infinity;
          for (let i = 0; i < samples.length; i++) {
            const s = samples[i];
            if (i === 0 || i === samples.length - 1 || s.elapsed_seconds - lastPickedElapsed >= downsampleIntervalS) {
              allPoints.push({
                t: s.timestamp_utc,
                btc: s.btc_price_usd ?? null,
                up: s.up_token_midpoint ?? null,
                down: s.down_token_midpoint ?? null,
              });
              lastPickedElapsed = s.elapsed_seconds;
            }
          }
        } catch {}
      }

      const result = {
        total_sessions: sessionRows.length,
        total_samples: totalSamples,
        downsampled_points: allPoints.length,
        sessions: sessionRows,
        samples: allPoints,
      };

      allTimeCache.set(prefix, {
        fileCount: files.length,
        generatedAt: Date.now(),
        data: result,
      });

      return result;
    }),

  archive: procedure
    .input(z.object({ prefix: z.string(), file: z.string() }))
    .mutation(({ input }) => {
      const sessionsDir = safePath(MARKETS_DIR, input.prefix, "sessions");
      if (!sessionsDir || !fs.existsSync(sessionsDir)) {
        return { ok: false, error: "Sessions directory not found" };
      }

      const filepath = path.join(sessionsDir, input.file);
      if (!filepath.startsWith(sessionsDir) || !fs.existsSync(filepath)) {
        return { ok: false, error: "Session not found" };
      }

      const archiveDir = path.join(MARKETS_DIR, input.prefix, "archive", "sessions");
      fs.mkdirSync(archiveDir, { recursive: true });

      fs.renameSync(filepath, path.join(archiveDir, input.file));

      const sidecarPrefix = input.file + ".trading.";
      const sidecars = fs.readdirSync(sessionsDir).filter(
        (f) => f.startsWith(sidecarPrefix) && f.endsWith(".json")
      );
      for (const sidecar of sidecars) {
        fs.renameSync(
          path.join(sessionsDir, sidecar),
          path.join(archiveDir, sidecar)
        );
      }

      return { ok: true, archived: input.file, sidecars: sidecars.length };
    }),
});
