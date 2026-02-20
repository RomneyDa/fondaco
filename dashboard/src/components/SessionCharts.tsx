import { useMemo } from "react";
import type { SessionData } from "../types";
import { Plot } from "./Plot";

interface Props {
  session: SessionData;
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

export function SessionCharts({ session }: Props) {
  const samples = session.samples || [];

  const elapsed = useMemo(() => samples.map((s) => s.elapsed_seconds), [samples]);

  if (samples.length === 0) {
    return <p className="subtitle">No samples yet</p>;
  }

  return (
    <>
      {/* BTC Price */}
      <Plot
        className="chart"
        data={[
          {
            x: elapsed,
            y: samples.map((s) => s.btc_price_usd),
            type: "scatter",
            mode: "lines",
            name: "BTC/USD",
            line: { color: "#f7931a", width: 2 },
          },
        ]}
        layout={{
          ...LAYOUT_BASE,
          title: { text: "BTC Price", font: { size: 14 } },
          yaxis: { ...LAYOUT_BASE.yaxis, title: "USD", tickformat: ",.0f" },
        }}
        config={PLOT_CFG}
      />

      {/* Token Midpoints */}
      <Plot
        className="chart"
        data={[
          {
            x: elapsed,
            y: samples.map((s) => s.up_token_midpoint),
            type: "scatter",
            mode: "lines",
            name: "Up",
            line: { color: "#3fb950", width: 2 },
          },
          {
            x: elapsed,
            y: samples.map((s) => s.down_token_midpoint),
            type: "scatter",
            mode: "lines",
            name: "Down",
            line: { color: "#f85149", width: 2 },
          },
        ]}
        layout={{
          ...LAYOUT_BASE,
          title: { text: "Token Midpoints", font: { size: 14 } },
          yaxis: { ...LAYOUT_BASE.yaxis, title: "Price" },
        }}
        config={PLOT_CFG}
      />

      {/* BTC + Midpoints overlay */}
      <Plot
        className="chart"
        data={[
          {
            x: elapsed,
            y: samples.map((s) => s.btc_price_usd),
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
        ]}
        layout={{
          ...LAYOUT_BASE,
          title: { text: "BTC Price vs Token Midpoints", font: { size: 14 } },
          yaxis: { ...LAYOUT_BASE.yaxis, title: "Token Price", side: "left" },
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
