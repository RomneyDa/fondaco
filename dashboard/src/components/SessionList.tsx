import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchSessionSummaries } from "../api";
import type { SessionSummary } from "../types";

export function SessionList() {
  const { prefix } = useParams<{ prefix: string }>();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);

  useEffect(() => {
    if (!prefix) return;
    fetchSessionSummaries(prefix, 100).then(setSessions);
  }, [prefix]);

  return (
    <div>
      <h2>{prefix}</h2>
      {sessions.length === 0 ? (
        <p className="subtitle">No sessions collected yet.</p>
      ) : (
        <div className="session-list">
          {sessions.map((s) => (
            <Link
              key={s.file}
              to={`/sessions/${prefix}/${s.file}`}
              className="session-list-item"
            >
              <span className="name">{s.file.replace(".json", "")}</span>
              <span
                className={`outcome ${
                  s.outcome ? s.outcome.toLowerCase() : "unresolved"
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
