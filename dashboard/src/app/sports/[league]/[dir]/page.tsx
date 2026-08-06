"use client";

import { use } from "react";
import { SportsGameDetail } from "@/components/SportsGameDetail";

export default function SportsGamePage({
  params,
}: {
  params: Promise<{ league: string; dir: string }>;
}) {
  const { league, dir } = use(params);
  return <SportsGameDetail league={league} dir={decodeURIComponent(dir)} />;
}
