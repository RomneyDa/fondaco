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
  const isTrading = botStatus?.trading_enabled ?? false;
  const isDryRun = botStatus?.dry_run ?? false;

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
                style={isDryRun ? { background: "var(--accent)" } : undefined}
              />
              {botStatus?.strategy || "Trading"}
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
            {p.recentSessions.map((file) => (
              <NavLink
                key={file}
                to={`/sessions/${p.name}/${file}`}
                className={({ isActive }) =>
                  `nav-item nav-session${isActive ? " active" : ""}`
                }
              >
                {formatSessionName(file)}
              </NavLink>
            ))}
          </div>
        ))}
      </div>
    </nav>
  );
}
