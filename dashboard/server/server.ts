import express, { Request, Response } from "express";
import { watch } from "chokidar";
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DATA_DIR = path.resolve(__dirname, "../../data");
const MARKETS_DIR = path.join(DATA_DIR, "markets");
const APP_JSON = path.join(DATA_DIR, "app.json");
const CLIENT_DIR = path.resolve(__dirname, "../dist/client");
const PORT = parseInt(process.env.PORT || "3456", 10);

const app = express();
app.use(express.json());
const sseClients = new Set<Response>();

// --- Helpers ---

function safePath(base: string, ...segments: string[]): string | null {
  const resolved = path.resolve(base, ...segments);
  if (!resolved.startsWith(base)) return null;
  return resolved;
}

function getControlPort(): number | null {
  try {
    const data = JSON.parse(fs.readFileSync(APP_JSON, "utf-8"));
    return data.port || null;
  } catch {
    return null;
  }
}

function marketSessionsDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "sessions");
}

function marketLiveDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "live");
}

function marketWorkersDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "workers");
}

function marketTradesFile(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "bot_trades.jsonl");
}

/** List all market slug keys that have a sessions directory */
function listMarketPrefixes(): string[] {
  try {
    if (!fs.existsSync(MARKETS_DIR)) return [];
    return fs
      .readdirSync(MARKETS_DIR, { withFileTypes: true })
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .filter((name) => fs.existsSync(path.join(MARKETS_DIR, name, "sessions")))
      .sort();
  } catch {
    return [];
  }
}

async function proxyToControl(
  endpoint: string,
  method: "GET" | "POST" = "GET",
  body?: object
): Promise<{ status: number; data: any }> {
  const port = getControlPort();
  if (!port) {
    return { status: 503, data: { error: "App not running (no app.json)" } };
  }
  try {
    const opts: RequestInit = { method };
    if (body && method === "POST") {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(`http://localhost:${port}${endpoint}`, opts);
    const data = await res.json();
    return { status: res.status, data };
  } catch (e) {
    return {
      status: 502,
      data: { error: `Cannot reach control server on port ${port}: ${e}` },
    };
  }
}

// --- REST API ---

app.get("/api/prefixes", (_req: Request, res: Response) => {
  try {
    const prefixes = listMarketPrefixes();
    res.json({ prefixes });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/sessions/:prefix/summaries", (req: Request, res: Response) => {
  try {
    const dir = safePath(MARKETS_DIR, req.params.prefix, "sessions");
    if (!dir) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
    if (!fs.existsSync(dir)) {
      res.json({ sessions: [] });
      return;
    }
    const files = fs
      .readdirSync(dir)
      .filter((f) => f.endsWith(".json") && !f.includes(".trading."))
      .sort()
      .reverse();

    const limit = Math.min(
      parseInt(req.query.limit as string) || 20,
      100
    );
    const summaries = files.slice(0, limit).map((file) => {
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
    res.json({ sessions: summaries });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

/**
 * Session stats: pre-computed per-session statistics from the cache.
 *
 * Query params:
 *   ?last=N        — return the N most recent stats (default: all)
 *   ?before=ISO    — only stats before this timestamp
 *   ?after=ISO     — only stats after this timestamp (for delta fetches)
 *   ?session=ISO   — return stats for a specific session
 */
app.get("/api/sessions/:prefix/stats", async (req: Request, res: Response) => {
  try {
    // Build query string to proxy to the Python control server
    const params = new URLSearchParams();
    params.set("slug_prefix", req.params.prefix);
    for (const key of ["last", "before", "after", "session"]) {
      if (req.query[key]) params.set(key, req.query[key] as string);
    }
    const { status, data } = await proxyToControl(
      `/api/control/session-stats?${params.toString()}`
    );
    res.status(status).json(data);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/sessions/:prefix", (req: Request, res: Response) => {
  try {
    const dir = safePath(MARKETS_DIR, req.params.prefix, "sessions");
    if (!dir) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
    if (!fs.existsSync(dir)) {
      res.json({ sessions: [] });
      return;
    }
    const files = fs
      .readdirSync(dir)
      .filter((f) => f.endsWith(".json") && !f.includes(".trading."))
      .sort()
      .reverse();
    res.json({ sessions: files });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/session/:prefix/:file", (req: Request, res: Response) => {
  try {
    const filepath = safePath(
      MARKETS_DIR,
      req.params.prefix,
      "sessions",
      req.params.file
    );
    if (!filepath) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
    if (!fs.existsSync(filepath)) {
      res.status(404).json({ error: "Not found" });
      return;
    }
    const data = JSON.parse(fs.readFileSync(filepath, "utf-8"));
    res.json(data);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get(
  "/api/session/:prefix/:file/trading",
  (req: Request, res: Response) => {
    try {
      const dir = safePath(MARKETS_DIR, req.params.prefix, "sessions");
      if (!dir) {
        res.status(403).json({ error: "Forbidden" });
        return;
      }

      // Try legacy single sidecar first
      const legacyFile = req.params.file + ".trading.json";
      const legacyPath = path.join(dir, legacyFile);
      if (fs.existsSync(legacyPath)) {
        const data = JSON.parse(fs.readFileSync(legacyPath, "utf-8"));
        res.json(data);
        return;
      }

      // Look for per-worker sidecars: {file}.trading.{strategy}.json
      const prefix = req.params.file + ".trading.";
      const workerSidecars = fs.existsSync(dir)
        ? fs.readdirSync(dir).filter(
            (f) => f.startsWith(prefix) && f.endsWith(".json") && f !== legacyFile
          )
        : [];

      if (workerSidecars.length === 0) {
        // Fallback: reconstruct trading data from bot_trades.jsonl
        const sessionName = req.params.file.replace(".json", "");
        const tradesFile = marketTradesFile(req.params.prefix);
        if (fs.existsSync(tradesFile)) {
          try {
            const lines = fs.readFileSync(tradesFile, "utf-8").trim().split("\n");
            // Parse session time window (5-minute windows)
            const sessionTs = sessionName.replace(/T(\d{2})-(\d{2})-(\d{2})Z?$/, "T$1:$2:$3Z");
            const windowStart = new Date(sessionTs);
            const windowEnd = new Date(windowStart.getTime() + 5 * 60 * 1000);
            const byStrategy: Record<string, any[]> = {};

            for (const line of lines) {
              if (!line) continue;
              try {
                const entry = JSON.parse(line);
                if (entry.type === "resolution" || !entry.timestamp || !entry.strategy) continue;
                // Skip unfilled GTC/GTD placements (fee is null for placements,
                // non-null for actual fills). Including these creates phantom
                // trades that never executed. But keep successful trades even
                // if fee is null (some strategies don't log fees).
                if (entry.order_type && entry.fee == null && entry.success !== true) continue;
                const ts = new Date(entry.timestamp);
                if (ts >= windowStart && ts < windowEnd) {
                  if (!byStrategy[entry.strategy]) byStrategy[entry.strategy] = [];
                  byStrategy[entry.strategy].push(entry);
                }
              } catch {}
            }

            // Deduplicate by order_id: GTC orders are logged once at
            // placement (no fee) and again at fill (with fee). Keep the
            // fill record (has fee) and drop the placement duplicate.
            for (const name of Object.keys(byStrategy)) {
              const seen = new Map<string, number>();
              const trades = byStrategy[name];
              const deduped: any[] = [];
              for (const t of trades) {
                const key = t.order_id || `${t.timestamp}-${t.side}`;
                const existing = seen.get(key);
                if (existing != null) {
                  // Prefer the record with a fee (the fill)
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
              // Enrich with PnL data from bot_log.jsonl
              const logFile = path.join(MARKETS_DIR, req.params.prefix, "bot_log.jsonl");
              const logByStrategy: Record<string, any> = {};
              if (fs.existsSync(logFile)) {
                const logLines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
                // Match by timestamp (resolution time = window end)
                const resolutionTs = windowEnd.toISOString();
                for (const ll of logLines) {
                  if (!ll) continue;
                  try {
                    const le = JSON.parse(ll);
                    if (le.strategy && le.timestamp) {
                      const leTs = new Date(le.timestamp);
                      // Resolution timestamp should be close to window end
                      if (Math.abs(leTs.getTime() - windowEnd.getTime()) < 60000) {
                        logByStrategy[le.strategy] = le;
                      }
                    }
                  } catch {}
                }
              }

              // Get session outcome from session file
              let outcome: string | null = null;
              const sessionFile = path.join(dir, req.params.file);
              if (fs.existsSync(sessionFile)) {
                try {
                  const sd = JSON.parse(fs.readFileSync(sessionFile, "utf-8"));
                  outcome = sd.outcome || null;
                } catch {}
              }

              const strategies = strategyNames.map((name) => {
                const logEntry = logByStrategy[name];
                const stratTrades = byStrategy[name];
                // Compute PnL from trades when bot_log is unreliable (optimizer reload)
                let tradePnl: number | null = null;
                if (logEntry?.pnl != null && logEntry.trades > 0) {
                  tradePnl = logEntry.pnl;
                } else if (stratTrades.length > 0) {
                  // Estimate PnL from trades, accounting for resolution
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
                  // Winning tokens pay $1 each at resolution, losing tokens pay $0
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
                  dry_run: false,
                };
              });
              res.json({ strategies });
              return;
            }
          } catch {}
        }
        res.status(404).json({ error: "No trading data" });
        return;
      }

      // Merge per-worker sidecars into a combined response
      const strategies: any[] = [];
      let firstData: any = null;
      for (const sidecar of workerSidecars) {
        try {
          const data = JSON.parse(
            fs.readFileSync(path.join(dir, sidecar), "utf-8")
          );
          strategies.push(data);
          if (!firstData) firstData = data;
        } catch {}
      }

      const merged = { ...(firstData || {}), strategies };
      res.json(merged);
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  }
);

app.post("/api/session/:prefix/:file/archive", (req: Request, res: Response) => {
  try {
    const sessionsDir = safePath(MARKETS_DIR, req.params.prefix, "sessions");
    if (!sessionsDir) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
    if (!fs.existsSync(sessionsDir)) {
      res.status(404).json({ error: "Sessions directory not found" });
      return;
    }

    const filepath = path.join(sessionsDir, req.params.file);
    if (!filepath.startsWith(sessionsDir) || !fs.existsSync(filepath)) {
      res.status(404).json({ error: "Session not found" });
      return;
    }

    const archiveDir = path.join(MARKETS_DIR, req.params.prefix, "archive", "sessions");
    fs.mkdirSync(archiveDir, { recursive: true });

    // Move main session file
    fs.renameSync(filepath, path.join(archiveDir, req.params.file));

    // Move any trading sidecar files (e.g. foo.json.trading.json, foo.json.trading.Strategy.json)
    const sidecarPrefix = req.params.file + ".trading.";
    const sidecars = fs.readdirSync(sessionsDir).filter(
      (f) => f.startsWith(sidecarPrefix) && f.endsWith(".json")
    );
    for (const sidecar of sidecars) {
      fs.renameSync(
        path.join(sessionsDir, sidecar),
        path.join(archiveDir, sidecar)
      );
    }

    res.json({ ok: true, archived: req.params.file, sidecars: sidecars.length });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/live", (_req: Request, res: Response) => {
  try {
    const live: string[] = [];
    for (const prefix of listMarketPrefixes()) {
      const liveFile = path.join(MARKETS_DIR, prefix, "live", "session.json");
      if (fs.existsSync(liveFile)) {
        live.push(prefix);
      }
    }
    res.json({ live });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/live/:prefix", (req: Request, res: Response) => {
  try {
    const filepath = safePath(MARKETS_DIR, req.params.prefix, "live", "session.json");
    if (!filepath) {
      res.status(403).json({ error: "Forbidden" });
      return;
    }
    if (!fs.existsSync(filepath)) {
      res.status(404).json({ error: "No active session" });
      return;
    }
    const data = JSON.parse(fs.readFileSync(filepath, "utf-8"));
    res.json(data);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// --- Bot control proxy ---

app.get("/api/bot/status", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/status");
  res.status(status).json(data);
});

app.post("/api/bot/market-on", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/market-on",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/market-off", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/market-off",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/trade-on", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/trade-on",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/trade-off", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/trade-off",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/set-cash", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/set-cash",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/set-auto-exit", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/set-auto-exit",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/stop", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/stop", "POST");
  res.status(status).json(data);
});

app.post("/api/bot/reload", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/reload",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/kill", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/kill", "POST");
  res.status(status).json(data);
});

// --- Optimizer proxy ---

app.get("/api/bot/optimizer-status", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/optimizer-status");
  res.status(status).json(data);
});

app.get("/api/bot/optimizer-events", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/optimizer-events");
  res.status(status).json(data);
});

app.post("/api/bot/optimizer-on", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/optimizer-on",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

app.post("/api/bot/optimizer-off", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/optimizer-off",
    "POST"
  );
  res.status(status).json(data);
});

app.get("/api/bot/optimizer-config", async (_req: Request, res: Response) => {
  // Try control server first, fall back to reading config file directly
  const { status, data } = await proxyToControl("/api/control/optimizer-config");
  if (status === 200 && data?.config) {
    res.json({ ok: true, config: data.config });
    return;
  }
  // Fallback: read config file directly
  try {
    const configPath = path.join(DATA_DIR, "optimizer", "config.json");
    if (fs.existsSync(configPath)) {
      const config = JSON.parse(fs.readFileSync(configPath, "utf-8"));
      res.json({ ok: true, config });
    } else {
      res.json({ ok: true, config: {} });
    }
  } catch {
    res.json({ ok: true, config: {} });
  }
});

app.post("/api/bot/optimizer-config", async (req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/optimizer-config",
    "POST",
    req.body
  );
  res.status(status).json(data);
});

// --- Strategies ---

app.get("/api/strategies", async (_req: Request, res: Response) => {
  // Read strategy names from the canonical Python registry in bots/updown/__init__.py
  try {
    const initFile = path.join(PROJECT_ROOT, "bots", "updown", "__init__.py");
    if (fs.existsSync(initFile)) {
      const content = fs.readFileSync(initFile, "utf-8");
      // Match keys in STRATEGIES = { "Name": ..., }
      const names: string[] = [];
      const re = /^\s*"(\w+)"\s*:/gm;
      let m;
      while ((m = re.exec(content)) !== null) {
        names.push(m[1]);
      }
      res.json({ strategies: names.sort() });
    } else {
      res.json({ strategies: [] });
    }
  } catch {
    res.json({ strategies: [] });
  }
});

// --- Slugs & App start ---

const PROJECT_ROOT = path.resolve(__dirname, "../..");
const SLUGS_JSON = path.join(PROJECT_ROOT, "slugs.json");
const AUTH_CACHE = path.join(PROJECT_ROOT, ".cache", "auth.json");
const DATA_API_BASE = "https://data-api.polymarket.com";
const USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
const POLYGON_RPCS = [
  "https://polygon-bor-rpc.publicnode.com",
  "https://polygon.llamarpc.com",
];

// Cache portfolio value for 30s to avoid hammering APIs
let balanceCache: { value: number; timestamp: number } | null = null;
const BALANCE_CACHE_TTL = 30_000;

async function fetchPortfolioValue(): Promise<number | null> {
  // Check cache
  if (balanceCache && Date.now() - balanceCache.timestamp < BALANCE_CACHE_TTL) {
    return balanceCache.value;
  }

  let safeAddress: string;
  try {
    const auth = JSON.parse(fs.readFileSync(AUTH_CACHE, "utf-8"));
    safeAddress = auth.safe_address;
    if (!safeAddress) return null;
  } catch {
    return null;
  }

  // Fetch positions and USDC balance in parallel
  const [positions, usdcBalance] = await Promise.all([
    fetchAllPositions(safeAddress),
    fetchUsdcBalance(safeAddress),
  ]);

  if (positions === null && usdcBalance === null) return null;

  const positionValue = (positions ?? []).reduce(
    (sum: number, p: any) => sum + (parseFloat(p.currentValue) || 0),
    0
  );
  const total = positionValue + (usdcBalance ?? 0);

  balanceCache = { value: Math.round(total * 100) / 100, timestamp: Date.now() };
  return balanceCache.value;
}

async function fetchAllPositions(safeAddress: string): Promise<any[] | null> {
  try {
    const url = `${DATA_API_BASE}/positions?user=${safeAddress}&sizeThreshold=0&limit=500`;
    const res = await fetch(url);
    if (!res.ok) return null;
    const data = await res.json();
    return Array.isArray(data) ? data : null;
  } catch {
    return null;
  }
}

async function fetchUsdcBalance(safeAddress: string): Promise<number | null> {
  // balanceOf(address) selector = 0x70a08231
  const calldata = "0x70a08231" + safeAddress.slice(2).padStart(64, "0");
  for (const rpc of POLYGON_RPCS) {
    try {
      const res = await fetch(rpc, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jsonrpc: "2.0",
          method: "eth_call",
          params: [{ to: USDC_E, data: calldata }, "latest"],
          id: 1,
        }),
      });
      const json = await res.json() as any;
      if (json.result) {
        // USDC.e has 6 decimals
        return parseInt(json.result, 16) / 1e6;
      }
    } catch {
      continue;
    }
  }
  return null;
}

app.get("/api/balance", async (_req: Request, res: Response) => {
  const value = await fetchPortfolioValue();
  res.json({ portfolio: value });
});

app.get("/api/positions", async (_req: Request, res: Response) => {
  let safeAddress: string;
  try {
    const auth = JSON.parse(fs.readFileSync(AUTH_CACHE, "utf-8"));
    safeAddress = auth.safe_address;
    if (!safeAddress) { res.json({ positions: [], usdc: null }); return; }
  } catch {
    res.json({ positions: [], usdc: null }); return;
  }

  const [positions, usdc] = await Promise.all([
    fetchAllPositions(safeAddress),
    fetchUsdcBalance(safeAddress),
  ]);

  res.json({ positions: positions ?? [], usdc });
});

app.get("/api/slugs", (_req: Request, res: Response) => {
  try {
    if (!fs.existsSync(SLUGS_JSON)) {
      res.json({ slugs: [] });
      return;
    }
    const data = JSON.parse(fs.readFileSync(SLUGS_JSON, "utf-8"));
    res.json({ slugs: data.slug_prefixes || [] });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post("/api/bot/start", async (req: Request, res: Response) => {
  // Check if already running
  if (fs.existsSync(APP_JSON)) {
    try {
      const state = JSON.parse(fs.readFileSync(APP_JSON, "utf-8"));
      if (state.pid) {
        try {
          process.kill(state.pid, 0);
          res.status(409).json({
            error: `App already running (PID ${state.pid})`,
          });
          return;
        } catch {
          // Process dead, stale file — continue
        }
      }
    } catch {}
  }

  const { restore } = req.body;
  const args = ["run", "python", "-m", "app", "start"];
  if (restore) {
    args.push("--restore");
  }

  try {
    const child = spawn("uv", args, {
      cwd: PROJECT_ROOT,
      detached: true,
      stdio: "ignore",
    });
    child.unref();

    // Wait briefly for app.json to appear
    await new Promise((resolve) => setTimeout(resolve, 2000));

    res.json({ ok: true, pid: child.pid });
  } catch (err) {
    res.status(500).json({ error: `Failed to start: ${err}` });
  }
});

// --- Bot trade log ---

app.get("/api/bot/trades", (req: Request, res: Response) => {
  try {
    const limit = Math.min(
      Math.max(parseInt(req.query.limit as string) || 100, 1),
      500
    );
    const allTrades: any[] = [];

    // Scan all markets
    for (const prefix of listMarketPrefixes()) {
      const liveDir = marketLiveDir(prefix);
      const sessionsDir = marketSessionsDir(prefix);
      const workersDir = marketWorkersDir(prefix);
      const tradesFile = marketTradesFile(prefix);

      // 1. Live session files
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

      // 2. Recent trading sidecar files
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

      // 2b. Worker live_trades.json files
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

      // 3. bot_trades.jsonl — resolutions + individual trades
      if (fs.existsSync(tradesFile)) {
        try {
          const lines = fs.readFileSync(tradesFile, "utf-8").trim().split("\n");
          const recentLines = lines.slice(-limit * 2);
          for (const line of recentLines) {
            if (!line) continue;
            try {
              const entry = JSON.parse(line);
              if (entry.type === "resolution") {
                allTrades.push({
                  ...entry,
                  _type: "resolution",
                  session_start: entry.timestamp,
                  market: prefix,
                });
              } else if (entry.strategy) {
                allTrades.push({ ...entry, market: prefix });
              }
            } catch {}
          }
        } catch {}
      }
    }

    // Deduplicate by order_id (trades may appear in both sidecars and bot_trades.jsonl)
    const seen = new Set<string>();
    const deduped: any[] = [];
    for (const t of allTrades) {
      if (t._type === "resolution") {
        deduped.push(t);
        continue;
      }
      const key = t.order_id || `${t.timestamp}-${t.side}-${t.strategy}`;
      if (!seen.has(key)) {
        seen.add(key);
        deduped.push(t);
      }
    }

    // Sort newest-first
    deduped.sort((a, b) => {
      const ta = a.timestamp || a.session_start || "";
      const tb = b.timestamp || b.session_start || "";
      return tb.localeCompare(ta);
    });

    res.json({ trades: deduped.slice(0, limit) });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// --- Strategy stats ---

app.get("/api/strategy/:market/:strategy", (req: Request, res: Response) => {
  try {
    const { market, strategy } = req.params;

    // Read bot_log.jsonl for session-level PnL
    const logFile = path.join(MARKETS_DIR, market, "bot_log.jsonl");
    const sessions: any[] = [];
    if (fs.existsSync(logFile)) {
      const lines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
      for (const line of lines) {
        if (!line) continue;
        try {
          const entry = JSON.parse(line);
          if (entry.strategy === strategy && entry.pnl !== undefined) {
            sessions.push(entry);
          }
        } catch {}
      }
    }

    // Read bot_trades.jsonl for individual trades and resolution records
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

    // Also pull trades from session trading sidecars
    const sessionsDir = marketSessionsDir(market);
    if (fs.existsSync(sessionsDir)) {
      const files = fs.readdirSync(sessionsDir).sort().reverse().slice(0, 100);
      for (const f of files) {
        // Per-strategy sidecar: *.trading.StrategyName.json
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

    // Deduplicate by order_id BEFORE computing active sessions.
    // GTC orders are logged at placement (no fee) and again at fill (with fee);
    // prefer the fill record.
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

    // Build in-progress sessions from trades after the last resolution
    // Group unresolved trades into sessions by 5-minute windows
    const lastResolutionTime = resolutions.length > 0
      ? resolutions[resolutions.length - 1].timestamp
      : null;
    const unresolvedTrades = trades.filter((t) => {
      if (!t.timestamp) return false;
      if (lastResolutionTime && t.timestamp <= lastResolutionTime) return false;
      return true;
    });
    if (unresolvedTrades.length > 0) {
      // Group by ~5 min windows (use first trade as anchor, 6 min gap = new session)
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

      // Read live session for tick/timing info
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
        const successfulTrades = windowTrades.filter((t) => t.success !== false);
        const totalSpent = successfulTrades.reduce((sum, t) => sum + (t.amount || 0), 0);
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

    // Sort trades by timestamp descending
    trades.sort((a, b) => {
      const ta = a.timestamp || a.session_start || "";
      const tb = b.timestamp || b.session_start || "";
      return tb.localeCompare(ta);
    });

    // Compute total fees from trades
    let totalFees = 0;
    for (const t of trades) {
      if (typeof t.fee === "number") totalFees += t.fee;
    }
    for (const r of resolutions) {
      if (typeof r.fee === "number") totalFees += r.fee;
    }

    // Compute stats from resolved sessions only (exclude active/unresolved)
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

    // Get current version from worker state.json
    const stateFile = path.join(MARKETS_DIR, market, "workers", strategy, "state.json");
    let currentVersion: string | null = null;
    if (fs.existsSync(stateFile)) {
      try {
        const state = JSON.parse(fs.readFileSync(stateFile, "utf-8"));
        currentVersion = state.version || null;
      } catch {}
    }

    // Build version history from session logs (group sessions by version)
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
    // Round pnl
    for (const v of Object.values(versionSummary)) {
      v.pnl = Math.round(v.pnl * 100) / 100;
    }

    res.json({
      strategy,
      market,
      stats: {
        total_sessions: resolvedSessions.length,
        traded_sessions: tradedSessions.length,
        total_pnl: Math.round(totalPnl * 100) / 100,
        wins,
        losses,
        win_rate: tradedSessions.length > 0 ? Math.round((wins / tradedSessions.length) * 1000) / 10 : 0,
        sharpe: Math.round(sharpe * 1000) / 1000,
        current_cash: (() => {
          // Read live cash from worker state.json (updated immediately by set-cash)
          const stateFile = path.join(MARKETS_DIR, market, "workers", strategy, "state.json");
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
      sessions: sessions.slice(-200),
      trades: trades.slice(0, 500),
      resolutions: resolutions.slice(-200),
      version_summary: versionSummary,
    });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// --- Market PnL ---

app.get("/api/market/:prefix/pnl", (req: Request, res: Response) => {
  try {
    const { prefix } = req.params;
    const logFile = path.join(MARKETS_DIR, prefix, "bot_log.jsonl");
    if (!fs.existsSync(logFile)) {
      res.json({ overall: 0, last_24h: 0, last_1h: 0, by_strategy: {} });
      return;
    }

    const lines = fs.readFileSync(logFile, "utf-8").trim().split("\n");
    const now = Date.now();
    const cutoff24h = now - 24 * 60 * 60 * 1000;
    const cutoff1h = now - 60 * 60 * 1000;

    let overall = 0;
    let last24h = 0;
    let last1h = 0;
    const byStrategy: Record<string, { overall: number; last_24h: number; last_1h: number }> = {};

    for (const line of lines) {
      if (!line) continue;
      try {
        const entry = JSON.parse(line);
        const pnl = entry.pnl as number;
        if (pnl === undefined || pnl === null) continue;

        const strat = entry.strategy || "unknown";
        if (!byStrategy[strat]) byStrategy[strat] = { overall: 0, last_24h: 0, last_1h: 0 };

        overall += pnl;
        byStrategy[strat].overall += pnl;

        const ts = entry.timestamp ? new Date(entry.timestamp).getTime() : 0;
        if (ts >= cutoff24h) {
          last24h += pnl;
          byStrategy[strat].last_24h += pnl;
        }
        if (ts >= cutoff1h) {
          last1h += pnl;
          byStrategy[strat].last_1h += pnl;
        }
      } catch {}
    }

    // Round values
    const round = (n: number) => Math.round(n * 100) / 100;
    overall = round(overall);
    last24h = round(last24h);
    last1h = round(last1h);
    for (const k of Object.keys(byStrategy)) {
      byStrategy[k].overall = round(byStrategy[k].overall);
      byStrategy[k].last_24h = round(byStrategy[k].last_24h);
      byStrategy[k].last_1h = round(byStrategy[k].last_1h);
    }

    res.json({ overall, last_24h: last24h, last_1h: last1h, by_strategy: byStrategy });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// --- SSE ---

app.get("/events", (_req: Request, res: Response) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });
  res.write('data: {"type":"connected"}\n\n');
  sseClients.add(res);
  _req.on("close", () => sseClients.delete(res));
});

function broadcast(event: string, data: object) {
  const payload = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of sseClients) {
    client.write(payload);
  }
}

// --- Static files (production) ---

if (fs.existsSync(CLIENT_DIR)) {
  app.use(express.static(CLIENT_DIR));
  app.get("*", (req: Request, res: Response) => {
    if (req.path.startsWith("/api/")) return res.status(404).json({ error: "Not found" });
    res.sendFile(path.join(CLIENT_DIR, "index.html"));
  });
}

// --- File Watching ---

function startWatcher() {
  fs.mkdirSync(MARKETS_DIR, { recursive: true });

  const watchPaths = [
    path.join(MARKETS_DIR, "*/sessions/**/*.json"),
    path.join(MARKETS_DIR, "*/live/*.json"),
    path.join(MARKETS_DIR, "*/workers/*/state.json"),
    path.join(DATA_DIR, "optimizer", "*.json"),
    path.join(DATA_DIR, "optimizer", "*.jsonl"),
    APP_JSON,
  ];

  const watcher = watch(watchPaths, {
    ignoreInitial: true,
    awaitWriteFinish: { stabilityThreshold: 1000, pollInterval: 250 },
  });

  watcher.on("add", (filepath: string) => {
    const rel = path.relative(MARKETS_DIR, filepath);
    const parts = rel.split(path.sep);
    if (parts.length >= 3 && parts[1] === "live") {
      broadcast("live-start", { prefix: parts[0] });
    } else if (parts.length >= 3 && parts[1] === "sessions" && !parts[2].includes(".trading.")) {
      broadcast("session-saved", { prefix: parts[0], file: parts[2] });
    }
  });

  watcher.on("change", (filepath: string) => {
    if (filepath === APP_JSON) {
      proxyToControl("/api/control/status")
        .then(({ data }) => broadcast("bot-status", data))
        .catch(() => {});
      return;
    }

    const rel = path.relative(MARKETS_DIR, filepath);
    const parts = rel.split(path.sep);

    if (filepath.includes("/optimizer/")) {
      // Broadcast optimizer status update
      try {
        const statusPath = path.join(DATA_DIR, "optimizer", "status.json");
        if (fs.existsSync(statusPath)) {
          const data = JSON.parse(fs.readFileSync(statusPath, "utf-8"));
          broadcast("optimizer-status", data);
        }
      } catch {}
      return;
    }

    if (parts.length >= 4 && parts[1] === "workers" && parts[3] === "state.json") {
      proxyToControl("/api/control/status")
        .then(({ data }) => broadcast("bot-status", data))
        .catch(() => {});
      return;
    }

    if (parts.length >= 3 && parts[1] === "live") {
      try {
        const raw = fs.readFileSync(filepath, "utf-8");
        const data = JSON.parse(raw);
        broadcast("live-update", { prefix: parts[0], session: data });
      } catch {
        // File might be mid-write
      }
    }
  });

  watcher.on("unlink", (filepath: string) => {
    if (filepath === APP_JSON) {
      broadcast("bot-status", { running: false });
      return;
    }
    const rel = path.relative(MARKETS_DIR, filepath);
    const parts = rel.split(path.sep);
    if (parts.length >= 3 && parts[1] === "live") {
      broadcast("live-end", { prefix: parts[0] });
    }
  });

  return watcher;
}

// --- Start ---

const watcher = startWatcher();

app.listen(PORT, () => {
  console.log(`API server running at http://localhost:${PORT}`);
  console.log(`Markets dir: ${MARKETS_DIR}`);
});

process.on("SIGINT", () => {
  watcher.close();
  process.exit(0);
});
