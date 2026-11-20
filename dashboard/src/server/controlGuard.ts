/** Listen address and market-name checks for the dashboard control API. */

const COMPONENT = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

export function isLoopback(host: string): boolean {
  return host === "127.0.0.1" || host === "::1" || host === "localhost";
}

export function resolveListen(
  host: string | undefined,
  token: string | undefined,
): { host: string; token: string } {
  const chosen = (host ?? "").trim() || "127.0.0.1";
  const secret = (token ?? "").trim();
  if (!isLoopback(chosen) && secret.length === 0) {
    throw new Error(
      "refusing to listen on a non-loopback address without FONDACO_DASHBOARD_TOKEN",
    );
  }
  return { host: chosen, token: secret };
}

export function safeName(raw: string): string | null {
  const value = raw.trim();
  if (!COMPONENT.test(value) || value.includes("..")) return null;
  return value;
}

/** Market key from a slug prefix. A trailing dash run is stripped. */
export function safeSlugKey(prefix: string): string | null {
  return safeName(prefix.trim().replace(/-+$/, ""));
}

export function tokenAccepted(expected: string, presented: string): boolean {
  if (expected.length === 0) return true;
  if (expected.length !== presented.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ presented.charCodeAt(i);
  }
  return diff === 0;
}
