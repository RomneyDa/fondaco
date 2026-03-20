"use client";

import { use } from "react";
import { AllTime } from "@/components/AllTime";

export default function AllTimePage({ params }: { params: Promise<{ prefix: string }> }) {
  const { prefix } = use(params);
  return <AllTime prefix={prefix} />;
}
