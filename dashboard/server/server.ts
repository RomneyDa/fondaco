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

      // 3. bot_trades.jsonl
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
              }
            } catch {}
          }
        } catch {}
      }
    }

    // Sort newest-first
    allTrades.sort((a, b) => {
      const ta = a.timestamp || a.session_start || "";
      const tb = b.timestamp || b.session_start || "";
      return tb.localeCompare(ta);
    });

    res.json({ trades: allTrades.slice(0, limit) });
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
