import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchSession, fetchSessionList } from "../api";

interface SessionSummary {
  file: string;
  outcome?: string | null;
}

export function SessionList() {
  const { prefix } = useParams<{ prefix: string }>();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);

  useEffect(() => {
    if (!prefix) return;

    fetchSessionList(prefix).then((files) => {
      setSessions(files.map((f) => ({ file: f })));

      // Load outcomes in batches
      const BATCH = 20;
      (async () => {
        for (let i = 0; i < files.length; i += BATCH) {
          const batch = files.slice(i, i + BATCH);
          const results = await Promise.all(
            batch.map((f) =>
              fetchSession(prefix, f)
                .then((d) => ({ file: f, outcome: d.outcome }))
                .catch(() => ({ file: f, outcome: null }))
            )
          );
          setSessions((prev) =>
            prev.map((s) => {
              const r = results.find((x) => x.file === s.file);
              return r ? { ...s, outcome: r.outcome } : s;
            })
          );
        }
      })();
    });
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
                {s.outcome === undefined ? "" : s.outcome || "Unresolved"}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
