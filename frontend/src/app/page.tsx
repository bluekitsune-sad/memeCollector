import type { Metadata } from "next";
import { Suspense } from "react";
import GalleryView from "@/components/GalleryView";
import { GridSkeleton } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Gallery",
  description: "Browse, filter and paginate the MemeVault archive.",
};

export default function GalleryPage() {
  return (
    <Suspense fallback={<GridSkeleton />}>
      <GalleryView mode="library" />
    </Suspense>
  );
}
