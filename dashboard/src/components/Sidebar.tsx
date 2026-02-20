import { NavLink } from "react-router-dom";
import type { PrefixInfo } from "../types";

interface Props {
  prefixes: PrefixInfo[];
  livePrefixes: string[];
}

export function Sidebar({ prefixes, livePrefixes }: Props) {
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
        <div className="nav-section-header">Sessions</div>
        {prefixes.map((p) => (
          <NavLink
            key={p.name}
            to={`/sessions/${p.name}`}
            className={({ isActive }) =>
              `nav-item${isActive ? " active" : ""}`
            }
          >
            {p.name}
            <span className="count">({p.count})</span>
          </NavLink>
        ))}
      </div>
    </nav>
  );
}
