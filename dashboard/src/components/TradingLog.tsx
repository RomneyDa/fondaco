import { useEffect, useState } from "react";
import { fetchTrades } from "../api";
import type { TradeLogEntry } from "../types";

interface Props {
  refreshTrigger: number;
}

type Filter = "all" | "success" | "failure" | "resolutions";

export function TradingLog({ refreshTrigger }: Props) {
  const [trades, setTrades] = useState<TradeLogEntry[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [limit, setLimit] = useState(100);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    fetchTrades(limit).then((t) => {
      setTrades(t);
      setLoading(false);
    });
  }, [limit, refreshTrigger]);

  const filtered = trades.filter((t) => {
    if (filter === "success") return t._type !== "resolution" && t.success === true;
    if (filter === "failure") return t._type !== "resolution" && t.success === false;
    if (filter === "resolutions") return t._type === "resolution";
    return true;
  });

  function formatTime(ts: string): string {
    try {
      const d = new Date(ts);
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } catch {
      return ts;
    }
  }

  function formatDate(ts: string): string {
    try {
      const d = new Date(ts);
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    } catch {
      return "";
    }
  }

  return (
    <div className="trade-log">
      <div className="trade-log-header">
        <h3>Trade Log</h3>
        <div className="trade-log-controls">
          <select
            value={filter}
            onChange={(e) => setFilter(e.target.value as Filter)}
          >
            <option value="all">All</option>
            <option value="success">Successes</option>
            <option value="failure">Failures</option>
            <option value="resolutions">Results</option>
          </select>
          <select
            value={limit}
            onChange={(e) => setLimit(parseInt(e.target.value))}
          >
            <option value="50">50</option>
            <option value="100">100</option>
            <option value="250">250</option>
            <option value="500">500</option>
          </select>
        </div>
      </div>

      {loading && trades.length === 0 ? (
        <p className="subtitle" style={{ padding: 16 }}>Loading...</p>
      ) : filtered.length === 0 ? (
        <p className="subtitle" style={{ padding: 16 }}>
          {trades.length === 0 ? "No trades recorded yet." : "No matching trades."}
        </p>
      ) : (
        <div className="trade-log-table-wrap">
          <table className="trade-log-table">
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
                  <tr key={`res-${i}`} className="trade-row-resolution">
                    <td className="trade-time">
                      {t.timestamp ? (
                        <>
                          <span className="trade-date">{formatDate(t.timestamp)}</span>
                          {formatTime(t.timestamp)}
                        </>
                      ) : "\u2014"}
                    </td>
                    <td colSpan={filtered.some((tr) => tr.strategy) ? 7 : 6} className="resolution-cell">
                      <span className={`trade-status-pill ${
                        t.pnl != null && t.pnl > 0 ? "status-win" : t.pnl != null && t.pnl < 0 ? "status-loss" : "status-neutral"
                      }`}>
                        {t.outcome}
                      </span>
                      {t.strategy && <span className="resolution-strategy">{t.strategy}</span>}
                      {t.trades != null && <span className="resolution-detail">{t.trades} trade{t.trades !== 1 ? "s" : ""}</span>}
                      {t.pnl != null && (
                        <span className={`resolution-pnl ${t.pnl > 0 ? "pnl-positive" : t.pnl < 0 ? "pnl-negative" : ""}`}>
                          {t.pnl >= 0 ? "+" : ""}{t.pnl.toFixed(2)}
                        </span>
                      )}
                      {t.final_cash != null && (
                        <span className="resolution-detail">${t.final_cash.toFixed(2)}</span>
                      )}
                    </td>
                  </tr>
                ) : (
                  <tr key={i} className={t.success === false ? "trade-row-fail" : ""}>
                    <td className="trade-time">
                      {t.timestamp ? (
                        <>
                          <span className="trade-date">{formatDate(t.timestamp)}</span>
                          {formatTime(t.timestamp)}
                        </>
                      ) : t.session_start ? (
                        <>
                          <span className="trade-date">{formatDate(t.session_start)}</span>
                          {formatTime(t.session_start)}
                        </>
                      ) : (
                        "\u2014"
                      )}
                    </td>
                    <td>{t.elapsed_seconds != null ? `${t.elapsed_seconds.toFixed(1)}s` : "\u2014"}</td>
                    <td>
                      <span className={`trade-side ${t.side.includes("up") ? "side-up" : "side-down"}`}>
                        {t.side === "buy_up" ? "Buy Up" : t.side === "buy_down" ? "Buy Down" : t.side}
                      </span>
                    </td>
                    <td>${t.price.toFixed(2)}</td>
                    <td>{t.tokens}</td>
                    <td>${t.amount.toFixed(2)}</td>
                    <td>
                      <span className={`trade-status-pill ${t.success === false ? "status-fail" : "status-ok"}`}>
                        {t.success === false ? "FAIL" : t.success === true ? "OK" : "SIM"}
                      </span>
                    </td>
                    {filtered.some((tr) => tr.strategy) && (
                      <td>{t.strategy || "\u2014"}</td>
                    )}
                    <td className="trade-error-cell" title={t.error || ""}>
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
