import { useMemo } from "react";
import type { BotTrade, SessionData } from "../types";
import { Plot } from "./Plot";

interface Props {
  session: SessionData;
  trades?: BotTrade[];
  isDryRun?: boolean;
}

const LAYOUT_BASE = {
  height: 300,
  paper_bgcolor: "transparent",
  plot_bgcolor: "transparent",
  font: { color: "#e6edf3", size: 12 },
  margin: { l: 60, r: 60, t: 40, b: 50 },
  xaxis: {
    title: "Elapsed (s)",
    gridcolor: "#30363d",
    zerolinecolor: "#30363d",
  },
  yaxis: {
    gridcolor: "#30363d",
    zerolinecolor: "#30363d",
  },
  legend: { orientation: "h" as const, y: -0.2 },
};

const PLOT_CFG = { responsive: true, displayModeBar: false };

function fmtTradeHover(t: BotTrade, label: string): string {
  const prefix = t._strategy ? t._strategy + ": " : "";
  const tkns = t.tokens < 0.01 && t.tokens > 0 ? "<0.01" : t.tokens.toFixed(1);
  const amt = t.amount < 0.01 && t.amount > 0 ? "<$0.01" : "$" + t.amount.toFixed(2);
  return `${prefix}${label} ${amt} (${tkns} tokens)`;
}

interface TraceConfig {
  trades: BotTrade[];
  name: string;
  label: string;
  symbol: string;
  color: string;
}

function buildTradeTraces(trades: BotTrade[], isDryRun: boolean) {
  const opacity = isDryRun ? 0.5 : 1.0;
  const configs: TraceConfig[] = [
    { trades: trades.filter((t) => t.side === "buy_up"), name: "Buy Up", label: "buy_up", symbol: "triangle-up", color: "#3fb950" },
    { trades: trades.filter((t) => t.side === "buy_down"), name: "Buy Down", label: "buy_down", symbol: "triangle-up", color: "#f85149" },
    { trades: trades.filter((t) => t.side === "sell_up"), name: "Sell Up", label: "sell_up", symbol: "triangle-down", color: "#3fb950" },
    { trades: trades.filter((t) => t.side === "sell_down"), name: "Sell Down", label: "sell_down", symbol: "triangle-down", color: "#f85149" },
  ];

  return configs
    .filter((c) => c.trades.length > 0)
    .map((c) => ({
      x: c.trades.map((t) => t.elapsed_seconds),
      y: c.trades.map((t) => t.price),
      type: "scatter",
      mode: "markers",
      name: c.name,
      marker: {
        symbol: c.symbol,
        size: 12,
        color: c.color,
        opacity,
        line: isDryRun ? { color: c.color, width: 2, dash: "dot" } : undefined,
      },
      text: c.trades.map((t) => fmtTradeHover(t, c.label)),
      hoverinfo: "text+x",
    }));
}

export function SessionCharts({ session, trades, isDryRun }: Props) {
  const samples = session.samples || [];

  const elapsed = useMemo(
    () => samples.map((s) => s.elapsed_seconds),
    [samples]
  );

  // BTC price only changes ~1/sec (Chainlink) vs ~18/sec (CLOB), so deduplicate
  // to reduce render cost while keeping the same visual shape.
  const btcDeduped = useMemo(() => {
    const x: number[] = [];
    const y: (number | null)[] = [];
    let prev: number | null = null;
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i].btc_price_usd;
      if (v !== prev || i === samples.length - 1) {
        x.push(samples[i].elapsed_seconds);
        y.push(v);
        prev = v;
      }
    }
    return { x, y };
  }, [samples]);

  const tradeTraces = useMemo(
    () => (trades && trades.length > 0 ? buildTradeTraces(trades, !!isDryRun) : []),
    [trades, isDryRun]
  );

  if (samples.length === 0) {
    return <p className="subtitle">No samples yet</p>;
  }

  return (
    <>
      <Plot
        className="chart"
        data={[
          {
            x: btcDeduped.x,
            y: btcDeduped.y,
            type: "scatter",
            mode: "lines",
            name: "BTC/USD",
            line: { color: "#f7931a", width: 1.5 },
            yaxis: "y2",
          },
          {
            x: elapsed,
            y: samples.map((s) => s.up_token_midpoint),
            type: "scatter",
            mode: "lines",
            name: "Up Mid",
            line: { color: "#3fb950", width: 2 },
          },
          {
            x: elapsed,
            y: samples.map((s) => s.down_token_midpoint),
            type: "scatter",
            mode: "lines",
            name: "Down Mid",
            line: { color: "#f85149", width: 2 },
          },
          ...tradeTraces,
        ]}
        layout={{
          ...LAYOUT_BASE,
          title: {
            text: "BTC Price vs Token Midpoints",
            font: { size: 14 },
          },
          yaxis: {
            ...LAYOUT_BASE.yaxis,
            title: "Token Price",
            side: "left",
          },
          yaxis2: {
            title: "BTC (USD)",
            overlaying: "y",
            side: "right",
            gridcolor: "transparent",
            tickformat: ",.0f",
          },
        }}
        config={PLOT_CFG}
      />
    </>
  );
}
