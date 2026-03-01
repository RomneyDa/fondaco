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

function buildTradeTraces(trades: BotTrade[], isDryRun: boolean) {
  const buyUp = trades.filter((t) => t.side === "buy_up");
  const buyDown = trades.filter((t) => t.side === "buy_down");
  const sellUp = trades.filter((t) => t.side === "sell_up");
  const sellDown = trades.filter((t) => t.side === "sell_down");

  const opacity = isDryRun ? 0.5 : 1.0;

  const traces: any[] = [];

  if (buyUp.length > 0) {
    traces.push({
      x: buyUp.map((t) => t.elapsed_seconds),
      y: buyUp.map((t) => t.price),
      type: "scatter",
      mode: "markers",
      name: "Buy Up",
      marker: {
        symbol: "triangle-up",
        size: 12,
        color: "#3fb950",
        opacity,
        line: isDryRun
          ? { color: "#3fb950", width: 2, dash: "dot" }
          : undefined,
      },
      text: buyUp.map(
        (t) => `${t._strategy ? t._strategy + ": " : ""}buy_up $${t.amount.toFixed(2)} (${t.tokens.toFixed(1)} tokens)`
      ),
      hoverinfo: "text+x",
    });
  }

  if (buyDown.length > 0) {
    traces.push({
      x: buyDown.map((t) => t.elapsed_seconds),
      y: buyDown.map((t) => t.price),
      type: "scatter",
      mode: "markers",
      name: "Buy Down",
      marker: {
        symbol: "triangle-up",
        size: 12,
        color: "#f85149",
        opacity,
        line: isDryRun
          ? { color: "#f85149", width: 2, dash: "dot" }
          : undefined,
      },
      text: buyDown.map(
        (t) =>
          `${t._strategy ? t._strategy + ": " : ""}buy_down $${t.amount.toFixed(2)} (${t.tokens.toFixed(1)} tokens)`
      ),
      hoverinfo: "text+x",
    });
  }

  if (sellUp.length > 0) {
    traces.push({
      x: sellUp.map((t) => t.elapsed_seconds),
      y: sellUp.map((t) => t.price),
      type: "scatter",
      mode: "markers",
      name: "Sell Up",
      marker: {
        symbol: "triangle-down",
        size: 12,
        color: "#3fb950",
        opacity,
        line: isDryRun
          ? { color: "#3fb950", width: 2, dash: "dot" }
          : undefined,
      },
      text: sellUp.map(
        (t) =>
          `${t._strategy ? t._strategy + ": " : ""}sell_up $${t.amount.toFixed(2)} (${t.tokens.toFixed(1)} tokens)`
      ),
      hoverinfo: "text+x",
    });
  }

  if (sellDown.length > 0) {
    traces.push({
      x: sellDown.map((t) => t.elapsed_seconds),
      y: sellDown.map((t) => t.price),
      type: "scatter",
      mode: "markers",
      name: "Sell Down",
      marker: {
        symbol: "triangle-down",
        size: 12,
        color: "#f85149",
        opacity,
        line: isDryRun
          ? { color: "#f85149", width: 2, dash: "dot" }
          : undefined,
      },
      text: sellDown.map(
        (t) =>
          `${t._strategy ? t._strategy + ": " : ""}sell_down $${t.amount.toFixed(2)} (${t.tokens.toFixed(1)} tokens)`
      ),
      hoverinfo: "text+x",
    });
  }

  return traces;
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
