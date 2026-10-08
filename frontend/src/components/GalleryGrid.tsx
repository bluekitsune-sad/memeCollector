"use client";

import MediaCard, { type MediaCardData } from "./MediaCard";
import { EmptyState, ErrorPanel, GridSkeleton, LoadingRow } from "./StateBlocks";

interface GalleryGridProps {
  items: MediaCardData[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  showScore?: boolean;
  emptyTitle: string;
  emptyHint?: string;
}

/** Responsive card grid with skeleton / error / empty states (PRD §25). */
export default function GalleryGrid({
  items,
  loading,
  error,
  onRetry,
  showScore = false,
  emptyTitle,
  emptyHint,
}: GalleryGridProps) {
  if (error && items.length === 0) {
    return <ErrorPanel message={error} onRetry={onRetry} />;
  }
  if (loading && items.length === 0) {
    return <GridSkeleton />;
  }
  if (items.length === 0) {
    return (
      <>
        {error ? <ErrorPanel message={error} onRetry={onRetry} /> : null}
        <EmptyState title={emptyTitle} hint={emptyHint} />
      </>
    );
  }

  return (
    <>
      {error ? <ErrorPanel message={error} onRetry={onRetry} /> : null}
      {loading ? <LoadingRow label="Refreshing…" /> : null}
      <div className="grid">
        {items.map((item) => (
          <MediaCard key={item.id} item={item} showScore={showScore} />
        ))}
      </div>
    </>
  );
}
