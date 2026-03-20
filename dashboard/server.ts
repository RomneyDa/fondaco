import express, { Request, Response } from "express";
import { watch } from "chokidar";
import next from "next";
import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const dev = process.env.NODE_ENV !== "production";
const PORT = parseInt(process.env.PORT || "3456", 10);

const DATA_DIR = path.resolve(__dirname, "../data");
const MARKETS_DIR = path.join(DATA_DIR, "markets");
const APP_JSON = path.join(DATA_DIR, "app.json");

const app = express();
// Only parse JSON for express-handled routes (SSE etc), NOT /api/trpc
// which Next.js handles directly — express.json() consumes the body stream
// before Next.js can read it, breaking tRPC mutations.
app.use((req, res, nextFn) => {
  if (req.path.startsWith("/api/trpc")) return nextFn();
  express.json()(req, res, nextFn);
});

const sseClients = new Set<Response>();

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

  let appJsonUnlinkTimer: ReturnType<typeof setTimeout> | null = null;

  watcher.on("add", (filepath: string) => {
    if (filepath === APP_JSON) {
      if (appJsonUnlinkTimer) {
        clearTimeout(appJsonUnlinkTimer);
        appJsonUnlinkTimer = null;
      }
      broadcast("bot-status", { source: "file-watch" });
      return;
    }
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
      if (appJsonUnlinkTimer) {
        clearTimeout(appJsonUnlinkTimer);
        appJsonUnlinkTimer = null;
      }
      broadcast("bot-status", { source: "file-watch" });
      return;
    }

    const rel = path.relative(MARKETS_DIR, filepath);
    const parts = rel.split(path.sep);

    if (filepath.includes("/optimizer/")) {
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
      broadcast("bot-status", { source: "file-watch" });
      return;
    }

    if (parts.length >= 3 && parts[1] === "live") {
      try {
        const raw = fs.readFileSync(filepath, "utf-8");
        const data = JSON.parse(raw);
        broadcast("live-update", { prefix: parts[0], session: data });
      } catch {}
    }
  });

  watcher.on("unlink", (filepath: string) => {
    if (filepath === APP_JSON) {
      if (appJsonUnlinkTimer) clearTimeout(appJsonUnlinkTimer);
      appJsonUnlinkTimer = setTimeout(() => {
        if (!fs.existsSync(APP_JSON)) {
          broadcast("bot-status", { running: false });
        }
        appJsonUnlinkTimer = null;
      }, 500);
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

async function main() {
  const nextApp = next({ dev, dir: __dirname });
  const handle = nextApp.getRequestHandler();

  await nextApp.prepare();

  const watcher = startWatcher();

  // Let Next.js handle everything except /events (SSE)
  app.all("*", (req: Request, res: Response) => {
    return handle(req, res);
  });

  app.listen(PORT, () => {
    console.log(`Dashboard running at http://localhost:${PORT} (${dev ? "dev" : "production"})`);
    console.log(`Markets dir: ${MARKETS_DIR}`);
  });

  process.on("SIGINT", () => {
    watcher.close();
    process.exit(0);
  });
}

main().catch((err) => {
  console.error("Failed to start server:", err);
  process.exit(1);
});
