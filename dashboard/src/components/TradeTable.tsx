import type { BotTrade } from "../types";

interface Props {
  trades: BotTrade[];
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
  return v.toFixed(v === Math.floor(v) ? 0 : 1);
}

function fmtAmount(v: number): string {
  if (v < 0.01 && v > 0) return "<$0.01";
  return "$" + v.toFixed(2);
}

export function TradeTable({ trades }: Props) {
  return (
    <div className="trade-log">
      <div className="trade-log-header">
        <h3>Session Trades</h3>
      </div>
      {trades.length === 0 ? (
        <p className="subtitle" style={{ padding: "16px" }}>No trades yet</p>
      ) : (
        <div className="trade-log-table-wrap">
          <table className="trade-log-table">
            <thead>
              <tr>
                <th>Elapsed</th>
                <th>Strategy</th>
                <th>Side</th>
                <th>Price</th>
                <th>Tokens</th>
                <th>Amount</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t: any, i: number) => (
                <tr key={i} className={t.success === false ? "trade-row-fail" : ""}>
                  <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "\u2014"}</td>
                  <td>{t._strategy || "\u2014"}</td>
                  <td>
                    <span className={`trade-side ${t.side.includes("up") ? "side-up" : "side-down"}`}>
                      {formatSide(t.side)}
                    </span>
                  </td>
                  <td>${t.price.toFixed(2)}</td>
                  <td>{fmtTokens(t.tokens)}</td>
                  <td>{fmtAmount(t.amount)}</td>
                  <td>
                    <span className={`trade-status-pill ${t.success === false ? "status-fail" : "status-ok"}`}>
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
