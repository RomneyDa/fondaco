import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import { MARKETS_DIR, PROJECT_ROOT, marketSessionsDir, marketTradesFile } from "../helpers";

export const strategyRouter = router({
  list: procedure.query(() => {
    try {
      const modFile = path.join(PROJECT_ROOT, "src", "strategies", "mod.rs");
      if (fs.existsSync(modFile)) {
        const content = fs.readFileSync(modFile, "utf-8");
        const names: string[] = [];
        const re = /m\.insert\("(\w+)"/g;
        let m;
        while ((m = re.exec(content)) !== null) {
          names.push(m[1]);
        }
        return names.sort();
      }
      return [];
    } catch {
      return [];
    }
  }),

  stats: procedure
    .input(z.object({
      market: z.string(),
      strategy: z.string(),
      limit: z.number().optional(),
    }))
    .query(({ input }) => {
      const { market, strategy } = input;

      const logFile = path.join(MARKETS_DIR, market, "bot_log.jsonl");
      const sessions: any[] = [];
      if (fs.existsSync(logFile)) {
        const lines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
        for (const line of lines) {
          if (!line) continue;
          try {
            const entry = JSON.parse(line);
            if (entry.strategy === strategy && entry.pnl !== undefined && entry.dry_run !== true) {
              sessions.push(entry);
            }
          } catch {}
        }
      }

      const tradesFile = marketTradesFile(market);
      const trades: any[] = [];
      const resolutions: any[] = [];
      if (fs.existsSync(tradesFile)) {
        const lines = fs.readFileSync(tradesFile, "utf-8").trim().split("\n");
        for (const line of lines) {
          if (!line) continue;
          try {
            const entry = JSON.parse(line);
            if (entry.type === "resolution" && entry.strategy === strategy) {
              resolutions.push(entry);
            } else if (entry.strategy === strategy) {
              trades.push(entry);
            }
          } catch {}
        }
      }

      const sessionsDir = marketSessionsDir(market);
      if (fs.existsSync(sessionsDir)) {
        const files = fs.readdirSync(sessionsDir).sort().reverse().slice(0, 100);
        for (const f of files) {
          if (f.endsWith(`.trading.${strategy}.json`)) {
            try {
              const data = JSON.parse(fs.readFileSync(path.join(sessionsDir, f), "utf-8"));
              const sessionName = f.replace(`.trading.${strategy}.json`, "");
              const strats = data.strategies || [data];
              for (const s of strats) {
                if (s.name === strategy || s.strategy === strategy) {
                  for (const t of s.trades || []) {
                    trades.push({ ...t, strategy, session_start: sessionName, market });
                  }
                }
              }
            } catch {}
          }
        }
      }

      // Deduplicate trades
      {
        const seenIdx = new Map<string, number>();
        const deduped: any[] = [];
        for (const t of trades) {
          const key = t.order_id || `${t.timestamp}-${t.side}`;
          const existing = seenIdx.get(key);
          if (existing != null) {
            if (t.fee != null && deduped[existing].fee == null) {
              deduped[existing] = t;
            }
          } else {
            seenIdx.set(key, deduped.length);
            deduped.push(t);
          }
        }
        trades.length = 0;
        trades.push(...deduped);
      }

      // Build in-progress sessions from unresolved trades
      const lastResolutionTime = resolutions.length > 0
        ? resolutions[resolutions.length - 1].timestamp
        : null;
      const unresolvedTrades = trades.filter((t) => {
        if (!t.timestamp) return false;
        if (lastResolutionTime && t.timestamp <= lastResolutionTime) return false;
        return true;
      });
      if (unresolvedTrades.length > 0) {
        const windows: any[][] = [];
        let currentWindow: any[] = [unresolvedTrades[0]];
        for (let i = 1; i < unresolvedTrades.length; i++) {
          const prev = new Date(unresolvedTrades[i - 1].timestamp).getTime();
          const curr = new Date(unresolvedTrades[i].timestamp).getTime();
          if (curr - prev > 6 * 60 * 1000) {
            windows.push(currentWindow);
            currentWindow = [unresolvedTrades[i]];
          } else {
            currentWindow.push(unresolvedTrades[i]);
          }
        }
        windows.push(currentWindow);

        let liveTicks: number | null = null;
        let liveWindowEnd: string | null = null;
        let liveWindowStart: string | null = null;
        let liveElapsed: number | null = null;
        try {
          const liveFile = path.join(MARKETS_DIR, market, "live", "session.json");
          if (fs.existsSync(liveFile)) {
            const liveData = JSON.parse(fs.readFileSync(liveFile, "utf-8"));
            if (liveData.samples) {
              liveTicks = liveData.samples.length;
              liveElapsed = liveData.samples.length > 0
                ? liveData.samples[liveData.samples.length - 1].elapsed_seconds
                : 0;
            }
            if (liveData.metadata) {
              liveWindowEnd = liveData.metadata.window_end_utc || null;
              liveWindowStart = liveData.metadata.window_start_utc || null;
            }
          }
        } catch {}

        for (const windowTrades of windows) {
          const successfulTrades = windowTrades.filter((t: any) => t.success !== false);
          const totalSpent = successfulTrades.reduce((sum: number, t: any) => sum + (t.amount || 0), 0);
          sessions.push({
            timestamp: windowTrades[0].timestamp,
            strategy,
            strategy_version: windowTrades[0].strategy_version || null,
            outcome: "active",
            trades: successfulTrades.length,
            pnl: -totalSpent,
            credit: 0,
            cash: null,
            session_number: null,
            _active: true,
            _tick_index: liveTicks,
            _elapsed_seconds: liveElapsed,
            _window_end_utc: liveWindowEnd,
            _window_start_utc: liveWindowStart,
          });
        }
      }

      trades.sort((a, b) => {
        const ta = a.timestamp || a.session_start || "";
        const tb = b.timestamp || b.session_start || "";
        return tb.localeCompare(ta);
      });

      let totalFees = 0;
      for (const t of trades) {
        if (typeof t.fee === "number") totalFees += t.fee;
      }
      for (const r of resolutions) {
        if (typeof r.fee === "number") totalFees += r.fee;
      }

      const resolvedSessions = sessions.filter((s) => !s._active);
      const pnls = resolvedSessions.map((s) => s.pnl as number);
      const totalPnl = pnls.reduce((a, b) => a + b, 0);
      const tradedSessions = resolvedSessions.filter((s) => s.trades > 0);
      const tradedPnls = tradedSessions.map((s) => s.pnl as number);
      const wins = tradedPnls.filter((p) => p > 0).length;
      const losses = tradedPnls.filter((p) => p < 0).length;

      let sharpe = 0;
      if (tradedPnls.length >= 2) {
        const mean = tradedPnls.reduce((a, b) => a + b, 0) / tradedPnls.length;
        const variance = tradedPnls.reduce((a, b) => a + (b - mean) ** 2, 0) / (tradedPnls.length - 1);
        const std = Math.sqrt(variance);
        if (std > 0) sharpe = mean / std;
      }

      const stateFile = path.join(MARKETS_DIR, market, "workers", strategy, "state.json");
      let currentVersion: string | null = null;
      if (fs.existsSync(stateFile)) {
        try {
          const state = JSON.parse(fs.readFileSync(stateFile, "utf-8"));
          currentVersion = state.version || null;
        } catch {}
      }

      const versionSummary: Record<string, { sessions: number; traded: number; pnl: number; wins: number; losses: number; first: string; last: string }> = {};
      for (const s of resolvedSessions) {
        const v = s.strategy_version || "unknown";
        if (!versionSummary[v]) {
          versionSummary[v] = { sessions: 0, traded: 0, pnl: 0, wins: 0, losses: 0, first: s.timestamp, last: s.timestamp };
        }
        const vs = versionSummary[v];
        vs.sessions++;
        vs.pnl += s.pnl;
        vs.last = s.timestamp;
        if (s.trades > 0) {
          vs.traded++;
          if (s.pnl > 0) vs.wins++;
          else if (s.pnl < 0) vs.losses++;
        }
      }
      for (const v of Object.values(versionSummary)) {
        v.pnl = Math.round(v.pnl * 100) / 100;
      }

      const limit = input.limit ?? 200;

      return {
        strategy,
        market,
        stats: {
          total_sessions: resolvedSessions.length,
          traded_sessions: tradedSessions.length,
          total_pnl: Math.round(totalPnl * 100) / 100,
          wins,
          losses,
          entry_rate: resolvedSessions.length > 0 ? Math.round((tradedSessions.length / resolvedSessions.length) * 1000) / 10 : 0,
          win_rate: tradedSessions.length > 0 ? Math.round((wins / tradedSessions.length) * 1000) / 10 : 0,
          sharpe: Math.round(sharpe * 1000) / 1000,
          current_cash: (() => {
            if (fs.existsSync(stateFile)) {
              try {
                const state = JSON.parse(fs.readFileSync(stateFile, "utf-8"));
                if (state.bankroll != null) return state.bankroll;
              } catch {}
            }
            return sessions.length > 0 ? sessions[sessions.length - 1].cash : null;
          })(),
          current_version: currentVersion,
          total_fees: Math.round(totalFees * 100) / 100,
        },
        sessions: (() => {
          if (sessions.length <= limit) return sessions;
          const cutoff = sessions.length - limit;
          const early = sessions.slice(0, cutoff).filter((s: any) => s.trades > 0 || s.type === "adjustment" || s.pnl !== 0);
          return [...early, ...sessions.slice(cutoff)];
        })(),
        trades: trades.slice(0, 500),
        resolutions: resolutions.slice(-200),
        version_summary: versionSummary,
      };
    }),
});
