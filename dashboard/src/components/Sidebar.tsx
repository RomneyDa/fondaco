import { NavLink } from "react-router-dom";
import type { BotStatus, PrefixInfo } from "../types";

interface Props {
  prefixes: PrefixInfo[];
  livePrefixes: string[];
  botStatus: BotStatus | null;
}

function formatSessionName(file: string): string {
  // file looks like "2026-02-20T03-45-00Z.json" — show a compact label
  const name = file.replace(".json", "");
  const match = name.match(/^(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})/);
  if (match) {
    return `${match[1]} ${match[2]}:${match[3]}`;
  }
  return name;
}

export function Sidebar({ prefixes, livePrefixes, botStatus }: Props) {
  const isRunning = botStatus?.running ?? false;
  const strategies = botStatus?.strategies ?? [];
  const isTrading = strategies.length > 0;
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);

  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <h1>eros-pm</h1>
      </div>

      <div className="nav-section">
        <div className="nav-section-header">Live</div>
        {livePrefixes.length === 0 ? (
          <div className="nav-item dim">No active sessions</div>
        ) : (
          livePrefixes.map((p) => (
            <NavLink
              key={p}
              to={`/live/${p}`}
              className={({ isActive }) =>
                `nav-item live-item${isActive ? " active" : ""}`
              }
            >
              <span className="live-dot" />
              {p}
            </NavLink>
          ))
        )}
      </div>

      <div className="nav-section">
        <div className="nav-section-header">Bot</div>
        <NavLink
          to="/bot"
          className={({ isActive }) =>
            `nav-item live-item${isActive ? " active" : ""}`
          }
        >
          {isTrading ? (
            <>
              <span
                className="live-dot"
                style={allDryRun ? { background: "var(--accent)" } : undefined}
              />
              {strategies.length === 1
                ? strategies[0].name
                : `${strategies.length} strategies`}
            </>
          ) : isRunning ? (
            <>
              <span
                className="live-dot"
                style={{ background: "var(--text-dim)", animation: "none" }}
              />
              Idle
            </>
          ) : (
            <>
              <span
                className="live-dot"
                style={{ background: "var(--red)", animation: "none" }}
              />
              Offline
            </>
          )}
        </NavLink>
        {isTrading && strategies.map((s) => (
          <div key={s.name} className="nav-item nav-session" style={{ paddingLeft: 28 }}>
            <span>{s.name}</span>
            <span className="count">
              ${s.cash.toFixed(0)}
              {s.session_cap != null && <span style={{ opacity: 0.6 }}> ({s.session_cap}/s)</span>}
            </span>
            {s.dry_run && <span className="dry-run-tag" style={{ fontSize: 9, marginLeft: 4 }}>DRY</span>}
          </div>
        ))}
      </div>

      <div className="nav-section">
        <div className="nav-section-header">Recent Sessions</div>
        {prefixes.map((p) => (
          <div key={p.name}>
            <NavLink
              to={`/sessions/${p.name}`}
              className={({ isActive }) =>
                `nav-item nav-prefix${isActive ? " active" : ""}`
              }
            >
              {p.name}
              <span className="count">({p.count})</span>
            </NavLink>
            {p.recentSessions.map((s) => (
              <NavLink
                key={s.file}
                to={`/sessions/${p.name}/${s.file}`}
                className={({ isActive }) =>
                  `nav-item nav-session${isActive ? " active" : ""}`
                }
              >
                <span>{formatSessionName(s.file)}</span>
                {s.outcome && (
                  <span className={`session-outcome ${s.outcome.toLowerCase()}`}>
                    {s.outcome}
                  </span>
                )}
              </NavLink>
            ))}
          </div>
        ))}
      </div>
    </nav>
  );
}
