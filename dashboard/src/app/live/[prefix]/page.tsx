"use client";

import { use } from "react";
import { LiveView } from "@/components/LiveView";

export default function LiveViewPage({ params }: { params: Promise<{ prefix: string }> }) {
  const { prefix } = use(params);
  return <LiveView prefix={prefix} />;
}
