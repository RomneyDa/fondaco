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
const CLIENT_DIR = path.resolve(__dirname, "../dist/client");
const PORT = parseInt(process.env.PORT || "3456", 10);

const app = express();
const sseClients = new Set<Response>();

// --- Helpers ---

function safePath(base: string, ...segments: string[]): string | null {
  const resolved = path.resolve(base, ...segments);
  if (!resolved.startsWith(base)) return null;
  return resolved;
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
      .filter((f) => f.endsWith(".json"))
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

  const watcher = watch(
    [path.join(SESSIONS_DIR, "**/*.json"), path.join(LIVE_DIR, "*.json")],
    {
      ignoreInitial: true,
      awaitWriteFinish: { stabilityThreshold: 300, pollInterval: 100 },
    }
  );

  watcher.on("add", (filepath: string) => {
    if (filepath.startsWith(LIVE_DIR)) {
      const prefix = path.basename(filepath, ".json");
      broadcast("live-start", { prefix });
    } else if (filepath.startsWith(SESSIONS_DIR)) {
      const rel = path.relative(SESSIONS_DIR, filepath);
      const parts = rel.split(path.sep);
      if (parts.length === 2) {
        broadcast("session-saved", { prefix: parts[0], file: parts[1] });
      }
    }
  });

  watcher.on("change", (filepath: string) => {
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
