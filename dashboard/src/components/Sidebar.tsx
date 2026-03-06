import { NavLink } from "react-router-dom";
import type { BotStatus, MarketStatus, PrefixInfo } from "../types";
import { fmtSessionName } from "../utils/formatTime";

interface Props {
  prefixes: PrefixInfo[];
  livePrefixes: string[];
  botStatus: BotStatus | null;
}

export function Sidebar({ prefixes, livePrefixes, botStatus }: Props) {
  const isRunning = botStatus?.running ?? false;
  const markets = botStatus?.markets ?? [];

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
        <div className="nav-section-header">Trading</div>
        <NavLink
          to="/bot"
          className={({ isActive }) =>
            `nav-item live-item${isActive ? " active" : ""}`
          }
        >
          {isRunning ? (
            <>
              <span
                className="live-dot"
                style={
                  markets.length === 0
                    ? { background: "var(--text-dim)", animation: "none" }
                    : undefined
                }
              />
              {markets.length === 0
                ? "Idle"
                : `${markets.length} market${markets.length !== 1 ? "s" : ""}`}
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

        {markets.map((m) => (
          <MarketSidebarItem key={m.slug_key} market={m} />
        ))}
      </div>

      <div className="nav-section">
        <div className="nav-section-header">Sessions</div>
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
                <span>{fmtSessionName(s.file)}</span>
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

function MarketSidebarItem({ market }: { market: MarketStatus }) {
  const strategies = market.strategies ?? [];
  const isTrading = strategies.length > 0;
  const anyDead = strategies.some((s) => s.alive === false);
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);

  return (
    <>
      <div className="nav-item" style={{ paddingLeft: 20 }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <span
            style={{
              display: "inline-block",
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: anyDead
                ? "var(--red)"
                : isTrading
                  ? allDryRun ? "var(--accent)" : "var(--green)"
                  : "var(--text-dim)",
              animation: isTrading && !anyDead ? "pulse 2s infinite" : "none",
              flexShrink: 0,
            }}
          />
          {market.slug_key}
        </span>
        {market.stop_after_session && (
          <span className="dry-run-tag" style={{ background: "var(--red)", marginLeft: 4, fontSize: 9 }}>STOPPING</span>
        )}
      </div>
      {strategies.map((s) => (
        <div key={s.name} className="nav-item nav-session" style={{ paddingLeft: 36 }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
            <span
              style={{
                display: "inline-block",
                width: 6,
                height: 6,
                borderRadius: "50%",
                background: s.alive === false ? "var(--red)" : "var(--green)",
                flexShrink: 0,
              }}
            />
            {s.name}
          </span>
          <span className="count">
            ${(s.bankroll ?? s.cash).toFixed(0)}
            {s.session_cap != null && <span style={{ opacity: 0.6 }}> (${s.session_cap}/s)</span>}
          </span>
          {s.dry_run && <span className="dry-run-tag" style={{ fontSize: 9, marginLeft: 4 }}>DRY</span>}
        </div>
      ))}
    </>
  );
}
