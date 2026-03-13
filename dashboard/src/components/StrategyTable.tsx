export interface StrategyRow {
  name: string;
  dry_run?: boolean;
  bankroll?: number;
  session_cash_ratio?: { ratio: number; max: number } | null;
  session_cap?: number | null;
  trades: number;
  fees: number;
  up: number;
  down: number;
  pnl: number;
  hasPnl: boolean;
  posValue: number;
  finalCash?: number | null;
}

interface Props {
  rows: StrategyRow[];
  showBankroll?: boolean;
  showPosition?: boolean;
  showFinalCash?: boolean;
}

export function StrategyTable({ rows, showBankroll, showPosition, showFinalCash }: Props) {
  if (rows.length === 0) return null;

  let totalPnl = 0;
  let totalPosValue = 0;
  let totalTrades = 0;
  let totalFees = 0;
  let dryPnl = 0;
  let hasDry = false;
  for (const r of rows) {
    if (r.dry_run) {
      dryPnl += r.pnl;
      hasDry = true;
    } else {
      totalPnl += r.pnl;
    }
    totalPosValue += r.posValue;
    totalTrades += r.trades;
    totalFees += r.fees;
  }

  const showTotal = rows.length > 1 && rows.some((r) => r.hasPnl);

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden mb-4">
      <table className="data-table strategy-tbl">
        <thead>
          <tr>
            <th>Strategy</th>
            {showBankroll && <th className="text-right">Bankroll</th>}
            {showBankroll && <th className="text-right">Limit</th>}
            {showPosition && <th className="text-right">Position</th>}
            {showPosition && <th className="text-right">Value</th>}
            <th className="text-right">PnL</th>
            <th className="text-right">Fees</th>
            {showFinalCash && <th className="text-right">Cash</th>}
            <th className="text-right">Trades</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name}>
              <td>
                {r.name}
                {r.dry_run && (
                  <span className="inline-block ml-1.5 px-2 py-0.5 rounded text-[11px] font-semibold bg-accent/15 text-accent">
                    DRY
                  </span>
                )}
              </td>
              {showBankroll && (
                <td className="text-right mono">
                  {r.bankroll != null ? `$${r.bankroll.toFixed(2)}` : "—"}
                </td>
              )}
              {showBankroll && (
                <td className="text-right mono text-dim">
                  {r.session_cash_ratio != null
                    ? `${Math.round(r.session_cash_ratio.ratio * 100)}%:$${r.session_cash_ratio.max.toFixed(0)}`
                    : r.session_cap != null
                      ? `$${r.session_cap.toFixed(0)}/s`
                      : "—"}
                </td>
              )}
              {showPosition && (
                <td className="text-right">
                  {r.up > 0 && <span className="inline-block text-[11px] px-1.5 py-px rounded-lg font-semibold tabular-nums mr-0.5 bg-up/15 text-up">{r.up} Up</span>}
                  {r.down > 0 && <span className="inline-block text-[11px] px-1.5 py-px rounded-lg font-semibold tabular-nums mr-0.5 bg-down/15 text-down">{r.down} Dn</span>}
                  {r.up === 0 && r.down === 0 && <span className="text-dim">—</span>}
                </td>
              )}
              {showPosition && (
                <td className="text-right mono">
                  {r.up > 0 || r.down > 0 ? `$${r.posValue.toFixed(2)}` : "—"}
                </td>
              )}
              <td className={`text-right mono ${r.pnl > 0 ? "!text-up" : r.pnl < 0 ? "!text-down" : ""}`}>
                {r.hasPnl
                  ? `${r.pnl >= 0 ? "+$" : "-$"}${Math.abs(r.pnl).toFixed(2)}`
                  : "—"}
              </td>
              <td className="text-right mono text-dim">
                {r.fees > 0 ? `$${r.fees.toFixed(2)}` : "—"}
              </td>
              {showFinalCash && (
                <td className="text-right mono">
                  {r.finalCash != null ? `$${r.finalCash.toFixed(2)}` : "—"}
                </td>
              )}
              <td className="text-right mono">{r.trades > 0 ? r.trades : "—"}</td>
            </tr>
          ))}
          {showTotal && (
            <tr className="border-t-2 border-border font-semibold">
              <td className="!border-b-0">Total</td>
              {showBankroll && <td className="!border-b-0" />}
              {showBankroll && <td className="!border-b-0" />}
              {showPosition && <td className="!border-b-0" />}
              {showPosition && (
                <td className="text-right mono !border-b-0">${totalPosValue.toFixed(2)}</td>
              )}
              <td className={`text-right mono !border-b-0 ${totalPnl > 0 ? "!text-up" : totalPnl < 0 ? "!text-down" : ""}`}>
                {totalPnl >= 0 ? "+$" : "-$"}
                {Math.abs(totalPnl).toFixed(2)}
                {hasDry && (
                  <span className="text-accent text-[11px] ml-1.5 font-normal opacity-70">
                    ({dryPnl >= 0 ? "+" : ""}{dryPnl.toFixed(2)} dry)
                  </span>
                )}
              </td>
              <td className="text-right mono text-dim !border-b-0">
                {totalFees > 0 ? `$${totalFees.toFixed(2)}` : ""}
              </td>
              {showFinalCash && <td className="!border-b-0" />}
              <td className="text-right mono !border-b-0">{totalTrades || ""}</td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
