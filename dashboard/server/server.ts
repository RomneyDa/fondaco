import express, { Request, Response } from "express";
import { watch } from "chokidar";
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

app.post("/api/bot/trade-off", async (_req: Request, res: Response) => {
  const { status, data } = await proxyToControl(
    "/api/control/trade-off",
    "POST"
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
  app.get("*", (_req: Request, res: Response) => {
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
