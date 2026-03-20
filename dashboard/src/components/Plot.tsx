"use client";

import dynamic from "next/dynamic";
import type { PlotParams } from "react-plotly.js";

const PlotlyChart = dynamic(() => import("react-plotly.js"), { ssr: false });

type Props = Pick<PlotParams, "data" | "layout" | "config"> & {
  className?: string;
};

export function Plot({ data, layout, config, className }: Props) {
  return (
    <div className={className}>
      <PlotlyChart
        data={data}
        layout={layout}
        config={config}
        useResizeHandler
        style={{ width: "100%", height: "100%" }}
      />
    </div>
  );
}
