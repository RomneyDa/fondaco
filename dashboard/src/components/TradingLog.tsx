"use client";

import { useState } from "react";
import { trpc } from "@/trpc/client";
import type { TradeLogEntry } from "@/types";
import { fmtTimeOnly, fmtDateOnly } from "@/utils/formatTime";

interface Props {
  refreshTrigger: number;
}

type Filter = "all" | "success" | "failure" | "resolutions";

export function TradingLog({ refreshTrigger }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [limit, setLimit] = useState(100);

  const tradesQuery = trpc.bot.trades.useQuery({ limit }, {
    refetchInterval: 5000,
  });

  const trades = (tradesQuery.data ?? []) as TradeLogEntry[];
  const loading = tradesQuery.isLoading;

  const filtered = trades.filter((t) => {
    if (filter === "success") return t._type !== "resolution" && t.success === true;
    if (filter === "failure") return t._type !== "resolution" && t.success === false;
    if (filter === "resolutions") return t._type === "resolution";
    return true;
  });

  return (
    <div className="mt-8 bg-card border border-border rounded-lg overflow-hidden">
      <div className="flex justify-between items-center px-4 py-3.5 border-b border-border">
        <h3 className="text-sm font-semibold">Trade Log</h3>
        <div className="flex gap-2">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as Filter)}
            className="!py-1 !px-2 !pr-6 !text-xs"
          >
            <option value="all">All</option>
            <option value="success">Successes</option>
            <option value="failure">Failures</option>
            <option value="resolutions">Results</option>
          </select>
          <select
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value))}
            className="!py-1 !px-2 !pr-6 !text-xs"
          >
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="250">250</option>
            <option value="500">500</option>
          </select>
        </div>
      </div>

      {loading && trades.length === 0 ? (
        <p className="text-dim text-sm p-4">Loading...</p>
      ) : filtered.length === 0 ? (
        <p className="text-dim text-sm p-4">
          {trades.length === 0 ? "No trades recorded yet." : "No matching trades."}
        </p>
      ) : (
        <div className="overflow-x-auto max-h-[480px] overflow-y-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Elapsed</th>
                <th>Side</th>
                <th>Price</th>
                <th>Tokens</th>
                <th>Amount</th>
                <th>Status</th>
                {filtered.some((t) => t.strategy) && <th>Strategy</th>}
                <th>Error</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t, i) =>
                t._type === "resolution" ? (
                  <tr key={`res-${i}`} className="bg-[rgba(136,132,216,0.06)] border-y border-border">
                    <td colSpan={filtered.some((tr) => tr.strategy) ? 9 : 8}>
                      <div className="flex items-center gap-2.5 text-xs">
                        <span className="font-mono text-xs text-dim">
                          {t.timestamp ? (
                            <>
                              <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(t.timestamp)}</span>
                              {fmtTimeOnly(t.timestamp)}
                            </>
                          ) : "\u2014"}
                        </span>
                        <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                          t.pnl != null && t.pnl > 0 ? "bg-up/15 text-up" : t.pnl != null && t.pnl < 0 ? "bg-down/15 text-down" : "bg-[rgba(136,132,216,0.15)] text-dim"
                        }`}>
                          {t.outcome}
                        </span>
                        {t.strategy && <span className="font-medium">{t.strategy}</span>}
                        {t.trades != null && <span className="text-dim">{t.trades} trade{t.trades !== 1 ? "s" : ""}</span>}
                        {t.pnl != null && (
                          <span className={`font-semibold font-mono ${t.pnl > 0 ? "text-up" : t.pnl < 0 ? "text-down" : ""}`}>
                            {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)}
                          </span>
                        )}
                        {t.final_cash != null && (
                          <span className="text-dim">${t.final_cash.toFixed(2)}</span>
                        )}
                      </div>
                    </td>
                  </tr>
                ) : (
                  <tr key={i} className={t.success === false ? "bg-down/[0.04]" : ""}>
                    <td className="font-mono text-xs">
                      {t.timestamp ? (
                        <>
                          <span className="text-dim mr-1.5 text-[11px]">{fmtDateOnly(t.timestamp)}</span>
                          {fmtTimeOnly(t.timestamp)}
                        </>
                      ) : "\u2014"}
                    </td>
                    <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "\u2014"}</td>
                    <td>
                      <span className={`font-semibold text-xs ${t.side.includes("up") ? "text-up" : "text-down"}`}>
                        {t.side === "buy_up" ? "Buy Up" : t.side === "buy_down" ? "Buy Down" : t.side}
                      </span>
                    </td>
                    <td>${t.price.toFixed(2)}</td>
                    <td>{t.tokens}</td>
                    <td>${t.amount.toFixed(2)}</td>
                    <td>
                      <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                        t.success === false ? "bg-down/15 text-down" : "bg-up/15 text-up"
                      }`}>
                        {t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM"}
                      </span>
                    </td>
                    {filtered.some((tr) => tr.strategy) && (
                      <td>{t.strategy || "\u2014"}</td>
                    )}
                    <td className="max-w-[240px] overflow-hidden text-ellipsis whitespace-nowrap text-dim text-xs cursor-default" title={t.error || ""}>
                      {t.error || "\u2014"}
                    </td>
                  </tr>
                )
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
