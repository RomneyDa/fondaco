import express, { Request, Response } from "express";
import { watch } from "chokidar";
import { spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DATA_DIR = path.resolve(__dirname, "../../data");
const SESSIONS_DIR = path.join(DATA_DIR, "sessions");
const LIVE_DIR = path.join(DATA_DIR, "live");
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
    if (!fs.existsSync(SESSIONS_DIR)) {
      res.json({ prefixes: [] });
      return;
    }
    const entries = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true });
    const prefixes = entries
      .filter((e) => e.isDirectory())
      .map((e) => e.name)
      .sort();
    res.json({ prefixes });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/sessions/:prefix/summaries", (req: Request, res: Response) => {
  try {
    const dir = safePath(SESSIONS_DIR, req.params.prefix);
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
    const dir = safePath(SESSIONS_DIR, req.params.prefix);
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
      SESSIONS_DIR,
      req.params.prefix,
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
      const sidecarFile = req.params.file + ".trading.json";
      const filepath = safePath(SESSIONS_DIR, req.params.prefix, sidecarFile);
      if (!filepath) {
        res.status(403).json({ error: "Forbidden" });
        return;
      }
      if (!fs.existsSync(filepath)) {
        res.status(404).json({ error: "No trading data" });
        return;
      }
      const data = JSON.parse(fs.readFileSync(filepath, "utf-8"));
      res.json(data);
    } catch (err) {
      res.status(500).json({ error: String(err) });
    }
  }
);

app.get("/api/live", (_req: Request, res: Response) => {
  try {
    if (!fs.existsSync(LIVE_DIR)) {
      res.json({ live: [] });
      return;
    }
    const files = fs.readdirSync(LIVE_DIR).filter((f) => f.endsWith(".json"));
    const live = files.map((f) => f.replace(".json", ""));
    res.json({ live });
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.get("/api/live/:prefix", (req: Request, res: Response) => {
  try {
    const filepath = safePath(LIVE_DIR, `${req.params.prefix}.json`);
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

app.post("/api/bot/stop", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl("/api/control/stop", "POST");
  res.status(status).json(data);
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

  const { slug_prefix, window: windowDuration } = req.body;
  if (!slug_prefix) {
    res.status(400).json({ error: "Missing slug_prefix" });
    return;
  }

  const args = ["run", "python", "-m", "app", "start", slug_prefix];
  if (windowDuration) {
    args.push("--window", String(windowDuration));
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
    // Collect trades from live session files and recent trading sidecars.
    // Each trade gets a session_start timestamp for global sorting.
    const allTrades: any[] = [];

    // 1. Live session files (current/in-progress sessions)
    if (fs.existsSync(LIVE_DIR)) {
      for (const file of fs.readdirSync(LIVE_DIR).filter((f) => f.endsWith(".json"))) {
        try {
          const data = JSON.parse(fs.readFileSync(path.join(LIVE_DIR, file), "utf-8"));
          const sessionStart = data.metadata?.window_start_utc || data.collection_started_utc || "";
          const trading = data.trading;
          if (trading?.strategies) {
            for (const strat of trading.strategies) {
              if (strat.trades?.length) {
                for (const t of strat.trades) {
                  allTrades.push({ ...t, session_start: sessionStart, strategy: strat.name });
                }
              }
            }
          } else if (trading?.trades?.length) {
            for (const t of trading.trades) {
              allTrades.push({ ...t, session_start: sessionStart });
            }
          }
        } catch {}
      }
    }

    // 2. Recent trading sidecar files (historical sessions)
    if (fs.existsSync(SESSIONS_DIR)) {
      const prefixes = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true })
        .filter((e) => e.isDirectory())
        .map((e) => e.name);
      for (const prefix of prefixes) {
        const dir = path.join(SESSIONS_DIR, prefix);
        const sidecars = fs.readdirSync(dir)
          .filter((f) => f.endsWith(".trading.json"))
          .sort()
          .reverse()
          .slice(0, 20); // last 20 sessions
        for (const sidecar of sidecars) {
          try {
            const data = JSON.parse(fs.readFileSync(path.join(dir, sidecar), "utf-8"));
            // Session timestamp from filename (e.g. "1234567890.json.trading.json")
            const sessionStart = sidecar.replace(".json.trading.json", "");
            if (data.strategies) {
              for (const strat of data.strategies) {
                if (strat.trades?.length) {
                  for (const t of strat.trades) {
                    allTrades.push({ ...t, session_start: sessionStart, strategy: strat.strategy || strat.name });
                  }
                }
              }
            } else if (data.trades?.length) {
              for (const t of data.trades) {
                allTrades.push({ ...t, session_start: sessionStart });
              }
            }
          } catch {}
        }
      }
    }

    // Sort newest-first: by timestamp if available, else by session_start
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
  fs.mkdirSync(SESSIONS_DIR, { recursive: true });
  fs.mkdirSync(LIVE_DIR, { recursive: true });

  const watchPaths = [
    path.join(SESSIONS_DIR, "**/*.json"),
    path.join(LIVE_DIR, "*.json"),
  ];

  // Also watch app.json for bot status changes
  if (fs.existsSync(path.dirname(APP_JSON))) {
    watchPaths.push(APP_JSON);
  }

  const watcher = watch(watchPaths, {
    ignoreInitial: true,
    awaitWriteFinish: { stabilityThreshold: 1000, pollInterval: 250 },
  });

  watcher.on("add", (filepath: string) => {
    if (filepath.startsWith(LIVE_DIR)) {
      const prefix = path.basename(filepath, ".json");
      broadcast("live-start", { prefix });
    } else if (filepath.startsWith(SESSIONS_DIR)) {
      const rel = path.relative(SESSIONS_DIR, filepath);
      const parts = rel.split(path.sep);
      if (parts.length === 2 && !parts[1].includes(".trading.")) {
        broadcast("session-saved", { prefix: parts[0], file: parts[1] });
      }
    }
  });

  watcher.on("change", (filepath: string) => {
    if (filepath === APP_JSON) {
      // Broadcast bot status change
      proxyToControl("/api/control/status")
        .then(({ data }) => broadcast("bot-status", data))
        .catch(() => {});
      return;
    }
    if (filepath.startsWith(LIVE_DIR)) {
      try {
        const raw = fs.readFileSync(filepath, "utf-8");
        const data = JSON.parse(raw);
        const prefix = path.basename(filepath, ".json");
        broadcast("live-update", { prefix, session: data });
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
    if (filepath.startsWith(LIVE_DIR)) {
      const prefix = path.basename(filepath, ".json");
      broadcast("live-end", { prefix });
    }
  });

  return watcher;
}

// --- Start ---

const watcher = startWatcher();

app.listen(PORT, () => {
  console.log(`API server running at http://localhost:${PORT}`);
  console.log(`Watching: ${SESSIONS_DIR}`);
  console.log(`Live dir: ${LIVE_DIR}`);
});

process.on("SIGINT", () => {
  watcher.close();
  process.exit(0);
});
