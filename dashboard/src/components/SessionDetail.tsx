import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { fetchSession } from "../api";
import type { SessionData } from "../types";
import { MetaGrid } from "./MetaGrid";
import { SessionCharts } from "./SessionCharts";

export function SessionDetail() {
  const { prefix, file } = useParams<{ prefix: string; file: string }>();
  const [session, setSession] = useState<SessionData | null>(null);

  useEffect(() => {
    if (prefix && file) {
      fetchSession(prefix, file).then(setSession);
    }
  }, [prefix, file]);

  if (!session) return <p className="subtitle">Loading...</p>;

  return (
    <div>
      <h2>{session.metadata?.event_title || file?.replace(".json", "")}</h2>
      <MetaGrid session={session} />
      <SessionCharts session={session} />
    </div>
  );
}
