"use client";

import { use } from "react";
import { SessionDetail } from "@/components/SessionDetail";

export default function SessionDetailPage({ params }: { params: Promise<{ prefix: string; file: string }> }) {
  const { prefix, file } = use(params);
  return <SessionDetail prefix={prefix} file={file} />;
}
