import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import { MARKETS_DIR } from "../helpers";

export const marketRouter = router({
  pnl: procedure
    .input(z.object({ prefix: z.string() }))
    .query(({ input }) => {
      const logFile = path.join(MARKETS_DIR, input.prefix, "bot_log.jsonl");
      if (!fs.existsSync(logFile)) {
        return { overall: 0, last_24h: 0, last_1h: 0, by_strategy: {} as Record<string, any> };
      }

      const lines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
      const now = Date.now();
      const cutoff24h = now - 24 * 60 * 60 * 1000;
      const cutoff1h = now - 60 * 60 * 1000;

      let overall = 0;
      let last24h = 0;
      let last1h = 0;
      const byStrategy: Record<string, { overall: number; last_24h: number; last_1h: number }> = {};
      let dryOverall = 0;
      let dryLast24h = 0;
      let dryLast1h = 0;
      let hasDry = false;

      for (const line of lines) {
        if (!line) continue;
        try {
          const entry = JSON.parse(line);
          const pnl = entry.pnl as number;
          if (pnl === undefined || pnl === null) continue;

          const isDry = entry.dry_run === true;
          const ts = entry.timestamp ? new Date(entry.timestamp).getTime() : 0;

          if (isDry) {
            hasDry = true;
            dryOverall += pnl;
            if (ts >= cutoff24h) dryLast24h += pnl;
            if (ts >= cutoff1h) dryLast1h += pnl;
          } else {
            const strat = entry.strategy || "unknown";
            if (!byStrategy[strat]) byStrategy[strat] = { overall: 0, last_24h: 0, last_1h: 0 };

            overall += pnl;
            byStrategy[strat].overall += pnl;

            if (ts >= cutoff24h) {
              last24h += pnl;
              byStrategy[strat].last_24h += pnl;
            }
            if (ts >= cutoff1h) {
              last1h += pnl;
              byStrategy[strat].last_1h += pnl;
            }
          }
        } catch {}
      }

      const round = (n: number) => Math.round(n * 100) / 100;
      overall = round(overall);
      last24h = round(last24h);
      last1h = round(last1h);
      for (const k of Object.keys(byStrategy)) {
        byStrategy[k].overall = round(byStrategy[k].overall);
        byStrategy[k].last_24h = round(byStrategy[k].last_24h);
        byStrategy[k].last_1h = round(byStrategy[k].last_1h);
      }

      const result: any = { overall, last_24h: last24h, last_1h: last1h, by_strategy: byStrategy };
      if (hasDry) {
        result.dry_run = { overall: round(dryOverall), last_24h: round(dryLast24h), last_1h: round(dryLast1h) };
      }
      return result;
    }),
});
