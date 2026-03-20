import * as fs from "fs";
import * as path from "path";
import { router, procedure } from "../trpc";
import {
  PROJECT_ROOT, fetchPortfolioValue,
  fetchAllPositions, fetchUsdcBalance, getSafeAddress,
} from "../helpers";

export const portfolioRouter = router({
  balance: procedure.query(async () => {
    const value = await fetchPortfolioValue();
    return { portfolio: value };
  }),

  positions: procedure.query(async () => {
    const safeAddress = getSafeAddress();
    if (!safeAddress) return { positions: [], usdc: null };

    const [positions, usdc] = await Promise.all([
      fetchAllPositions(safeAddress),
      fetchUsdcBalance(safeAddress),
    ]);

    return { positions: positions ?? [], usdc };
  }),

  slugs: procedure.query(() => {
    const slugs: Array<{
      slug_prefix: string;
      window_duration_seconds: number;
      asset: string;
      example_title: string;
    }> = [];

    const marketsFile = path.join(PROJECT_ROOT, "src", "markets.rs");
    if (fs.existsSync(marketsFile)) {
      const content = fs.readFileSync(marketsFile, "utf-8");
      const re = /MarketConfig::new\("([^"]+)",\s*(\d+)/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(content)) !== null) {
        const prefix = m[1];
        const key = prefix.replace(/-$/, "");
        const parts = key.match(/^(\w+)-updown-(\d+)m$/);
        const asset = parts ? parts[1].toUpperCase() : key;
        const durationMin = parts ? parseInt(parts[2], 10) : parseInt(m[2], 10) / 60;
        slugs.push({
          slug_prefix: prefix,
          window_duration_seconds: parseInt(m[2], 10),
          asset,
          example_title: `${asset} ${durationMin}m Up/Down`,
        });
      }
    }

    return slugs;
  }),
});
