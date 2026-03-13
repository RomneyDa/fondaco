import { useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import type { PingEntry } from "../App";
import type { BalanceInfo } from "../api";
import type { BotStatus, MarketStatus, PrefixInfo } from "../types";
import { fmtSessionName } from "../utils/formatTime";

interface Props {
  prefixes: PrefixInfo[];
  livePrefixes: string[];
  botStatus: BotStatus | null;
  pings: PingEntry[];
  balance: BalanceInfo;
}

export function Sidebar({ prefixes, livePrefixes, botStatus, pings, balance }: Props) {
  const isRunning = botStatus?.running ?? false;
  const markets = botStatus?.markets ?? [];
  const marketsBySlug = new Map(markets.map((m) => [m.slug_key, m]));

  // Collect all known market names (from sessions + live markets + extra live)
  const allMarketNames = new Set([
    ...prefixes.map((p) => p.name),
    ...markets.map((m) => m.slug_key),
    ...livePrefixes,
  ]);

  return (
    <nav className="w-full h-full bg-sidebar border-r border-border flex flex-col overflow-y-auto scrollbar-none">
      <div className="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h1 className="text-lg font-semibold text-accent">eros-pm</h1>
        {balance.portfolio != null && (
          <NavLink to="/positions" className="text-sm font-mono text-dim hover:text-accent transition-colors">
            ${balance.portfolio.toFixed(2)}
          </NavLink>
        )}
      </div>

      <div className="py-3 border-b border-border">
        <NavLink
          to="/home"
          className={({ isActive }) =>
            `flex items-center gap-2 px-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
          }
        >
          {isRunning ? (
            markets.length > 0 ? (
              <>
                <span className="w-2 h-2 rounded-full shrink-0 bg-up animate-pulse-dot" />
                Home
              </>
            ) : livePrefixes.length > 0 ? (
              <>
                <span className="w-2 h-2 rounded-full shrink-0 bg-btc animate-pulse-dot" />
                Home
              </>
            ) : (
              <>
                <span className="w-2 h-2 rounded-full shrink-0 bg-dim" />
                Home
              </>
            )
          ) : livePrefixes.length > 0 ? (
            <>
              <span className="w-2 h-2 rounded-full shrink-0 bg-btc animate-pulse-dot" />
              Home
            </>
          ) : (
            <>
              <span className="w-2 h-2 rounded-full shrink-0 bg-down" />
              Home
            </>
          )}
        </NavLink>

        <NavLink
          to="/optimizer"
          className={({ isActive }) =>
            `flex items-center gap-2 px-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
          }
        >
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${
              botStatus?.optimizer?.running ? "bg-up" : "bg-dim"
            } ${botStatus?.optimizer?.state === "optimizing" ? "animate-pulse-dot" : ""}`}
          />
          Optimizer
        </NavLink>
      </div>

      <div className="py-3 border-b border-border flex-1 overflow-y-auto scrollbar-none">
        {[...allMarketNames].map((name) => {
          const prefix = prefixes.find((p) => p.name === name);
          const market = marketsBySlug.get(name);
          const hasLive = livePrefixes.includes(name);
          return (
            <MarketSection
              key={name}
              name={name}
              prefix={prefix}
              market={market}
              hasLiveData={hasLive}
            />
          );
        })}
      </div>

      <PingIndicator pings={pings} />
    </nav>
  );
}

function PingIndicator({ pings }: { pings: PingEntry[] }) {
  const [expanded, setExpanded] = useState(false);
  const last = pings[0];

  const dotColor = !last
    ? "bg-dim"
    : last.status === "ok"
      ? "bg-up"
      : last.status === "offline"
        ? "bg-btc"
        : "bg-down";

  return (
    <div className="mt-auto border-t border-border">
      <button
        className="flex items-center gap-1.5 w-full px-3 py-2 bg-transparent border-none text-dim text-[11px] font-inherit cursor-pointer text-left hover:bg-accent/5"
        onClick={() => setExpanded((v) => !v)}
        title="Status pings"
      >
        <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${dotColor}`} />
        <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
          {last
            ? `${last.latencyMs}ms · ${last.detail ?? last.status}`
            : "no pings"}
        </span>
        <span className="opacity-40 text-[10px]">{expanded ? "▼" : "▶"}</span>
      </button>
      {expanded && (
        <div className="max-h-[200px] overflow-y-auto border-t border-border">
          {pings.map((p, i) => (
            <div key={i} className={`flex items-center gap-1.5 px-3 py-0.5 text-[10px] font-mono text-dim ${p.status === "error" ? "!text-down" : p.status === "offline" ? "!text-btc" : ""}`}>
              <span className="opacity-50 min-w-[56px]">
                {p.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </span>
              <span className={`inline-block w-[5px] h-[5px] rounded-full shrink-0 ${
                p.status === "ok" ? "bg-up" : p.status === "offline" ? "bg-btc" : "bg-down"
              }`} />
              <span className="whitespace-nowrap">{p.latencyMs}ms</span>
              <span className="whitespace-nowrap opacity-70">{p.detail}</span>
            </div>
          ))}
          {pings.length === 0 && (
            <div className="flex items-center gap-1.5 px-3 py-0.5 text-[10px] font-mono text-dim opacity-50">No pings yet</div>
          )}
        </div>
      )}
    </div>
  );
}

function MarketSection({
  name,
  prefix,
  market,
  hasLiveData,
}: {
  name: string;
  prefix: PrefixInfo | undefined;
  market: MarketStatus | undefined;
  hasLiveData: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const location = useLocation();
  const strategies = market?.strategies ?? [];
  const isTrading = strategies.length > 0;
  const anyDead = strategies.some((s) => s.alive === false);
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);
  const sessionCount = prefix?.count ?? 0;

  const dotColor = anyDead
    ? "bg-down"
    : isTrading
      ? allDryRun ? "bg-accent" : "bg-up"
      : hasLiveData
        ? "bg-btc"
        : "bg-dim";

  const shouldPulse = (isTrading && !anyDead) || hasLiveData;

  return (
    <div>
      <div className="flex items-center w-full px-5 py-1.5 text-[13px] font-semibold hover:bg-accent/10 transition-colors">
        <span
          className={`inline-block w-[7px] h-[7px] rounded-full shrink-0 ${dotColor} ${shouldPulse ? "animate-pulse-dot" : ""}`}
          title={hasLiveData ? "Live" : "Inactive"}
        />
        <NavLink
          to={hasLiveData ? `/live/${name}` : `/sessions/${name}`}
          className={({ isActive }) =>
            `flex-1 truncate ml-1.5 cursor-pointer no-underline transition-colors ${isActive ? "text-accent" : "text-txt hover:text-accent"}`
          }
        >
          {name}
        </NavLink>
        {market?.stop_after_session && (
          <span className="px-1.5 py-0.5 rounded text-[9px] font-semibold bg-down text-white mr-1">STOP</span>
        )}
        <button
          className="bg-transparent border-none cursor-pointer text-dim hover:text-txt transition-colors p-0 font-inherit text-[9px] shrink-0"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "collapse" : "expand"}
        </button>
      </div>

      {expanded && (
        <div>
          {/* Active strategies */}
          {strategies.map((s) => (
            <NavLink
              key={s.name}
              to={`/strategy/${name}/${s.name}`}
              className="flex justify-between items-center py-[3px] pl-8 pr-5 text-xs font-mono text-dim no-underline hover:text-txt"
            >
              <span className="inline-flex items-center gap-1">
                <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${s.alive === false ? "bg-down" : "bg-up"}`} />
                {s.name}
              </span>
              <span className="text-dim text-xs">
                ${(s.bankroll ?? s.cash).toFixed(0)}
              </span>
            </NavLink>
          ))}

          {/* "All sessions" link only when live (title already goes to /sessions when not live) */}
          {prefix && hasLiveData && (() => {
            const onSessionsPage = location.pathname === `/sessions/${name}`;
            const onAnySession = location.pathname.startsWith(`/sessions/${name}`);
            const highlight = onAnySession && !onSessionsPage;
            return (
              <NavLink
                to={`/sessions/${name}`}
                className={({ isActive }) =>
                  `block pl-8 pr-5 py-[3px] text-xs cursor-pointer transition-colors ${isActive || highlight ? "text-accent" : "text-dim hover:text-txt hover:underline"}`
                }
              >
                All {sessionCount} sessions <span className="text-base leading-none">→</span>
              </NavLink>
            );
          })()}

          {/* Recent sessions */}
          {prefix?.recentSessions.map((s) => (
            <NavLink
              key={s.file}
              to={`/sessions/${name}/${s.file}`}
              className={({ isActive }) =>
                `flex justify-between items-center py-[3px] pl-8 pr-5 text-xs font-mono cursor-pointer transition-colors ${isActive ? "text-accent" : "text-dim hover:text-txt"}`
              }
            >
              <span>{fmtSessionName(s.file)}</span>
              {s.outcome && (
                <span className={`text-[11px] shrink-0 ml-1.5 ${s.outcome.toLowerCase() === "up" ? "text-up" : "text-down"}`}>
                  {s.outcome.toLowerCase() === "up" ? "▲" : "▼"}
                </span>
              )}
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}
