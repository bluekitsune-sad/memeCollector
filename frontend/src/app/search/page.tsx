import type { Metadata } from "next";
import { Suspense } from "react";
import SearchView from "@/components/SearchView";
import { GridSkeleton } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Search",
  description: "Hybrid keyword + semantic search across the archive (PRD §22–§24).",
};

export default function SearchPage() {
  return (
    <Suspense fallback={<GridSkeleton />}>
      <SearchView />
    </Suspense>
  );
}
