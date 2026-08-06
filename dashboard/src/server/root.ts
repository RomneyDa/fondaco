import { router } from "./trpc";
import { sessionsRouter } from "./routers/sessions";
import { liveRouter } from "./routers/live";
import { botRouter } from "./routers/bot";
import { collectorRouter } from "./routers/collector";
import { optimizerRouter } from "./routers/optimizer";
import { portfolioRouter } from "./routers/portfolio";
import { strategyRouter } from "./routers/strategy";
import { marketRouter } from "./routers/market";
import { sportsRouter } from "./routers/sports";

export const appRouter = router({
  sessions: sessionsRouter,
  live: liveRouter,
  bot: botRouter,
  collector: collectorRouter,
  optimizer: optimizerRouter,
  portfolio: portfolioRouter,
  strategy: strategyRouter,
  market: marketRouter,
  sports: sportsRouter,
});

export type AppRouter = typeof appRouter;
