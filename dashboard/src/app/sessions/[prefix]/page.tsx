"use client";

import { use } from "react";
import { SessionList } from "@/components/SessionList";

export default function SessionListPage({ params }: { params: Promise<{ prefix: string }> }) {
  const { prefix } = use(params);
  return <SessionList prefix={prefix} />;
}
