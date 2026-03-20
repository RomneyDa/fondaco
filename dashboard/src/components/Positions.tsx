"use client";

import { trpc } from "@/trpc/client";

export function Positions() {
  const dataQuery = trpc.portfolio.positions.useQuery();
  const loading = dataQuery.isLoading;
  const data = dataQuery.data;

  if (loading) return <div className="text-dim text-sm">Loading positions...</div>;
  if (!data) return <div className="text-dim text-sm">Failed to load positions</div>;

  const { positions, usdc } = data;

  const active = positions
    .filter((p: any) => p.currentValue > 0 || p.redeemable)
    .sort((a: any, b: any) => b.currentValue - a.currentValue);
  const totalPositionValue = active.reduce((s: number, p: any) => s + p.currentValue, 0);
  const totalPortfolio = totalPositionValue + (usdc ?? 0);

  return (
    <div>
      <h2 className="text-xl font-semibold mb-4">Portfolio</h2>

      <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3 mb-6">
        <Card label="Total" value={`$${totalPortfolio.toFixed(2)}`} />
        <Card label="USDC Balance" value={usdc != null ? `$${usdc.toFixed(2)}` : "\u2014"} />
        <Card label="Positions" value={`$${totalPositionValue.toFixed(2)}`} />
        <Card label="Active" value={String(active.length)} />
      </div>

      {active.length === 0 ? (
        <p className="text-dim text-sm">No active positions</p>
      ) : (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-left text-dim text-xs border-b border-border">
              <th className="py-2 pr-3 font-medium">Market</th>
              <th className="py-2 pr-3 font-medium">Side</th>
              <th className="py-2 pr-3 font-medium text-right">Size</th>
              <th className="py-2 pr-3 font-medium text-right">Avg Price</th>
              <th className="py-2 pr-3 font-medium text-right">Cur Price</th>
              <th className="py-2 pr-3 font-medium text-right">Value</th>
              <th className="py-2 font-medium text-right">PnL</th>
            </tr>
          </thead>
          <tbody>
            {active.map((p: any) => {
              const pnlColor = p.cashPnl > 0 ? "text-up" : p.cashPnl < 0 ? "text-down" : "text-dim";
              const pnlSign = p.cashPnl > 0 ? "+" : "";
              const shortTitle = p.title
                .replace(/^Bitcoin Up or Down - /, "BTC ")
                .replace(/^Will Bitcoin /, "BTC ");

              return (
                <tr key={p.asset} className="border-b border-border/50 hover:bg-accent/5">
                  <td className="py-2 pr-3">
                    <div className="max-w-[280px] truncate" title={p.title}>{shortTitle}</div>
                    {p.redeemable && (
                      <span className="inline-block px-1.5 py-0.5 rounded text-[9px] font-semibold bg-up/15 text-up mt-0.5">REDEEMABLE</span>
                    )}
                  </td>
                  <td className="py-2 pr-3">
                    <span className={p.outcome === "Up" ? "text-up" : p.outcome === "Down" ? "text-down" : ""}>{p.outcome}</span>
                  </td>
                  <td className="py-2 pr-3 text-right font-mono">{p.size.toFixed(2)}</td>
                  <td className="py-2 pr-3 text-right font-mono">{p.avgPrice.toFixed(3)}</td>
                  <td className="py-2 pr-3 text-right font-mono">{p.curPrice.toFixed(3)}</td>
                  <td className="py-2 pr-3 text-right font-mono">${p.currentValue.toFixed(2)}</td>
                  <td className={`py-2 text-right font-mono ${pnlColor}`}>
                    {pnlSign}${p.cashPnl.toFixed(2)}
                    <span className="text-[10px] ml-1 opacity-60">({pnlSign}{p.percentPnl.toFixed(1)}%)</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Card({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-card border border-border rounded-lg px-5 py-4">
      <div className="text-[13px] text-dim mb-1.5">{label}</div>
      <div className="text-2xl font-semibold">{value}</div>
    </div>
  );
}
