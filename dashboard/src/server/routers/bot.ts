import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { spawn } from "child_process";
import { router, procedure } from "../trpc";
import {
  MARKETS_DIR, PROJECT_ROOT, listMarketPrefixes,
  marketLiveDir, marketSessionsDir, marketWorkersDir, marketTradesFile,
  proxyToControl, getControlPort,
} from "../helpers";
import { safeName, safeSlugKey } from "../controlGuard";

function enrichWorkers(slugKey: string, workers: any[]): any[] {
  const posDir = path.join(MARKETS_DIR, slugKey, "bot_positions");
  return workers.map((w: any) => {
    const enriched = { ...w, name: w.strategy || w.name };
    // State file is source of truth for bankroll, session_cash_ratio, etc.
    try {
      const posFile = path.join(posDir, `${enriched.name}.json`);
      if (fs.existsSync(posFile)) {
        const pos = JSON.parse(fs.readFileSync(posFile, "utf-8"));
        enriched.bankroll = pos.bankroll ?? pos.cash ?? enriched.bankroll ?? 0;
        enriched.cash = pos.cash ?? enriched.cash ?? 0;
        enriched.session_cash_ratio = pos.session_cash_ratio ?? enriched.session_cash_ratio;
        enriched.session_count = pos.session_count ?? enriched.session_count;
      }
    } catch {}
    enriched.dry_run = enriched.dry_run ?? false;
    return enriched;
  });
}

function normalizeMarkets(data: any): any[] {
  if (Array.isArray(data.markets)) return data.markets;
  if (data.markets && typeof data.markets === "object") {
    return Object.entries(data.markets).map(([key, val]: [string, any]) => ({
      slug_key: key,
      slug_prefix: key + "-",
      running: val.collector?.alive ?? false,
      session_count: 0,
      trading_enabled: (val.workers?.length ?? 0) > 0,
      strategies: enrichWorkers(key, val.workers ?? []),
    }));
  }
  return [];
}

export const botRouter = router({
  status: procedure.query(async () => {
    const { status, data } = await proxyToControl("/status");
    if (status < 500) return { ...data, markets: normalizeMarkets(data), running: true };

    // Fallback: read from files
    const prefixes = listMarketPrefixes();
    const markets: Record<string, any> = {};

    for (const prefix of prefixes) {
      const collectorState = path.join(MARKETS_DIR, prefix, "collector_state.json");
      const positionsDir = path.join(MARKETS_DIR, prefix, "bot_positions");
      let collector: any = null;
      const workers: any[] = [];

      if (fs.existsSync(collectorState)) {
        try {
          collector = JSON.parse(fs.readFileSync(collectorState, "utf-8"));
          if (collector.pid) {
            try { process.kill(collector.pid, 0); collector.alive = true; }
            catch { collector.alive = false; }
          }
        } catch {}
      }

      if (fs.existsSync(positionsDir)) {
        for (const f of fs.readdirSync(positionsDir).filter(f => f.endsWith(".json"))) {
          try {
            const w = JSON.parse(fs.readFileSync(path.join(positionsDir, f), "utf-8"));
            if (w.pid) {
              try { process.kill(w.pid, 0); w.alive = true; }
              catch { w.alive = false; }
            }
            workers.push(w);
          } catch {}
        }
      }

      markets[prefix] = { collector, workers };
    }

    return { markets };
  }),

  start: procedure
    .input(z.object({
      slug_prefix: z.string().optional(),
      strategies: z.array(z.string()).optional(),
      window: z.number().optional(),
      restore: z.boolean().optional(),
    }))
    .mutation(async ({ input }) => {
      const slug_prefix = input.slug_prefix;
      if (!slug_prefix) {
        // Start orchestrator if not already running
        if (!getControlPort()) {
          const bin = path.join(PROJECT_ROOT, "target", "release", "orchestrator");
          if (!fs.existsSync(bin)) {
            return { ok: false, error: "orchestrator binary not found — run cargo build --release" };
          }
          const child = spawn(bin, [], {
            cwd: PROJECT_ROOT,
            detached: true,
            stdio: "ignore",
          });
          child.unref();
          // Wait for it to write app.json
          await new Promise((resolve) => setTimeout(resolve, 3000));
          if (!getControlPort()) {
            return { ok: false, error: "Orchestrator started but app.json not found after 3s" };
          }
          // Restore workers if requested
          if (input.restore) {
            await proxyToControl("/start", "POST", { restore: true });
          }
          return { ok: true, pid: child.pid };
        }
        // Already running — proxy to it
        const { status, data } = await proxyToControl("/start", "POST", { restore: input.restore });
        if (status >= 400) return { ok: false, error: data?.error || "failed" };
        return { ok: true, pid: data?.pid };
      }

      if (!safeSlugKey(slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      for (const strat of input.strategies ?? []) {
        if (!safeName(strat)) return { ok: false, error: "invalid strategy" };
      }

      const pids: Record<string, number | undefined> = {};
      const errors: string[] = [];

      const collectorResult = await proxyToControl("/market-on", "POST", { slug_prefix, window: input.window });
      if (collectorResult.status >= 400) {
        errors.push(`collector: ${collectorResult.data?.error || "failed"}`);
      } else {
        pids["collector"] = collectorResult.data?.pid;
      }

      if (input.strategies && Array.isArray(input.strategies)) {
        for (const strat of input.strategies) {
          const workerResult = await proxyToControl("/trade-on", "POST", { slug_prefix, strategy: strat });
          if (workerResult.status >= 400) {
            errors.push(`worker ${strat}: ${workerResult.data?.error || "failed"}`);
          } else {
            pids[`worker:${strat}`] = workerResult.data?.pid;
          }
        }
      }

      if (errors.length > 0) {
        return { ok: false, error: errors.join("; "), pids };
      }
      return { ok: true, pids };
    }),

  stop: procedure.mutation(async () => {
    const { status, data } = await proxyToControl("/stop", "POST");
    return status < 400 ? { ok: true } : { ok: false, error: data?.error };
  }),

  stopAll: procedure.mutation(async () => {
    const { status, data } = await proxyToControl("/stop-all", "POST");
    return status < 400 ? { ok: true } : { ok: false, error: data?.error };
  }),

  kill: procedure.mutation(async () => {
    const { status, data } = await proxyToControl("/kill-all", "POST");
    return status < 400 ? { ok: true } : { ok: false, error: data?.error };
  }),

  marketOn: procedure
    .input(z.object({
      slug_prefix: z.string(),
      window: z.number().optional(),
      market_type: z.string().optional(),
      restore: z.boolean().optional(),
    }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      const { status, data } = await proxyToControl("/market-on", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  marketOff: procedure
    .input(z.object({
      slug_prefix: z.string(),
      after_session: z.boolean().optional(),
    }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      const { status, data } = await proxyToControl("/market-off", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  tradeOn: procedure
    .input(z.object({
      slug_prefix: z.string(),
      strategy: z.string(),
      params: z.record(z.string()).optional(),
      cash: z.number().optional(),
      dry_run: z.boolean().optional(),
      session_cap: z.number().optional(),
      session_cash_ratio: z.object({ ratio: z.number(), max: z.number() }).optional(),
    }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      if (!safeName(input.strategy)) return { ok: false, error: "invalid strategy" };
      const { status, data } = await proxyToControl("/trade-on", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  tradeOff: procedure
    .input(z.object({
      slug_prefix: z.string(),
      strategy: z.string().optional(),
    }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      if (input.strategy && !safeName(input.strategy)) return { ok: false, error: "invalid strategy" };
      const { status, data } = await proxyToControl("/trade-off", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  reload: procedure
    .input(z.object({ slug_prefix: z.string(), strategy: z.string() }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      if (!safeName(input.strategy)) return { ok: false, error: "invalid strategy" };
      const { status, data } = await proxyToControl("/reload", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  setCash: procedure
    .input(z.object({
      slug_prefix: z.string(),
      cash: z.number(),
      strategy: z.string().optional(),
    }))
    .mutation(async ({ input }) => {
      if (!safeSlugKey(input.slug_prefix)) return { ok: false, error: "invalid slug_prefix" };
      if (input.strategy && !safeName(input.strategy)) return { ok: false, error: "invalid strategy" };
      const { status, data } = await proxyToControl("/set-cash", "POST", input);
      return status < 400 ? { ok: true, ...data } : { ok: false, error: data?.error };
    }),

  setAutoExit: procedure
    .input(z.object({
      slug_prefix: z.string(),
      enabled: z.boolean(),
      strategy: z.string().optional(),
    }))
    .mutation(async () => {
      return { ok: false, error: "Auto-exit not applicable in Rust architecture" };
    }),

  trades: procedure
    .input(z.object({ limit: z.number().optional() }))
    .query(({ input }) => {
      const limit = Math.min(Math.max(input.limit ?? 100, 1), 500);
      const allTrades: any[] = [];

      for (const prefix of listMarketPrefixes()) {
        const liveDir = marketLiveDir(prefix);
        const sessionsDir = marketSessionsDir(prefix);
        const workersDir = marketWorkersDir(prefix);
        const tradesFile = marketTradesFile(prefix);

        if (fs.existsSync(liveDir)) {
          for (const file of fs.readdirSync(liveDir).filter((f) => f.endsWith(".json"))) {
            try {
              const data = JSON.parse(fs.readFileSync(path.join(liveDir, file), "utf-8"));
              const sessionStart = data.metadata?.window_start_utc || data.collection_started_utc || "";
              const trading = data.trading;
              if (trading?.strategies) {
                for (const strat of trading.strategies) {
                  if (strat.trades?.length) {
                    for (const t of strat.trades) {
                      allTrades.push({ ...t, session_start: sessionStart, strategy: strat.name, market: prefix });
                    }
                  }
                }
              } else if (trading?.trades?.length) {
                for (const t of trading.trades) {
                  allTrades.push({ ...t, session_start: sessionStart, market: prefix });
                }
              }
            } catch {}
          }
        }

        if (fs.existsSync(sessionsDir)) {
          const sidecars = fs.readdirSync(sessionsDir)
            .filter((f) => f.includes(".trading.") && f.endsWith(".json"))
            .sort()
            .reverse()
            .slice(0, 40);
          for (const sidecar of sidecars) {
            try {
              const data = JSON.parse(fs.readFileSync(path.join(sessionsDir, sidecar), "utf-8"));
              const sessionStart = sidecar.replace(/\.json\.trading.*$/, "");
              if (data.strategies) {
                for (const strat of data.strategies) {
                  if (strat.trades?.length) {
                    for (const t of strat.trades) {
                      allTrades.push({ ...t, session_start: sessionStart, strategy: strat.strategy || strat.name, market: prefix });
                    }
                  }
                }
              } else if (data.trades?.length) {
                for (const t of data.trades) {
                  allTrades.push({ ...t, session_start: sessionStart, strategy: data.strategy, market: prefix });
                }
              }
            } catch {}
          }
        }

        if (fs.existsSync(workersDir)) {
          try {
            const workerDirs = fs.readdirSync(workersDir, { withFileTypes: true })
              .filter((e) => e.isDirectory());
            for (const wd of workerDirs) {
              const livePath = path.join(workersDir, wd.name, "live_trades.json");
              if (fs.existsSync(livePath)) {
                try {
                  const data = JSON.parse(fs.readFileSync(livePath, "utf-8"));
                  if (data.trades?.length) {
                    for (const t of data.trades) {
                      allTrades.push({ ...t, session_start: "", strategy: data.strategy || wd.name, market: prefix });
                    }
                  }
                } catch {}
              }
            }
          } catch {}
        }

        if (fs.existsSync(tradesFile)) {
          try {
            const lines = fs.readFileSync(tradesFile, "utf-8").trim().split("\n");
            const recentLines = lines.slice(-limit * 2);
            for (const line of recentLines) {
              if (!line) continue;
              try {
                const entry = JSON.parse(line);
                if (entry.type === "resolution") {
                  allTrades.push({ ...entry, _type: "resolution", session_start: entry.timestamp, market: prefix });
                } else if (entry.strategy) {
                  allTrades.push({ ...entry, market: prefix });
                }
              } catch {}
            }
          } catch {}
        }
      }

      // Deduplicate
      const seen = new Set<string>();
      const deduped: any[] = [];
      for (const t of allTrades) {
        if (t._type === "resolution") { deduped.push(t); continue; }
        const key = t.order_id || `${t.timestamp}-${t.side}-${t.strategy}`;
        if (!seen.has(key)) { seen.add(key); deduped.push(t); }
      }

      deduped.sort((a, b) => {
        const ta = a.timestamp || a.session_start || "";
        const tb = b.timestamp || b.session_start || "";
        return tb.localeCompare(ta);
      });

      return deduped.slice(0, limit);
    }),
});
