"use client";

import { use } from "react";
import { SportsGames } from "@/components/SportsGames";

export default function SportsLeaguePage({ params }: { params: Promise<{ league: string }> }) {
  const { league } = use(params);
  return <SportsGames league={league} />;
}
