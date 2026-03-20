"use client";

import type { BotTrade } from "@/types";

interface PendingOrder {
  order_id?: string;
  side: string;
  token: string;
  price: number;
  tokens: number;
  _strategy?: string;
}

interface Props {
  trades: BotTrade[];
  pendingOrders?: PendingOrder[];
}

function formatSide(side: string): string {
  switch (side) {
    case "buy_up": return "Buy Up";
    case "buy_down": return "Buy Down";
    case "sell_up": return "Sell Up";
    case "sell_down": return "Sell Down";
    default: return side;
  }
}

function fmtTokens(v: number): string {
  if (v < 0.01 && v > 0) return "<0.01";
  if (Number.isInteger(v) || Math.abs(v - Math.round(v)) < 0.001) return Math.round(v).toString();
  return v.toFixed(2);
}

function fmtAmount(v: number | undefined): string {
  const n = v ?? 0;
  if (n < 0.01 && n > 0) return "<$0.01";
  return "$" + n.toFixed(2);
}

export function TradeTable({ trades, pendingOrders }: Props) {
  const hasPending = pendingOrders && pendingOrders.length > 0;
  const isEmpty = trades.length === 0 && !hasPending;

  return (
    <div className="mt-8 bg-card border border-border rounded-lg overflow-hidden">
      <div className="flex justify-between items-center px-4 py-3.5 border-b border-border">
        <h3 className="text-sm font-semibold">Session Trades</h3>
      </div>
      {isEmpty ? (
        <p className="text-dim text-sm p-4">No trades this session.</p>
      ) : (
        <div className="overflow-x-auto max-h-[480px] overflow-y-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Elapsed</th>
                <th>Strategy</th>
                <th>Side</th>
                <th>Price</th>
                <th>Tokens</th>
                <th>Amount</th>
                <th>Fee</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {hasPending && pendingOrders.map((o, i) => (
                <tr key={`pending-${i}`} className="bg-accent/[0.04]">
                  <td className="text-dim text-xs">—</td>
                  <td>{o._strategy || "—"}</td>
                  <td>
                    <span className={`font-semibold text-xs ${o.side.includes("up") ? "text-up" : "text-down"}`}>
                      {formatSide(o.side)}
                    </span>
                  </td>
                  <td>${o.price.toFixed(2)}</td>
                  <td>{fmtTokens(o.tokens)}</td>
                  <td>{fmtAmount(o.tokens * o.price)}</td>
                  <td className="text-dim text-xs">—</td>
                  <td>
                    <span className="inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold bg-accent/15 text-accent animate-pulse">
                      PENDING
                    </span>
                  </td>
                </tr>
              ))}
              {trades.map((t: any, i: number) => (
                <tr key={i} className={t.success === false ? "bg-down/[0.04]" : ""}>
                  <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "—"}</td>
                  <td>{t._strategy || "—"}</td>
                  <td>
                    <span className={`font-semibold text-xs ${t.side.includes("up") ? "text-up" : "text-down"}`}>
                      {formatSide(t.side)}
                    </span>
                  </td>
                  <td>${t.price.toFixed(2)}</td>
                  <td>
                    {fmtTokens(t.tokens)}
                    {t.requested_tokens != null && t.requested_tokens !== t.tokens && (
                      <span className="text-dim text-[11px]"> / {fmtTokens(t.requested_tokens)}</span>
                    )}
                  </td>
                  <td>{fmtAmount(t.amount)}</td>
                  <td className="text-dim text-xs">{t.fee != null ? fmtAmount(t.fee) : "—"}</td>
                  <td>
                    <span className={`inline-block px-2 py-0.5 rounded-[10px] text-[11px] font-semibold ${
                      t.success === false ? "bg-down/15 text-down" : "bg-up/15 text-up"
                    }`}>
                      {t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
