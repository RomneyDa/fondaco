"use client";

import Link from "next/link";
import { trpc } from "@/trpc/client";

export function SessionList({ prefix }: { prefix: string }) {
  const sessionsQuery = trpc.sessions.summaries.useQuery({ prefix, limit: 100 });
  const sessions = sessionsQuery.data ?? [];

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h2 className="text-xl font-semibold">{prefix}</h2>
        <Link
          href={`/sessions/${prefix}/all-time`}
          className="text-sm text-accent no-underline hover:underline"
        >
          All-Time View &rarr;
        </Link>
      </div>
      {sessions.length === 0 ? (
        <p className="text-dim text-sm">No sessions collected yet.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {sessions.map((s) => (
            <Link
              key={s.file}
              href={`/sessions/${prefix}/${s.file}`}
              className="flex justify-between items-center px-4 py-2.5 bg-card border border-border rounded-md cursor-pointer transition-colors hover:border-accent no-underline text-inherit"
            >
              <span className="text-sm font-mono">{s.file.replace(".json", "")}</span>
              <span
                className={`text-[13px] font-semibold ${
                  s.outcome
                    ? s.outcome.toLowerCase() === "up" ? "text-up" : s.outcome.toLowerCase() === "down" ? "text-down" : "text-dim"
                    : "text-dim"
                }`}
              >
                {s.outcome || "Unresolved"}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
