import type { PrefixInfo, SessionData } from "./types";

export async function fetchPrefixes(): Promise<PrefixInfo[]> {
  const res = await fetch("/api/prefixes");
  const { prefixes } = (await res.json()) as { prefixes: string[] };

  const counts = await Promise.all(
    prefixes.map((p) =>
      fetch(`/api/sessions/${p}`)
        .then((r) => r.json())
        .then((d: { sessions: string[] }) => d.sessions.length)
    )
  );

  return prefixes.map((name, i) => ({ name, count: counts[i] }));
}

export async function fetchSessionList(
  prefix: string
): Promise<string[]> {
  const res = await fetch(`/api/sessions/${prefix}`);
  const { sessions } = (await res.json()) as { sessions: string[] };
  return sessions;
}

export async function fetchSession(
  prefix: string,
  file: string
): Promise<SessionData> {
  const res = await fetch(`/api/session/${prefix}/${file}`);
  return res.json();
}

export async function fetchLivePrefixes(): Promise<string[]> {
  const res = await fetch("/api/live");
  const { live } = (await res.json()) as { live: string[] };
  return live;
}

export async function fetchLiveSession(
  prefix: string
): Promise<SessionData | null> {
  const res = await fetch(`/api/live/${prefix}`);
  if (!res.ok) return null;
  return res.json();
}
