import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { spawn } from "child_process";
import { router, procedure } from "../trpc";
import { DATA_DIR, MARKETS_DIR, PROJECT_ROOT, listMarketPrefixes } from "../helpers";
import { safeName, safeSlugKey } from "../controlGuard";

export const optimizerRouter = router({
  status: procedure.query(() => {
    try {
      const prefixes = listMarketPrefixes();
      const results: Record<string, any> = {};
      for (const prefix of prefixes) {
        const logFile = path.join(MARKETS_DIR, prefix, "backtest_log.jsonl");
        if (fs.existsSync(logFile)) {
          const stat = fs.statSync(logFile);
          results[prefix] = {
            last_run: stat.mtime.toISOString(),
            size_bytes: stat.size,
          };
        }
      }
      return { mode: "offline", results };
    } catch {
      return { mode: "offline", results: {} };
    }
  }),

  events: procedure.query(() => {
    try {
      const prefix = listMarketPrefixes()[0];
      if (!prefix) return [];
      const logFile = path.join(MARKETS_DIR, prefix, "backtest_log.jsonl");
      if (!fs.existsSync(logFile)) return [];
      const lines = fs.readFileSync(logFile, "utf-8").trim().split("\n").slice(-50);
      return lines.map(line => { try { return JSON.parse(line); } catch { return null; } }).filter(Boolean);
    } catch {
      return [];
    }
  }),

  config: procedure.query(() => {
    try {
      const configPath = path.join(DATA_DIR, "optimizer", "config.json");
      if (fs.existsSync(configPath)) {
        return JSON.parse(fs.readFileSync(configPath, "utf-8"));
      }
      return {};
    } catch {
      return {};
    }
  }),

  updateConfig: procedure
    .input(z.object({
      interval_minutes: z.number().optional(),
      eval_sessions: z.number().optional(),
      min_sharpe: z.number().optional(),
      starting_cash: z.number().optional(),
    }))
    .mutation(({ input }) => {
      try {
        const configDir = path.join(DATA_DIR, "optimizer");
        if (!fs.existsSync(configDir)) fs.mkdirSync(configDir, { recursive: true });
        const configPath = path.join(configDir, "config.json");
        fs.writeFileSync(configPath, JSON.stringify(input, null, 2));
        return { ok: true };
      } catch (err) {
        return { ok: false, error: String(err) };
      }
    }),

  start: procedure
    .input(z.object({
      slug_prefix: z.string().optional(),
      strategy: z.string().optional(),
      interval: z.number().optional(),
      eval_sessions: z.number().optional(),
      min_sharpe: z.number().optional(),
      starting_cash: z.number().optional(),
    }))
    .mutation(async ({ input }) => {
      try {
        const slug_prefix = input.slug_prefix || listMarketPrefixes()[0];
        if (!slug_prefix) return { ok: false, error: "No markets available" };
        const slugKey = safeSlugKey(slug_prefix);
        if (!slugKey) return { ok: false, error: "invalid slug_prefix" };
        if (input.strategy && input.strategy !== "all" && !safeName(input.strategy)) {
          return { ok: false, error: "invalid strategy" };
        }
        const sessionsDir = path.join(MARKETS_DIR, slugKey, "sessions");
        const bin = path.join(PROJECT_ROOT, "target", "release", "backtest");
        const args = [sessionsDir, "--strategy", input.strategy || "all", "--sweep", "--recent", "30"];
        let spawnErr: string | null = null;
        const child = spawn(bin, args, {
          cwd: PROJECT_ROOT,
          detached: true,
          stdio: "ignore",
        });
        child.on("error", (err) => { spawnErr = err.message; });
        child.unref();
        await new Promise((resolve) => setTimeout(resolve, 500));
        if (spawnErr) {
          return { ok: false, error: `backtest spawn failed: ${spawnErr}` };
        }
        return { ok: true, pid: child.pid, mode: "offline_sweep" };
      } catch (err) {
        return { ok: false, error: String(err) };
      }
    }),

  stop: procedure.mutation(() => {
    return { ok: true, note: "Optimizer runs as a one-shot backtest process; it exits when complete." };
  }),
});
