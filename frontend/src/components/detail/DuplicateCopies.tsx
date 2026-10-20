"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { mediaFileUrl } from "@/lib/api";
import { dupLabel } from "@/lib/format";
import type { DuplicateCopy, MediaDetail } from "@/lib/types";

/** Video extensions the collector stores; everything else previews as an image. */
const VIDEO_EXTENSIONS = new Set(["mp4", "webm"]);

/** What this copy is and when (or whether) it gets auto-deleted (PRD §12.1). */
function caption(copy: DuplicateCopy): string {
  if (copy.is_retained) return "Kept copy · never deleted";
  if (copy.dup_status === "dup" && copy.dup_expires_at) {
    return `Flagged dup · until ${copy.dup_expires_at.slice(0, 10)}`;
  }
  if (copy.dup_status === "unflagged") return "Unflagged · never deleted";
  return dupLabel(copy.dup_status);
}

/**
 * The other copies of the same bytes under the preview (PRD §12.1): seeing
 * the retained copy beside the item is what makes a dup flag judgeable.
 * Each copy opens full size with the current grid's query string, so the
 * prev/next browsing continues from there.
 */
export default function DuplicateCopies({ item }: { item: MediaDetail }) {
  const searchParams = useSearchParams();
  if (item.duplicates.length === 0) return null;

  const qs = searchParams.toString();
  const selfRetained = item.duplicates.every((copy) => !copy.is_retained);
  const count = item.duplicates.length;

  return (
    <section className="dup-group" aria-label="Duplicate group">
      <h3 className="dup-group-head">
        {count === 1 ? "1 other copy of this file" : `${count} other copies of this file`}
        <span className="inline-msg">
          {selfRetained
            ? "— this is the kept copy"
            : `— the kept copy is media #${item.dup_of_media_id ?? "?"}`}
        </span>
      </h3>
      <div className="dup-group-row">
        {item.duplicates.map((copy) => {
          const isVideo = VIDEO_EXTENSIONS.has((copy.extension ?? "").toLowerCase());
          return (
            <Link
              key={copy.id}
              className="dup-copy"
              href={`/media/${copy.id}${qs === "" ? "" : `?${qs}`}`}
              title={`Open media #${copy.id}`}
            >
              {isVideo ? (
                <video src={mediaFileUrl(copy.id, "file")} autoPlay muted loop playsInline />
              ) : (
                <img
                  src={mediaFileUrl(copy.id, "file")}
                  alt={copy.original_filename ?? `Media ${copy.id}`}
                  loading="lazy"
                  onError={(event) => {
                    const img = event.currentTarget;
                    if (img.dataset.fallback === "1") return;
                    img.dataset.fallback = "1";
                    img.src = mediaFileUrl(copy.id, "thumbnail");
                  }}
                />
              )}
              <span className="dup-copy-caption">{caption(copy)}</span>
            </Link>
          );
        })}
      </div>
    </section>
  );
}
