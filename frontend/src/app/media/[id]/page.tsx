import type { Metadata } from "next";
import { Suspense } from "react";
import MediaDetailClient from "@/components/detail/MediaDetailClient";
import { DetailSkeleton, ErrorPanel } from "@/components/StateBlocks";

export const metadata: Metadata = {
  title: "Media",
  description: "Preview, edit, source and actions for one media item (PRD §26).",
};

interface MediaPageProps {
  params: Promise<{ id: string }>;
}

export default function MediaPage({ params }: MediaPageProps) {
  return (
    <Suspense fallback={<DetailSkeleton />}>
      <MediaDetail params={params} />
    </Suspense>
  );
}

async function MediaDetail({ params }: MediaPageProps) {
  const { id } = await params;
  const numericId = Number(id);
  if (!Number.isInteger(numericId) || numericId < 1) {
    return <ErrorPanel message={`Unknown media id: ${id}`} />;
  }
  return <MediaDetailClient id={numericId} />;
}
