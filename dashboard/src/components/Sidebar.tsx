import { useState } from "react";
import { NavLink } from "react-router-dom";
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

  const marketSlugs = new Set(markets.map((m) => m.slug_key));
  const extraLive = livePrefixes.filter((p) => !marketSlugs.has(p));

  return (
    <nav className="w-full h-full bg-sidebar border-r border-border flex flex-col overflow-y-auto">
      <div className="px-5 py-4 border-b border-border flex items-baseline justify-between">
        <h1 className="text-lg font-semibold text-accent">eros-pm</h1>
        {balance.portfolio != null && (
          <NavLink to="/positions" className="text-sm font-mono text-dim hover:text-accent transition-colors">
            ${balance.portfolio.toFixed(2)}
          </NavLink>
        )}
      </div>

      <div className="py-3 border-b border-border">
        <div className="px-5 pb-2 pt-1 text-[11px] font-semibold uppercase tracking-wide text-dim">Trading</div>
        <NavLink
          to="/bot"
          className={({ isActive }) =>
            `flex items-center gap-2 px-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
          }
        >
          {isRunning ? (
            <>
              <span
                className={`w-2 h-2 rounded-full shrink-0 ${markets.length === 0 ? "bg-dim" : "bg-up animate-pulse-dot"}`}
              />
              {markets.length === 0
                ? "Idle"
                : `${markets.length} market${markets.length !== 1 ? "s" : ""}`}
            </>
          ) : (
            <>
              <span className="w-2 h-2 rounded-full shrink-0 bg-down" />
              Offline
            </>
          )}
        </NavLink>

        <NavLink
          to="/optimizer"
          className={({ isActive }) =>
            `block px-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
          }
        >
          <span className="inline-flex items-center gap-1">
            <span
              className={`inline-block w-[7px] h-[7px] rounded-full shrink-0 ${
                botStatus?.optimizer?.running ? "bg-up" : "bg-dim"
              } ${botStatus?.optimizer?.state === "optimizing" ? "animate-pulse-dot" : ""}`}
            />
            Optimizer
          </span>
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
              `flex items-center gap-2 pl-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
            }
          >
            <span className="w-2 h-2 rounded-full bg-up animate-pulse-dot shrink-0" />
            {p}
          </NavLink>
        ))}
      </div>

      <div className="py-3 border-b border-border">
        <div className="px-5 pb-2 pt-1 text-[11px] font-semibold uppercase tracking-wide text-dim">Sessions</div>
        {prefixes.map((p) => (
          <div key={p.name}>
            <NavLink
              to={`/sessions/${p.name}`}
              className={({ isActive }) =>
                `block px-5 py-1.5 text-[13px] font-semibold cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
              }
            >
              {p.name}
              <span className="text-dim text-xs ml-1">({p.count})</span>
            </NavLink>
            {p.recentSessions.map((s) => (
              <NavLink
                key={s.file}
                to={`/sessions/${p.name}/${s.file}`}
                className={({ isActive }) =>
                  `flex justify-between items-center py-[3px] pl-8 pr-5 text-xs font-mono cursor-pointer transition-colors ${isActive ? "text-accent" : "text-dim hover:text-txt"}`
                }
              >
                <span>{fmtSessionName(s.file)}</span>
                {s.outcome && (
                  <span className={`text-[10px] font-semibold shrink-0 ml-1.5 ${s.outcome.toLowerCase() === "up" ? "text-up" : "text-down"}`}>
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

function MarketSidebarItem({ market, hasLiveData }: { market: MarketStatus; hasLiveData: boolean }) {
  const strategies = market.strategies ?? [];
  const isTrading = strategies.length > 0;
  const anyDead = strategies.some((s) => s.alive === false);
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);

  const dotColor = anyDead
    ? "bg-down"
    : isTrading
      ? allDryRun ? "bg-accent" : "bg-up"
      : hasLiveData
        ? "bg-up"
        : "bg-dim";

  const shouldPulse = (isTrading && !anyDead) || hasLiveData;

  return (
    <>
      <NavLink
        to={`/live/${market.slug_key}`}
        className={({ isActive }) =>
          `block pl-5 pr-5 py-1.5 text-sm cursor-pointer transition-colors ${isActive ? "bg-accent/15 text-accent" : "text-txt hover:bg-accent/10"}`
        }
      >
        <span className="inline-flex items-center gap-1">
          <span className={`inline-block w-[7px] h-[7px] rounded-full shrink-0 ${dotColor} ${shouldPulse ? "animate-pulse-dot" : ""}`} />
          {market.slug_key}
        </span>
        {market.stop_after_session && (
          <span className="ml-1 px-1.5 py-0.5 rounded text-[9px] font-semibold bg-down text-white">STOPPING</span>
        )}
      </NavLink>
      {strategies.map((s) => (
        <NavLink
          key={s.name}
          to={`/strategy/${market.slug_key}/${s.name}`}
          className="flex justify-between items-center py-0.5 pl-9 pr-5 text-xs font-mono text-dim no-underline hover:text-txt"
        >
          <span className="inline-flex items-center gap-1">
            <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${s.alive === false ? "bg-down" : "bg-up"}`} />
            {s.name}
          </span>
          <span className="text-dim text-xs">
            ${(s.bankroll ?? s.cash).toFixed(0)}
            {s.session_cash_ratio != null && (
              <span className="opacity-60"> ({Math.round(s.session_cash_ratio.ratio * 100)}%:${s.session_cash_ratio.max})</span>
            )}
            {s.session_cash_ratio == null && s.session_cap != null && (
              <span className="opacity-60"> (${s.session_cap}/s)</span>
            )}
          </span>
          {s.dry_run && <span className="inline-block px-2 py-0.5 rounded text-[9px] font-semibold bg-accent/15 text-accent ml-1">DRY</span>}
        </NavLink>
      ))}
    </>
  );
}
