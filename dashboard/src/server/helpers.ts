import * as fs from "fs";
import * as path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const PROJECT_ROOT = path.resolve(__dirname, "../../..");
export const DATA_DIR = path.resolve(PROJECT_ROOT, "data");
export const MARKETS_DIR = path.join(DATA_DIR, "markets");
export const APP_JSON = path.join(DATA_DIR, "app.json");
export const AUTH_CACHE = path.join(PROJECT_ROOT, ".cache", "auth.json");
export const DATA_API_BASE = "https://data-api.polymarket.com";
export const USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174";
export const POLYGON_RPCS = [
  "https://polygon-bor-rpc.publicnode.com",
  "https://polygon.llamarpc.com",
];

export function safePath(base: string, ...segments: string[]): string | null {
  const resolved = path.resolve(base, ...segments);
  if (!resolved.startsWith(base)) return null;
  return resolved;
}

export function getControlPort(): number | null {
  try {
    const data = JSON.parse(fs.readFileSync(APP_JSON, "utf-8"));
    if (!data.port) return null;
    // Verify the PID is actually alive
    if (data.pid) {
      try {
        process.kill(data.pid, 0);
      } catch {
        // Process is dead — clean up stale app.json
        try { fs.unlinkSync(APP_JSON); } catch {}
        return null;
      }
    }
    return data.port;
  } catch {
    return null;
  }
}

export function marketSessionsDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "sessions");
}

export function marketLiveDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "live");
}

export function marketWorkersDir(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "workers");
}

export function marketTradesFile(prefix: string): string {
  return path.join(MARKETS_DIR, prefix, "bot_trades.jsonl");
}

export function listMarketPrefixes(): string[] {
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

export async function proxyToControl(
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

// Cache portfolio value for 30s
let balanceCache: { value: number; timestamp: number } | null = null;
const BALANCE_CACHE_TTL = 30_000;

export async function fetchPortfolioValue(): Promise<number | null> {
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

export async function fetchAllPositions(safeAddress: string): Promise<any[] | null> {
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

export async function fetchUsdcBalance(safeAddress: string): Promise<number | null> {
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
        return parseInt(json.result, 16) / 1e6;
      }
    } catch {
      continue;
    }
  }
  return null;
}

export function getSafeAddress(): string | null {
  try {
    const auth = JSON.parse(fs.readFileSync(AUTH_CACHE, "utf-8"));
    return auth.safe_address || null;
  } catch {
    return null;
  }
}
