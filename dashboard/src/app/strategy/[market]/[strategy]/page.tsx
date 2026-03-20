"use client";

import { use } from "react";
import { StrategyDetail } from "@/components/StrategyDetail";

export default function StrategyDetailPage({ params }: { params: Promise<{ market: string; strategy: string }> }) {
  const { market, strategy } = use(params);
  return <StrategyDetail market={market} strategy={strategy} />;
}
