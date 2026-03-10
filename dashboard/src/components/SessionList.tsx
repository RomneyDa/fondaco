import { useEffect, useState } from "react";
import { Link, useParams, useLocation } from "react-router-dom";
import { fetchSessionSummaries } from "../api";
import type { SessionSummary } from "../types";

export function SessionList() {
  const { prefix } = useParams<{ prefix: string }>();
  const location = useLocation();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);

  useEffect(() => {
    if (!prefix) return;
    fetchSessionSummaries(prefix, 100).then(setSessions);
  }, [prefix, location.key]);

  return (
    <div>
      <h2 className="text-xl font-semibold mb-6">{prefix}</h2>
      {sessions.length === 0 ? (
        <p className="text-dim text-sm">No sessions collected yet.</p>
      ) : (
        <div className="flex flex-col gap-1.5">
          {sessions.map((s) => (
            <Link
              key={s.file}
              to={`/sessions/${prefix}/${s.file}`}
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
