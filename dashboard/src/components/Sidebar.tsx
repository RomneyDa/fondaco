import { useState } from "react";
import { NavLink } from "react-router-dom";
import type { PingEntry } from "../App";
import type { BotStatus, MarketStatus, PrefixInfo } from "../types";
import { fmtSessionName } from "../utils/formatTime";

interface Props {
  prefixes: PrefixInfo[];
  livePrefixes: string[];
  botStatus: BotStatus | null;
  pings: PingEntry[];
}

export function Sidebar({ prefixes, livePrefixes, botStatus, pings }: Props) {
  const isRunning = botStatus?.running ?? false;
  const markets = botStatus?.markets ?? [];

  // Live prefixes not covered by a market (e.g. data-only streams)
  const marketSlugs = new Set(markets.map((m) => m.slug_key));
  const extraLive = livePrefixes.filter((p) => !marketSlugs.has(p));

  return (
    <nav className="sidebar">
      <div className="sidebar-header">
        <h1>eros-pm</h1>
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
          <MarketSidebarItem
            key={m.slug_key}
            market={m}
            hasLiveData={livePrefixes.includes(m.slug_key)}
          />
        ))}

        {extraLive.map((p) => (
          <NavLink
            key={p}
            to={`/live/${p}`}
            className={({ isActive }) =>
              `nav-item live-item${isActive ? " active" : ""}`
            }
            style={{ paddingLeft: 20 }}
          >
            <span className="live-dot" />
            {p}
          </NavLink>
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

      <PingIndicator pings={pings} />
    </nav>
  );
}

function PingIndicator({ pings }: { pings: PingEntry[] }) {
  const [expanded, setExpanded] = useState(false);
  const last = pings[0];

  const dotColor = !last
    ? "var(--text-dim)"
    : last.status === "ok"
      ? "var(--green)"
      : last.status === "offline"
        ? "var(--btc)"
        : "var(--red)";

  return (
    <div className="ping-indicator">
      <button
        className="ping-summary"
        onClick={() => setExpanded((v) => !v)}
        title="Status pings"
      >
        <span
          style={{
            display: "inline-block",
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: dotColor,
            flexShrink: 0,
          }}
        />
        <span className="ping-label">
          {last
            ? `${last.latencyMs}ms · ${last.detail ?? last.status}`
            : "no pings"}
        </span>
        <span style={{ opacity: 0.4, fontSize: 10 }}>{expanded ? "▼" : "▶"}</span>
      </button>
      {expanded && (
        <div className="ping-log">
          {pings.map((p, i) => (
            <div key={i} className={`ping-entry ping-${p.status}`}>
              <span className="ping-time">
                {p.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
              <span
                style={{
                  display: "inline-block",
                  width: 5,
                  height: 5,
                  borderRadius: "50%",
                  background:
                    p.status === "ok"
                      ? "var(--green)"
                      : p.status === "offline"
                        ? "var(--btc)"
                        : "var(--red)",
                  flexShrink: 0,
                }}
              />
              <span className="ping-detail">{p.latencyMs}ms</span>
              <span className="ping-detail" style={{ opacity: 0.7 }}>{p.detail}</span>
            </div>
          ))}
          {pings.length === 0 && (
            <div className="ping-entry" style={{ opacity: 0.5 }}>No pings yet</div>
          )}
        </div>
      )}
    </div>
  );
}

function MarketSidebarItem({ market, hasLiveData }: { market: MarketStatus; hasLiveData: boolean }) {
  const strategies = market.strategies ?? [];
  const isTrading = strategies.length > 0;
  const anyDead = strategies.some((s) => s.alive === false);
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);

  const dotColor = anyDead
    ? "var(--red)"
    : isTrading
      ? allDryRun ? "var(--accent)" : "var(--green)"
      : hasLiveData
        ? "var(--green)"
        : "var(--text-dim)";

  return (
    <>
      <NavLink
        to={`/live/${market.slug_key}`}
        className={({ isActive }) =>
          `nav-item${isActive ? " active" : ""}`
        }
        style={{ paddingLeft: 20 }}
      >
        <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
          <span
            style={{
              display: "inline-block",
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: dotColor,
              animation: (isTrading && !anyDead) || hasLiveData ? "pulse 2s infinite" : "none",
              flexShrink: 0,
            }}
          />
          {market.slug_key}
        </span>
        {market.stop_after_session && (
          <span className="dry-run-tag" style={{ background: "var(--red)", marginLeft: 4, fontSize: 9 }}>STOPPING</span>
        )}
      </NavLink>
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
