import type { Metadata } from "next";
import { Suspense } from "react";
import GalleryView from "@/components/GalleryView";
import { GridSkeleton } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Favorites",
  description: "Every meme you marked as a favorite (PRD §28).",
};

export default function FavoritesPage() {
  return (
    <Suspense fallback={<GridSkeleton />}>
      <GalleryView mode="favorites" />
    </Suspense>
  );
}
