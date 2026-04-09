import { z } from "zod";
import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import { MARKETS_DIR, safePath, listMarketPrefixes } from "../helpers";

export const liveRouter = router({
  prefixes: procedure.query(() => {
    const live: string[] = [];
    for (const prefix of listMarketPrefixes()) {
      const pidFile = `/tmp/fondaco-${prefix}-collector.pid`;
      try {
        const pid = parseInt(fs.readFileSync(pidFile, "utf-8").trim(), 10);
        process.kill(pid, 0);
        live.push(prefix);
      } catch {
        const liveFile = path.join(MARKETS_DIR, prefix, "live", "session.json");
        try { fs.unlinkSync(liveFile); } catch {}
      }
    }
    return live;
  }),

  session: procedure
    .input(z.object({ prefix: z.string() }))
    .query(({ input }) => {
      const filepath = safePath(MARKETS_DIR, input.prefix, "live", "session.json");
      if (!filepath || !fs.existsSync(filepath)) return null;
      return JSON.parse(fs.readFileSync(filepath, "utf-8"));
    }),
});
