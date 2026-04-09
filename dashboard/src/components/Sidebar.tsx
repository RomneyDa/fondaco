"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { PingEntry } from "./AppShell";
import { trpc } from "@/trpc/client";
import type { BotStatus, MarketStatus } from "@/types";
import { fmtSessionName } from "@/utils/formatTime";
import { NavLink } from "./NavLink";

interface Props {
  prefixNames: string[];
  livePrefixes: string[];
  botStatus: BotStatus | null;
  pings: PingEntry[];
  balance: { portfolio: number | null };
}

export function Sidebar({ prefixNames, livePrefixes, botStatus, pings, balance }: Props) {
  const isRunning = botStatus?.running ?? false;
  const markets = Array.isArray(botStatus?.markets) ? botStatus.markets : [];
  const marketsBySlug = new Map(markets.map((m) => [m.slug_key, m]));

  const allMarketNames = new Set([
    ...prefixNames,
    ...markets.map((m) => m.slug_key),
    ...livePrefixes,
  ]);

  return (
    <nav className="w-full h-full bg-sidebar border-r border-border flex flex-col overflow-y-auto scrollbar-none">
      <div className="px-5 py-4 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/icon.svg" alt="Fondaco" width={24} height={24} className="rounded-md" />
          <h1 className="text-lg font-semibold text-accent">fondaco</h1>
          <Link href="/architecture" title="Architecture" className="text-dim hover:text-accent transition-colors opacity-50 hover:opacity-100">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="currentColor">
              <path d="M1 2.5A1.5 1.5 0 012.5 1h3A1.5 1.5 0 017 2.5v3A1.5 1.5 0 015.5 7h-3A1.5 1.5 0 011 5.5v-3zm8 0A1.5 1.5 0 0110.5 1h3A1.5 1.5 0 0115 2.5v3A1.5 1.5 0 0113.5 7h-3A1.5 1.5 0 019 5.5v-3zm-8 8A1.5 1.5 0 012.5 9h3A1.5 1.5 0 017 10.5v3A1.5 1.5 0 015.5 15h-3A1.5 1.5 0 011 13.5v-3zm8 0A1.5 1.5 0 0110.5 9h3a1.5 1.5 0 011.5 1.5v3a1.5 1.5 0 01-1.5 1.5h-3A1.5 1.5 0 019 13.5v-3z" />
            </svg>
          </Link>
        </div>
        {balance.portfolio != null && (
          <Link href="/positions" className="text-sm font-mono text-dim hover:text-accent transition-colors">
            ${balance.portfolio.toFixed(2)}
          </Link>
        )}
      </div>

      <div className="py-3 border-b border-border">
        <NavLink
          href="/home"
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
          href="/optimizer"
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
          const market = marketsBySlug.get(name);
          const hasLive = livePrefixes.includes(name);
          return (
            <MarketSection
              key={name}
              name={name}
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
        <span className="opacity-40 text-[10px]">{expanded ? "\u25BC" : "\u25B6"}</span>
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
  market,
  hasLiveData,
}: {
  name: string;
  market: MarketStatus | undefined;
  hasLiveData: boolean;
}) {
  const [expanded, setExpanded] = useState(true);
  const pathname = usePathname();
  const strategies = market?.strategies ?? [];
  const isTrading = strategies.length > 0;
  const anyDead = strategies.some((s) => s.alive === false);
  const allDryRun = strategies.length > 0 && strategies.every((s) => s.dry_run);

  // Fetch summaries for this market when expanded
  const summariesQuery = trpc.sessions.summaries.useQuery(
    { prefix: name, limit: 10 },
    { enabled: expanded }
  );
  const sessionsQuery = trpc.sessions.list.useQuery(
    { prefix: name },
    { enabled: expanded }
  );

  const sessionCount = sessionsQuery.data?.length ?? 0;
  const recentSessions = summariesQuery.data ?? [];

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
        <Link
          href={hasLiveData ? `/live/${name}` : `/sessions/${name}`}
          className={`flex-1 truncate ml-1.5 cursor-pointer no-underline transition-colors ${
            pathname.startsWith(`/live/${name}`) || pathname.startsWith(`/sessions/${name}`)
              ? "text-accent"
              : "text-txt hover:text-accent"
          }`}
        >
          {name}
        </Link>
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
          {strategies.map((s) => (
            <Link
              key={s.name}
              href={`/strategy/${name}/${s.name}`}
              className="flex justify-between items-center py-[3px] pl-8 pr-5 text-xs font-mono text-dim no-underline hover:text-txt"
            >
              <span className="inline-flex items-center gap-1">
                <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${s.alive === false ? "bg-down" : "bg-up"}`} />
                {s.name}
              </span>
              <span className="text-dim text-xs">
                ${(s.bankroll ?? s.cash).toFixed(0)}
              </span>
            </Link>
          ))}

          {hasLiveData && sessionCount > 0 && (() => {
            const onSessionsPage = pathname === `/sessions/${name}`;
            const onAnySession = pathname.startsWith(`/sessions/${name}`);
            const highlight = onAnySession && !onSessionsPage;
            return (
              <Link
                href={`/sessions/${name}`}
                className={`block pl-8 pr-5 py-[3px] text-xs cursor-pointer transition-colors ${
                  onSessionsPage || highlight ? "text-accent" : "text-dim hover:text-txt hover:underline"
                }`}
              >
                All {sessionCount} sessions <span className="text-base leading-none">&rarr;</span>
              </Link>
            );
          })()}

          {recentSessions.map((s) => (
            <Link
              key={s.file}
              href={`/sessions/${name}/${s.file}`}
              className={`flex justify-between items-center py-[3px] pl-8 pr-5 text-xs font-mono cursor-pointer transition-colors ${
                pathname === `/sessions/${name}/${s.file}` ? "text-accent" : "text-dim hover:text-txt"
              }`}
            >
              <span>{fmtSessionName(s.file)}</span>
              {s.outcome && (
                <span className={`text-[11px] shrink-0 ml-1.5 ${s.outcome.toLowerCase() === "up" ? "text-up" : "text-down"}`}>
                  {s.outcome.toLowerCase() === "up" ? "\u25B2" : "\u25BC"}
                </span>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
